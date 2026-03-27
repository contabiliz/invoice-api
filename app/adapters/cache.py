"""
Cache/queue adapter — abstract port + Redis implementation.

Responsibilities:
- Backpressure: check queue depth before accepting a new batch (PERF-008).
- Rate limiting: per-user sliding window counter for POST /batches/presign.
- Publish: send UploadConfirmationNotification to the uploads channel.

To migrate to AWS:
- Replace with SNS/EventBridge publish for notifications.
- Replace queue depth check with SQS ApproximateNumberOfMessages.
"""

import json
from abc import ABC, abstractmethod
from uuid import UUID

from redis.asyncio import Redis

from invoice_models import UploadConfirmationNotification


class CachePort(ABC):

    @abstractmethod
    async def queue_depth(self) -> int:
        """Return current depth of the uploads channel / processing queue."""

    @abstractmethod
    async def is_rate_limited(self, user_id: UUID) -> bool:
        """
        Return True if the user has exceeded the presign rate limit.
        Implements a fixed-window counter (resets every 60 seconds).
        """

    @abstractmethod
    async def publish_upload_confirmed(
        self,
        notification: UploadConfirmationNotification,
    ) -> None:
        """Publish a document upload confirmation to the Consumer."""


class RedisAdapter(CachePort):
    """
    Redis adapter using redis.asyncio (redis-py >= 5).

    Queue depth is measured via LLEN on the primary Celery queue.
    Rate limiting uses a per-user INCR key with 60-second TTL.
    Notifications are published to UPLOADS_CHANNEL via PUBLISH.
    """

    def __init__(
        self,
        client: Redis,
        channel: str,
        high_water_mark: int,
        rate_limit: int,
        queue_key: str = "celery",
    ) -> None:
        self._client = client
        self._channel = channel
        self._high_water_mark = high_water_mark
        self._rate_limit = rate_limit
        self._queue_key = queue_key

    async def queue_depth(self) -> int:
        depth = await self._client.llen(self._queue_key)
        return int(depth)

    async def is_rate_limited(self, user_id: UUID) -> bool:
        key = f"rate:presign:{user_id}"
        count = await self._client.incr(key)
        if count == 1:
            # First request in window — set 60-second expiry
            await self._client.expire(key, 60)
        return count > self._rate_limit

    async def publish_upload_confirmed(
        self,
        notification: UploadConfirmationNotification,
    ) -> None:
        payload = notification.model_dump_json()
        await self._client.publish(self._channel, payload)
