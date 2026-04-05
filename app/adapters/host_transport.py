import httpx


class RewriteHostTransport(httpx.AsyncBaseTransport):
    """
    Rewrites the request host from public_url to internal_url for outbound
    calls, so the storage adapter can be initialized with the public URL
    and remain unaware of Docker networking.
    """

    def __init__(
        self,
        wrapped: httpx.AsyncBaseTransport,
        public_base: str,
        internal_base: str,
    ) -> None:
        self._wrapped = wrapped
        self._public = public_base.rstrip("/")
        self._internal = internal_base.rstrip("/")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.startswith(self._public):
            rewritten = url_str.replace(self._public, self._internal, 1)
            request = httpx.Request(
                method=request.method,
                url=rewritten,
                headers=request.headers,
                stream=request.stream,
            )
        return await self._wrapped.handle_async_request(request)
