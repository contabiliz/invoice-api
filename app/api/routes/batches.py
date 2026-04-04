"""
Batch endpoints:

  POST /batches/presign            — create batch, generate presigned upload URLs
  POST /batches/{batch_id}/confirm — verify uploads, publish to Redis
  GET  /batches/{batch_id}         — batch status summary
  GET  /batches/{batch_id}/documents — paginated document list
"""

import asyncio
import re
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.adapters.cache import CachePort
from app.adapters.database import DatabasePort, _mime_to_doc_type
from app.adapters.storage import StoragePort
from app.api.deps import get_cache, get_current_user, get_db, get_storage
from app.core.config import settings
from app.core.logging import get_logger
from invoice_models import (
    BatchPresignRequest,
    BatchPresignResponse,
    DocumentListResponse,
    DocumentPresignUrl,
    DocumentStatus,
    DocumentDetailResponse,
    UploadConfirmationNotification,
)
from invoice_models.api.batches import BatchStatusResponse

logger = get_logger(__name__)

_PRESIGN_SEMAPHORE = asyncio.Semaphore(30)

_VIEWABLE_STATUSES = {"upload_confirmed", "queued", "processing", "completed", "failed"}

router = APIRouter(prefix="/batches", tags=["batches"])

# SEC-004: object key must be {user_id}/{batch_id}/{doc_id}
_OBJECT_KEY_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _build_object_key(user_id: UUID, batch_id: UUID, doc_id: UUID) -> str:
    return f"{user_id}/{batch_id}/{doc_id}"


def _validate_object_key(key: str, user_id: UUID) -> None:
    """
    SEC-004: Reject any key that does not match the expected pattern
    or that does not belong to the authenticated user.
    """
    if not _OBJECT_KEY_RE.match(key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid object key format: {key}",
        )
    if not key.startswith(str(user_id) + "/"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Object key does not belong to authenticated user",
        )


# ---------------------------------------------------------------------------
# POST /batches/presign
# ---------------------------------------------------------------------------


@router.post(
    "/presign",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=BatchPresignResponse,
    summary="Create batch and generate presigned upload URLs",
)
async def presign_batch(
    body: BatchPresignRequest,
    user_id: UUID = Depends(get_current_user),
    db: DatabasePort = Depends(get_db),
    storage: StoragePort = Depends(get_storage),
    cache: CachePort = Depends(get_cache),
) -> BatchPresignResponse:
    # PERF-008: backpressure — reject if queue is too deep
    depth = await cache.queue_depth()
    if depth > settings.QUEUE_HIGH_WATER_MARK:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Service is busy — please retry later",
        )

    # SEC-005: per-user rate limit on presign
    if await cache.is_rate_limited(user_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded — max 10 batch creations per minute",
        )

    # Assign UUIDs and build object keys before any DB write
    batch_id = uuid4()
    doc_entries: list[dict] = []
    for doc in body.documents:
        doc_id = uuid4()
        doc_type = _mime_to_doc_type(doc.mime_type)
        s3_key = _build_object_key(user_id, batch_id, doc_id)
        doc_entries.append(
            {
                "id": doc_id,
                "filename": doc.filename,
                "s3_key": s3_key,
                "file_size": doc.file_size,
                "doc_type": doc_type,
            }
        )

    # Persist batch + documents in a single transaction
    await db.create_batch_with_documents(batch_id, user_id, doc_entries)

    # Generate presigned URLs concurrently — FW-003 / performance requirement
    async def _presign(entry: dict) -> tuple[dict, str, datetime]:
        async with _PRESIGN_SEMAPHORE:
            url, expires_at = await storage.generate_presigned_upload_url(
                entry["s3_key"],
                settings.PRESIGN_EXPIRY_SECONDS,
            )
        return entry, url, expires_at

    async with asyncio.TaskGroup() as tg:
        presign_tasks = [tg.create_task(_presign(e)) for e in doc_entries]

    result_docs: list[DocumentPresignUrl] = []
    for task in presign_tasks:
        entry, url, expires_at = task.result()
        result_docs.append(
            DocumentPresignUrl(
                document_id=entry["id"],
                upload_url=url,
                upload_url_expires_at=expires_at,
                object_key=entry["s3_key"],
            )
        )

    logger.info(
        "batch.presigned",
        batch_id=str(batch_id),
        document_count=len(result_docs),
    )
    return BatchPresignResponse(batch_id=batch_id, documents=result_docs)


# ---------------------------------------------------------------------------
# POST /batches/{batch_id}/confirm
# ---------------------------------------------------------------------------


class ConfirmRequest(BaseModel):
    document_ids: list[UUID] = Field(min_length=1, max_length=500)


class FailedDocument(BaseModel):
    document_id: UUID
    reason: str


class ConfirmResponse(BaseModel):
    confirmed: list[UUID]
    failed: list[FailedDocument]


@router.post(
    "/{batch_id}/confirm",
    status_code=status.HTTP_200_OK,
    response_model=ConfirmResponse,
    summary="Confirm uploads and publish to Redis",
)
async def confirm_batch(
    batch_id: UUID,
    body: ConfirmRequest,
    user_id: UUID = Depends(get_current_user),
    db: DatabasePort = Depends(get_db),
    storage: StoragePort = Depends(get_storage),
    cache: CachePort = Depends(get_cache),
) -> ConfirmResponse:
    # Verify batch exists and belongs to the authenticated user
    batch_row = await db.get_batch(batch_id, user_id)
    if batch_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found"
        )

    if batch_row["status"] not in (
        "pending_upload",
        "processing",  # already confirmed but partially done — idempotent path
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Batch is not confirmable (status={batch_row['status']})",
        )

    # Validate that all submitted document_ids belong to this batch/user
    doc_rows = await db.get_documents_for_confirm(batch_id, user_id, body.document_ids)
    found_ids = {row["id"] for row in doc_rows}
    unknown_ids = set(body.document_ids) - found_ids
    if unknown_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Document IDs not found in this batch: {[str(i) for i in unknown_ids]}",
        )

    # Skip documents that are already past pending_upload (idempotency)
    to_verify = [
        row for row in doc_rows if row["status"] == DocumentStatus.pending_upload.value
    ]
    already_done = [
        row["id"]
        for row in doc_rows
        if row["status"] != DocumentStatus.pending_upload.value
    ]

    # Verify object existence concurrently — asyncio.TaskGroup
    async def _check(row: object) -> tuple[UUID, str, bool]:
        exists = await storage.object_exists(row["s3_key"])
        return row["id"], row["s3_key"], exists

    confirmed_ids: list[UUID] = list(already_done)
    failed_docs: list[FailedDocument] = []
    new_confirmed: list[UUID] = []
    new_failed: list[UUID] = []

    if to_verify:
        async with asyncio.TaskGroup() as tg:
            check_tasks = [tg.create_task(_check(row)) for row in to_verify]

        for task in check_tasks:
            doc_id, s3_key, exists = task.result()
            if exists:
                new_confirmed.append(doc_id)
                confirmed_ids.append(doc_id)
            else:
                new_failed.append(doc_id)
                failed_docs.append(
                    FailedDocument(document_id=doc_id, reason="object_not_found")
                )

        # Persist status changes in one transaction
        await db.confirm_documents(new_confirmed, new_failed)

    # Publish confirmed documents to Redis (post-transaction, sequential)
    for doc_id in new_confirmed:
        notification = UploadConfirmationNotification(
            document_id=doc_id,
            batch_id=batch_id,
        )
        await cache.publish_upload_confirmed(notification)

    logger.info(
        "batch.confirmed",
        batch_id=str(batch_id),
        confirmed=len(confirmed_ids),
        failed=len(failed_docs),
    )
    return ConfirmResponse(confirmed=confirmed_ids, failed=failed_docs)


# ---------------------------------------------------------------------------
# GET /batches/{batch_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{batch_id}",
    response_model=BatchStatusResponse,
    summary="Get batch status",
)
async def get_batch_status(
    batch_id: UUID,
    user_id: UUID = Depends(get_current_user),
    db: DatabasePort = Depends(get_db),
) -> BatchStatusResponse:
    result = await db.get_batch_status(batch_id, user_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found"
        )
    return result


# ---------------------------------------------------------------------------
# GET /batches/{batch_id}/documents
# ---------------------------------------------------------------------------


@router.get(
    "/{batch_id}/documents",
    response_model=DocumentListResponse,
    summary="List documents in a batch",
)
async def list_documents(
    batch_id: UUID,
    user_id: UUID = Depends(get_current_user),
    db: DatabasePort = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status_filter: DocumentStatus | None = Query(default=None, alias="status"),
) -> DocumentListResponse:
    # Verify batch ownership before listing
    batch_row = await db.get_batch(batch_id, user_id)
    if batch_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found"
        )

    return await db.list_documents(batch_id, user_id, page, page_size, status_filter)


# ---------------------------------------------------------------------------
# GET /{batch_id}/documentd/{document_id}
# ---------------------------------------------------------------------------
@router.get(
    "/{batch_id}/documents/{document_id}",
    response_model=DocumentDetailResponse,
    summary="Get single document detail",
)
async def get_document(
    batch_id: UUID,
    document_id: UUID,
    user_id: UUID = Depends(get_current_user),
    db: DatabasePort = Depends(get_db),
):
    row = await db.get_document(batch_id, user_id, document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return row


# ---------------------------------------------------------------------------
# GET /{batch_id}/documents/{document_id}/view-url
# ---------------------------------------------------------------------------
#
class ViewUrlResponse(BaseModel):
    url: str
    expires_at: datetime


@router.get(
    "/{batch_id}/documents/{document_id}/view-url",
    response_model=ViewUrlResponse,
    summary="Create view url for PDF documents",
)
async def get_document_view_url(
    batch_id: UUID,
    document_id: UUID,
    user_id: UUID = Depends(get_current_user),
    db: DatabasePort = Depends(get_db),
    storage: StoragePort = Depends(get_storage),
) -> ViewUrlResponse:
    row = await db.get_document(batch_id, user_id, document_id)

    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")

    if row["status"] not in _VIEWABLE_STATUSES:
        raise HTTPException(
            status_code=409, detail="Document not available for viewing"
        )

    url, expires_at = await storage.generate_presigned_read_url(
        row["s3_key"], settings.VIEW_URL_EXPIRY_SECONDS
    )
    return ViewUrlResponse(url=url, expires_at=expires_at)
