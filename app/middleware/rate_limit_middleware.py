"""ASGI middleware that enforces rate limits before routing."""

from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException
from starlette.requests import Request


class RateLimitMiddleware:
    """Enforce RateGuard limits for HTTP requests before the app runs.

    Resolves the client identity and the configured route/global limit
    through the existing ``RateLimiter`` facade, rejects over-limit
    requests with 429, and attaches the standard ``X-RateLimit-*``
    headers to every allowed response without buffering the body.
    """

    def __init__(
        self,
        app,
        client_key_fn,
        get_rate_limiter,
        excluded_paths=None,
        route_limits=None,
    ):
        self.app = app
        self.client_key_fn = client_key_fn
        self.get_rate_limiter = get_rate_limiter
        self.excluded_paths = set(excluded_paths or ())
        self.route_limits = route_limits or {}

    def _should_limit(self, path):
        if path == "/admin" or path.startswith("/admin/"):
            return False

        if path in self.excluded_paths:
            return path in self.route_limits

        return True

    @staticmethod
    def _merge_headers(message_headers, extra):
        headers = list(message_headers)
        existing = {
            name.decode("latin-1").lower()
            for name, _ in headers
        }

        for name, value in extra.items():
            key = name.lower()

            if key not in existing:
                headers.append(
                    (key.encode("latin-1"), value.encode("latin-1"))
                )
                existing.add(key)

        return headers

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        if not self._should_limit(path):
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        try:
            key = self.client_key_fn(request)
        except HTTPException as exc:
            response = await http_exception_handler(request, exc)
            await response(scope, receive, send)
            return

        rate_limiter = (
            self.get_rate_limiter()
            if callable(self.get_rate_limiter)
            else self.get_rate_limiter
        )

        allowed = rate_limiter.allow_request(key, route=path)
        headers = rate_limiter.rate_limit_headers(key, route=path)

        if not allowed:
            retry_after = headers["X-RateLimit-Reset"]

            exc = HTTPException(
                status_code=429,
                detail={
                    "error": "Too many requests",
                    "retry_after": int(retry_after),
                },
                headers={
                    **headers,
                    "Retry-After": retry_after,
                },
            )

            response = await http_exception_handler(request, exc)
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["rate_limit_headers"] = headers

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": self._merge_headers(
                        message["headers"],
                        headers,
                    ),
                }

            await send(message)

        await self.app(scope, receive, send_with_headers)