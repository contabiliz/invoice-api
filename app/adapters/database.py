"""
Database adapter — abstract port + asyncpg implementation.

Uses asyncpg directly (not Supabase Python client) as required by FW-013.
All queries filter by user_id for tenant isolation (SEC-002).

To migrate to AWS RDS:
1. DATABASE_URL points to RDS — no code changes needed.
2. If a different async driver is needed, implement DatabasePort.
"""

from abc import ABC, abstractmethod
from uuid import UUID

import asyncpg

from invoice_models import (
    BatchStatus,
    DocType,
    DocumentStatus,
    MimeType,
)
from invoice_models.api.batches import BatchStatusResponse
from invoice_models.api.documents import DocumentListResponse, DocumentStatusItem


def _mime_to_doc_type(mime_type: MimeType) -> DocType:
    if mime_type in (MimeType.xml, MimeType.xml_text):
        return DocType.xml
    if mime_type == MimeType.pdf:
        return DocType.pdf
    return DocType.img


class DatabasePort(ABC):
    @abstractmethod
    async def create_batch_with_documents(
        self,
        batch_id: UUID,
        user_id: UUID,
        documents: list[dict],  # {id, filename, s3_key, file_size, doc_type}
    ) -> None:
        """Insert batch + all documents in one transaction using the given batch_id."""

    @abstractmethod
    async def get_batch(self, batch_id: UUID, user_id: UUID) -> asyncpg.Record | None:
        """Fetch a batch row filtered by user_id."""

    @abstractmethod
    async def get_documents_for_confirm(
        self,
        batch_id: UUID,
        user_id: UUID,
        document_ids: list[UUID],
    ) -> list[asyncpg.Record]:
        """
        Return document rows for the given IDs that belong to this
        batch and user. Used to validate ownership before confirm.
        """

    @abstractmethod
    async def confirm_documents(
        self,
        confirmed_ids: list[UUID],
        failed_ids: list[UUID],
    ) -> None:
        """
        Bulk-update document statuses after storage verification.
        confirmed → upload_confirmed, failed → failed (object_not_found).
        """

    @abstractmethod
    async def get_batch_status(
        self,
        batch_id: UUID,
        user_id: UUID,
    ) -> BatchStatusResponse | None:
        """Return batch status summary or None if not found / wrong user."""

    @abstractmethod
    async def list_documents(
        self,
        batch_id: UUID,
        user_id: UUID,
        page: int,
        page_size: int,
        status: DocumentStatus | None,
    ) -> DocumentListResponse:
        """Paginated document list for a batch."""


class AsyncpgDatabaseAdapter(DatabasePort):
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_batch_with_documents(
        self,
        batch_id: UUID,
        user_id: UUID,
        documents: list[dict],
    ) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO batches (id, user_id, status, total_documents,
                        queued_count, completed_count, failed_count)
                    VALUES ($1, $2, $3, $4, 0, 0, 0)
                    """,
                    batch_id,
                    user_id,
                    BatchStatus.pending_upload.value,
                    len(documents),
                )
                await conn.executemany(
                    """
                    INSERT INTO documents
                        (id, batch_id, user_id, original_filename, s3_key,
                         file_size, doc_type, status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    [
                        (
                            doc["id"],
                            batch_id,
                            user_id,
                            doc["filename"],
                            doc["s3_key"],
                            doc["file_size"],
                            doc["doc_type"].value,
                            DocumentStatus.pending_upload.value,
                        )
                        for doc in documents
                    ],
                )

    async def get_batch(self, batch_id: UUID, user_id: UUID) -> asyncpg.Record | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM batches WHERE id = $1 AND user_id = $2",
                batch_id,
                user_id,
            )

    async def get_documents_for_confirm(
        self,
        batch_id: UUID,
        user_id: UUID,
        document_ids: list[UUID],
    ) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, s3_key, status
                FROM documents
                WHERE id = ANY($1::uuid[])
                  AND batch_id = $2
                  AND user_id = $3
                """,
                document_ids,
                batch_id,
                user_id,
            )

    async def confirm_documents(
        self,
        confirmed_ids: list[UUID],
        failed_ids: list[UUID],
    ) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if confirmed_ids:
                    await conn.execute(
                        """
                        UPDATE documents
                        SET status = $1, updated_at = NOW()
                        WHERE id = ANY($2::uuid[])
                        """,
                        DocumentStatus.upload_confirmed.value,
                        confirmed_ids,
                    )
                if failed_ids:
                    await conn.execute(
                        """
                        UPDATE documents
                        SET status = $1, error_reason = 'object_not_found',
                            updated_at = NOW()
                        WHERE id = ANY($2::uuid[])
                        """,
                        DocumentStatus.failed.value,
                        failed_ids,
                    )

    async def get_batch_status(
        self,
        batch_id: UUID,
        user_id: UUID,
    ) -> BatchStatusResponse | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id,
                    status,
                    total_documents,
                    queued_count,
                    completed_count,
                    failed_count,
                    created_at,
                    (
                        SELECT COUNT(*) FROM documents
                        WHERE batch_id = b.id AND status = 'processing'
                    ) AS processing_count
                FROM batches b
                WHERE id = $1 AND user_id = $2
                """,
                batch_id,
                user_id,
            )
        if row is None:
            return None

        total = row["total_documents"]
        queued = row["queued_count"]
        completed = row["completed_count"]
        failed = row["failed_count"]
        processing = row["processing_count"]

        return BatchStatusResponse(
            batch_id=row["id"],
            status=BatchStatus(row["status"]),
            total=total,
            queued=queued,
            processing=processing,
            completed=completed,
            failed=failed,
            created_at=row["created_at"],
            completed_at=None,
        )

    async def list_documents(
        self,
        batch_id: UUID,
        user_id: UUID,
        page: int,
        page_size: int,
        status: DocumentStatus | None,
    ) -> DocumentListResponse:
        offset = (page - 1) * page_size

        base_where = "batch_id = $1 AND user_id = $2"
        params: list = [batch_id, user_id]

        if status is not None:
            base_where += " AND status = $3"
            params.append(status.value)
            count_params = params[:]
            list_params = params + [page_size, offset]
            status_filter = "AND status = $3"
        else:
            count_params = params[:]
            list_params = params + [page_size, offset]
            status_filter = ""

        async with self._pool.acquire() as conn:
            total: int = await conn.fetchval(
                f"SELECT COUNT(*) FROM documents WHERE {base_where}",
                *count_params,
            )

            size_idx = len(params) + 1
            offset_idx = len(params) + 2
            rows = await conn.fetch(
                f"""
                SELECT id, original_filename, doc_type, status,
                       error_reason, created_at, updated_at
                FROM documents
                WHERE batch_id = $1 AND user_id = $2 {status_filter}
                ORDER BY created_at
                LIMIT ${size_idx} OFFSET ${offset_idx}
                """,
                *list_params,
            )

        items = [
            DocumentStatusItem(
                document_id=row["id"],
                filename=row["original_filename"],
                doc_type=DocType(row["doc_type"]),
                status=DocumentStatus(row["status"]),
                error_reason=row["error_reason"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

        return DocumentListResponse(total=total, page=page, items=items)
