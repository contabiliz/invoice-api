"""
Batch endpoint E2E tests.

Covers:
  - Presign: happy path, validation errors, rate limiting
  - Confirm: happy path, partial storage failure, idempotency, cross-tenant isolation
  - Status: GET /batches/{id}
  - Documents: GET /batches/{id}/documents with filtering
  - DocumentDetail: GET /batches/{id}/documents/{doc_id}
  - ViewUrl: GET /batches/{id}/documents/{doc_id}/view-url
  - Cross-tenant isolation (T-CRIT-006, T-CRIT-009)
"""

import pytest

from tests.e2e.conftest import (
    API_PREFIX,
    SAMPLE_PDF,
    SAMPLE_XML,
    presign_one_doc,
    presign_one_pdf_doc,
    upload_to_presigned_url,
)


# ── POST /batches/presign ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_presign_happy_path(authed):
    data = await presign_one_doc(authed)

    assert "batch_id" in data
    assert len(data["documents"]) == 1

    doc = data["documents"][0]
    assert "document_id" in doc
    assert "upload_url" in doc
    assert "upload_url_expires_at" in doc
    assert "object_key" in doc


@pytest.mark.asyncio
async def test_presign_object_key_contains_batch_and_doc_id(authed):
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]
    doc = data["documents"][0]

    assert batch_id in doc["object_key"]
    assert doc["object_key"].endswith(doc["document_id"])


@pytest.mark.asyncio
async def test_presign_multiple_documents(authed):
    resp = await authed.post(
        f"{API_PREFIX}/batches/presign",
        json={
            "documents": [
                {"filename": f"doc_{i}.xml", "mime_type": "application/xml", "file_size": 1024}
                for i in range(5)
            ]
        },
    )
    assert resp.status_code == 202
    data = resp.json()
    assert len(data["documents"]) == 5
    # All document_ids must be unique
    ids = [d["document_id"] for d in data["documents"]]
    assert len(set(ids)) == 5


@pytest.mark.asyncio
async def test_presign_rejects_empty_documents(authed):
    resp = await authed.post(
        f"{API_PREFIX}/batches/presign",
        json={"documents": []},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_presign_rejects_invalid_mime_type(authed):
    resp = await authed.post(
        f"{API_PREFIX}/batches/presign",
        json={
            "documents": [
                {"filename": "virus.exe", "mime_type": "application/octet-stream", "file_size": 1024}
            ]
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_presign_rejects_zero_file_size(authed):
    resp = await authed.post(
        f"{API_PREFIX}/batches/presign",
        json={
            "documents": [
                {"filename": "empty.xml", "mime_type": "application/xml", "file_size": 0}
            ]
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_presign_rejects_file_over_50mb(authed):
    resp = await authed.post(
        f"{API_PREFIX}/batches/presign",
        json={
            "documents": [
                {"filename": "huge.pdf", "mime_type": "application/pdf", "file_size": 52_428_801}
            ]
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_presign_accepts_all_valid_mime_types(authed):
    mime_types = [
        ("image/jpeg", "photo.jpg"),
        ("image/png", "scan.png"),
        ("image/webp", "scan.webp"),
        ("image/tiff", "scan.tiff"),
        ("application/pdf", "invoice.pdf"),
        ("application/xml", "nfe.xml"),
        ("text/xml", "nfe2.xml"),
    ]
    resp = await authed.post(
        f"{API_PREFIX}/batches/presign",
        json={
            "documents": [
                {"filename": fname, "mime_type": mime, "file_size": 1024}
                for mime, fname in mime_types
            ]
        },
    )
    assert resp.status_code == 202
    assert len(resp.json()["documents"]) == len(mime_types)


# ── POST /batches/{id}/confirm ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_happy_path(authed):
    # 1. Presign
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]
    doc = data["documents"][0]

    # 2. Upload directly to Supabase Storage
    await upload_to_presigned_url(doc["upload_url"])

    # 3. Confirm
    resp = await authed.post(
        f"{API_PREFIX}/batches/{batch_id}/confirm",
        json={"document_ids": [doc["document_id"]]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert doc["document_id"] in body["confirmed"]
    assert body["failed"] == []


@pytest.mark.asyncio
async def test_confirm_object_not_found_returns_200_with_failed(authed):
    """
    T-HIGH-011: confirm where file was never uploaded.
    Must return 200 with the doc in failed[], not a 5xx.
    """
    data = await presign_one_doc(authed, filename="never_uploaded.xml")
    batch_id = data["batch_id"]
    doc = data["documents"][0]

    # Do NOT upload — confirm immediately
    resp = await authed.post(
        f"{API_PREFIX}/batches/{batch_id}/confirm",
        json={"document_ids": [doc["document_id"]]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed"] == []
    assert len(body["failed"]) == 1
    assert body["failed"][0]["document_id"] == doc["document_id"]
    assert body["failed"][0]["reason"] == "object_not_found"


@pytest.mark.asyncio
async def test_confirm_partial_failure(authed):
    """
    T-HIGH-011: 3 docs presigned, only 2 uploaded.
    Response is 200; confirmed has 2, failed has 1.
    """
    resp = await authed.post(
        f"{API_PREFIX}/batches/presign",
        json={
            "documents": [
                {"filename": f"doc_{i}.xml", "mime_type": "application/xml", "file_size": len(SAMPLE_XML)}
                for i in range(3)
            ]
        },
    )
    assert resp.status_code == 202
    data = resp.json()
    batch_id = data["batch_id"]
    docs = data["documents"]

    # Upload only the first two
    await upload_to_presigned_url(docs[0]["upload_url"])
    await upload_to_presigned_url(docs[1]["upload_url"])

    resp = await authed.post(
        f"{API_PREFIX}/batches/{batch_id}/confirm",
        json={"document_ids": [d["document_id"] for d in docs]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["confirmed"]) == 2
    assert len(body["failed"]) == 1
    assert body["failed"][0]["reason"] == "object_not_found"


@pytest.mark.asyncio
async def test_confirm_is_idempotent(authed):
    """T-HIGH-012: confirming the same batch twice must not error or duplicate."""
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]
    doc = data["documents"][0]
    await upload_to_presigned_url(doc["upload_url"])

    doc_ids = [doc["document_id"]]

    resp1 = await authed.post(
        f"{API_PREFIX}/batches/{batch_id}/confirm",
        json={"document_ids": doc_ids},
    )
    assert resp1.status_code == 200
    assert doc["document_id"] in resp1.json()["confirmed"]

    # Second call — document is already past pending_upload, must be skipped
    resp2 = await authed.post(
        f"{API_PREFIX}/batches/{batch_id}/confirm",
        json={"document_ids": doc_ids},
    )
    assert resp2.status_code == 200
    assert doc["document_id"] in resp2.json()["confirmed"]


@pytest.mark.asyncio
async def test_confirm_unknown_batch_returns_404(authed):
    resp = await authed.post(
        f"{API_PREFIX}/batches/00000000-0000-0000-0000-000000000000/confirm",
        json={"document_ids": ["00000000-0000-0000-0000-000000000001"]},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_confirm_rejects_empty_document_ids(authed):
    data = await presign_one_doc(authed)
    resp = await authed.post(
        f"{API_PREFIX}/batches/{data['batch_id']}/confirm",
        json={"document_ids": []},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_confirm_rejects_doc_ids_from_other_batch(authed):
    """T-HIGH-013: document IDs that belong to a different batch must return 422."""
    batch_a = await presign_one_doc(authed, filename="a.xml")
    batch_b = await presign_one_doc(authed, filename="b.xml")

    # Try to confirm batch_a using batch_b's document ID
    resp = await authed.post(
        f"{API_PREFIX}/batches/{batch_a['batch_id']}/confirm",
        json={"document_ids": [batch_b["documents"][0]["document_id"]]},
    )
    assert resp.status_code == 422


# ── PDF document tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_happy_path_pdf(authed):
    # 1. Presign a PDF
    data = await presign_one_pdf_doc(authed)
    batch_id = data["batch_id"]
    doc = data["documents"][0]

    # 2. Upload PDF directly to Supabase Storage
    await upload_to_presigned_url(doc["upload_url"], content=SAMPLE_PDF, content_type="application/pdf")

    # 3. Confirm
    resp = await authed.post(
        f"{API_PREFIX}/batches/{batch_id}/confirm",
        json={"document_ids": [doc["document_id"]]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert doc["document_id"] in body["confirmed"]
    assert body["failed"] == []


@pytest.mark.asyncio
async def test_list_documents_pdf_doc_type(authed):
    """PDF documents must be listed with doc_type == 'pdf'."""
    data = await presign_one_pdf_doc(authed)
    batch_id = data["batch_id"]

    resp = await authed.get(f"{API_PREFIX}/batches/{batch_id}/documents")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["doc_type"] == "pdf"


# ── GET /batches/{id} ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_batch_status_after_presign(authed):
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]

    resp = await authed.get(f"{API_PREFIX}/batches/{batch_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch_id"] == batch_id
    assert body["status"] == "pending_upload"
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_get_batch_status_unknown_returns_404(authed):
    resp = await authed.get(f"{API_PREFIX}/batches/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_batch_status_fields(authed):
    data = await presign_one_doc(authed)
    resp = await authed.get(f"{API_PREFIX}/batches/{data['batch_id']}")
    body = resp.json()

    for field in ("batch_id", "status", "total", "queued", "processing", "completed", "failed", "created_at"):
        assert field in body, f"Missing field: {field}"


# ── GET /batches/{id}/documents ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_documents_after_presign(authed):
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]

    resp = await authed.get(f"{API_PREFIX}/batches/{batch_id}/documents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert len(body["items"]) == 1

    item = body["items"][0]
    assert item["status"] == "pending_upload"
    assert item["doc_type"] == "xml"


@pytest.mark.asyncio
async def test_list_documents_status_filter(authed):
    """Filter by status=pending_upload should return the doc; failed should return empty."""
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]

    resp = await authed.get(
        f"{API_PREFIX}/batches/{batch_id}/documents",
        params={"status": "pending_upload"},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    resp = await authed.get(
        f"{API_PREFIX}/batches/{batch_id}/documents",
        params={"status": "failed"},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_list_documents_pagination(authed):
    resp = await authed.post(
        f"{API_PREFIX}/batches/presign",
        json={
            "documents": [
                {"filename": f"p_{i}.xml", "mime_type": "application/xml", "file_size": 512}
                for i in range(10)
            ]
        },
    )
    assert resp.status_code == 202
    batch_id = resp.json()["batch_id"]

    page1 = await authed.get(
        f"{API_PREFIX}/batches/{batch_id}/documents",
        params={"page": 1, "page_size": 6},
    )
    page2 = await authed.get(
        f"{API_PREFIX}/batches/{batch_id}/documents",
        params={"page": 2, "page_size": 6},
    )
    assert page1.status_code == 200
    assert page2.status_code == 200
    assert page1.json()["total"] == 10
    assert len(page1.json()["items"]) == 6
    assert len(page2.json()["items"]) == 4


# ── Cross-tenant isolation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_tenant_get_batch_returns_404(authed, authed2):
    """T-CRIT-006: user2 must not see user1's batch."""
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]

    resp = await authed2.get(f"{API_PREFIX}/batches/{batch_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_list_documents_returns_404(authed, authed2):
    """T-CRIT-006: user2 must not list user1's documents."""
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]

    resp = await authed2.get(f"{API_PREFIX}/batches/{batch_id}/documents")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_confirm_returns_404(authed, authed2):
    """T-CRIT-009: user2 confirm on user1's batch must return 404, no changes."""
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]
    doc_id = data["documents"][0]["document_id"]

    resp = await authed2.post(
        f"{API_PREFIX}/batches/{batch_id}/confirm",
        json={"document_ids": [doc_id]},
    )
    assert resp.status_code == 404


# ── GET /batches/{id}/documents/{doc_id} ─────────────────────────────────────

@pytest.mark.asyncio
async def test_get_document_happy_path(authed):
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]
    doc_id = data["documents"][0]["document_id"]

    resp = await authed.get(f"{API_PREFIX}/batches/{batch_id}/documents/{doc_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == doc_id
    assert body["filename"] == "nota.xml"
    assert body["doc_type"] == "xml"
    assert body["status"] == "pending_upload"
    assert body["extracted_data"] is None
    assert body["error_reason"] is None
    assert "created_at" in body
    assert "updated_at" in body


@pytest.mark.asyncio
async def test_get_document_pdf_doc_type(authed):
    data = await presign_one_pdf_doc(authed)
    batch_id = data["batch_id"]
    doc_id = data["documents"][0]["document_id"]

    resp = await authed.get(f"{API_PREFIX}/batches/{batch_id}/documents/{doc_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == doc_id
    assert body["doc_type"] == "pdf"
    assert body["status"] == "pending_upload"


@pytest.mark.asyncio
async def test_get_document_unknown_doc_returns_404(authed):
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]

    resp = await authed.get(
        f"{API_PREFIX}/batches/{batch_id}/documents/00000000-0000-0000-0000-000000000000"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_document_unknown_batch_returns_404(authed):
    resp = await authed.get(
        f"{API_PREFIX}/batches/00000000-0000-0000-0000-000000000000"
        f"/documents/00000000-0000-0000-0000-000000000001"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_document_doc_from_other_batch_returns_404(authed):
    """Document ID that exists but belongs to a different batch must return 404."""
    batch_a = await presign_one_doc(authed, filename="a.xml")
    batch_b = await presign_one_doc(authed, filename="b.xml")

    doc_b_id = batch_b["documents"][0]["document_id"]
    resp = await authed.get(
        f"{API_PREFIX}/batches/{batch_a['batch_id']}/documents/{doc_b_id}"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_document_cross_tenant_returns_404(authed, authed2):
    """T-CRIT-006: user2 must not retrieve user1's document detail."""
    data = await presign_one_doc(authed)
    batch_id = data["batch_id"]
    doc_id = data["documents"][0]["document_id"]

    resp = await authed2.get(f"{API_PREFIX}/batches/{batch_id}/documents/{doc_id}")
    assert resp.status_code == 404


# ── GET /batches/{id}/documents/{doc_id}/view-url ────────────────────────────

@pytest.mark.asyncio
async def test_get_view_url_returns_409_when_pending_upload(authed):
    """Document in pending_upload status must not be viewable."""
    data = await presign_one_pdf_doc(authed)
    batch_id = data["batch_id"]
    doc_id = data["documents"][0]["document_id"]

    resp = await authed.get(
        f"{API_PREFIX}/batches/{batch_id}/documents/{doc_id}/view-url"
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_get_view_url_happy_path_after_confirm(authed):
    """Confirmed document must return a presigned URL with expiry."""
    data = await presign_one_pdf_doc(authed)
    batch_id = data["batch_id"]
    doc = data["documents"][0]

    await upload_to_presigned_url(doc["upload_url"], content=SAMPLE_PDF, content_type="application/pdf")
    await authed.post(
        f"{API_PREFIX}/batches/{batch_id}/confirm",
        json={"document_ids": [doc["document_id"]]},
    )

    resp = await authed.get(
        f"{API_PREFIX}/batches/{batch_id}/documents/{doc['document_id']}/view-url"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "url" in body
    assert "expires_at" in body
    assert body["url"].startswith("http")


@pytest.mark.asyncio
async def test_get_view_url_unknown_document_returns_404(authed):
    data = await presign_one_pdf_doc(authed)
    batch_id = data["batch_id"]

    resp = await authed.get(
        f"{API_PREFIX}/batches/{batch_id}/documents/00000000-0000-0000-0000-000000000000/view-url"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_view_url_cross_tenant_returns_404(authed, authed2):
    """T-CRIT-006: user2 must not obtain a view URL for user1's document."""
    data = await presign_one_pdf_doc(authed)
    batch_id = data["batch_id"]
    doc = data["documents"][0]

    await upload_to_presigned_url(doc["upload_url"], content=SAMPLE_PDF, content_type="application/pdf")
    await authed.post(
        f"{API_PREFIX}/batches/{batch_id}/confirm",
        json={"document_ids": [doc["document_id"]]},
    )

    resp = await authed2.get(
        f"{API_PREFIX}/batches/{batch_id}/documents/{doc['document_id']}/view-url"
    )
    assert resp.status_code == 404
