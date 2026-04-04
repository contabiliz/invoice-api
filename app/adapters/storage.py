"""
Storage adapter — abstract port + Supabase Storage implementation.

To migrate to S3:
1. Implement StoragePort with boto3/aiobotocore.
2. Swap the concrete class injected at lifespan startup.
The rest of the application is unaffected.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


class StoragePort(ABC):
    """Abstract interface for object storage operations."""

    @abstractmethod
    async def generate_presigned_upload_url(
        self,
        object_key: str,
        expires_in: int,
    ) -> tuple[str, datetime]:
        """
        Generate a presigned URL for a PUT upload.

        Returns:
            (upload_url, expires_at)
        """

    @abstractmethod
    async def generate_presigned_read_url(
        self,
        object_key: str,
        expires_in: int,
    ) -> tuple[str, datetime]:
        """
        Generate a presigned URL for a GET.

        Returns:
            (url, expires_at)
        """

    @abstractmethod
    async def object_exists(self, object_key: str) -> bool:
        """Return True if the object exists in storage."""


class SupabaseStorageAdapter(StoragePort):
    """
    Supabase Storage adapter using the REST API directly via httpx.

    Does NOT use the Supabase Python client — keeps the adapter
    layer clean and dependency-free from Supabase-specific SDKs.
    """

    def __init__(
        self,
        supabase_url: str,
        service_role_key: str,
        bucket: str,
        client: httpx.AsyncClient,
    ) -> None:
        self._base = supabase_url.rstrip("/")
        self._key = service_role_key
        self._bucket = bucket
        self._client = client
        self._auth_headers = {
            "Authorization": f"Bearer {service_role_key}",
            "apikey": service_role_key,
        }

    async def generate_presigned_upload_url(
        self,
        object_key: str,
        expires_in: int,
    ) -> tuple[str, datetime]:
        """
        Call Supabase Storage to create a signed upload URL.

        POST /storage/v1/object/sign/upload/{bucket}/{key}
        """
        url = f"{self._base}/storage/v1/object/upload/sign/{self._bucket}/{object_key}"
        resp = await self._client.post(
            url,
            headers={**self._auth_headers, "Content-Type": "application/json"},
            json={"upsert": False, "expiresIn": expires_in},
        )
        if not resp.is_success:
            logger.error(
                "storage.presign_failed",
                status=resp.status_code,
                body=resp.text,
                object_key=object_key,
            )
            resp.raise_for_status()

        data = resp.json()
        signed_path: str = data.get("url") or data.get("signedURL", "")
        if not signed_path:
            raise RuntimeError(f"Unexpected storage response: {data}")

        # Supabase returns a path relative to /storage/v1; prepend full base if needed
        if signed_path.startswith("/"):
            upload_url = f"{self._base}/storage/v1{signed_path}"
        else:
            upload_url = signed_path

        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)
        return upload_url, expires_at

    async def generate_presigned_read_url(
        self,
        object_key: str,
        expires_in: int,
    ) -> tuple[str, datetime]:
        """
        Call Supabase Storage to create a signed GET URL.

        POST /storage/v1/object/sign/{bucket}/{key}
        """
        url = f"{self._base}/storage/v1/object/sign/{self._bucket}/{object_key}"
        resp = await self._client.post(
            url,
            headers={**self._auth_headers, "Content-Type": "application/json"},
            json={"expiresIn": expires_in},
        )
        if not resp.is_success:
            logger.error(
                "storage.view_presign_failed",
                status=resp.status_code,
                body=resp.text,
                object_key=object_key,
            )
            resp.raise_for_status()

        data = resp.json()
        signed_path: str = data.get("url") or data.get("signedURL", "")
        if not signed_path:
            raise RuntimeError(f"Unexpected storage response: {data}")

        # Supabase returns a path relative to /storage/v1; prepend full base if needed
        if signed_path.startswith("/"):
            view_url = f"{self._base}/storage/v1{signed_path}"
        else:
            view_url = signed_path

        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)
        return view_url, expires_at

    async def object_exists(self, object_key: str) -> bool:
        """
        Issue a HEAD request to verify the object was uploaded.

        HEAD /storage/v1/object/{bucket}/{key}
        """
        url = f"{self._base}/storage/v1/object/{self._bucket}/{object_key}"
        try:
            resp = await self._client.head(url, headers=self._auth_headers)
            return resp.status_code == 200
        except httpx.HTTPStatusError:
            return False
        except Exception as exc:
            logger.warning("storage.head_failed", object_key=object_key, error=str(exc))
            return False
