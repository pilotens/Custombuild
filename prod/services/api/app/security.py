from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (
                            b"content-security-policy",
                            b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                        ),
                        (b"cache-control", b"no-store"),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class InMemoryRateLimitMiddleware:
    """Small-process local guard.

    Edge or Redis-backed rate limiting remains required in production.
    """

    def __init__(self, app: ASGIApp, requests: int = 180, window_seconds: int = 60) -> None:
        self.app = app
        self.requests = requests
        self.window_seconds = window_seconds
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        key = client[0] if client else "unknown"
        now = time.monotonic()
        bucket = self.hits[key]
        while bucket and bucket[0] <= now - self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.requests:
            response = JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            await response(scope, receive, send)
            return
        bucket.append(now)
        await self.app(scope, receive, send)


ALLOWED_UPLOAD_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "application/pdf": (b"%PDF-",),
    "image/vnd.dxf": (b"0\nSECTION", b"  0\r\nSECTION"),
    "application/dxf": (b"0\nSECTION", b"  0\r\nSECTION"),
}
ALLOWED_UPLOAD_EXTENSIONS: dict[str, frozenset[str]] = {
    "image/png": frozenset({"png"}),
    "image/jpeg": frozenset({"jpg", "jpeg"}),
    "application/pdf": frozenset({"pdf"}),
    "image/vnd.dxf": frozenset({"dxf"}),
    "application/dxf": frozenset({"dxf"}),
}


def validate_upload(content: bytes, content_type: str, filename: str) -> None:
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("File exceeds 20 MiB limit")
    if (
        not filename
        or len(filename.encode("utf-8")) > 255
        or filename.startswith(".")
        or filename != filename.strip()
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise ValueError("Unsafe filename")
    signatures = ALLOWED_UPLOAD_SIGNATURES.get(content_type)
    if signatures is None or not any(content.startswith(signature) for signature in signatures):
        raise ValueError("File signature does not match an allowed media type")
    extension = filename.rpartition(".")[2].lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS[content_type]:
        raise ValueError("Filename extension does not match the declared media type")
