from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import time
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

RATE_LIMIT_STORE_TIMEOUT_SECONDS = 2.0


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


class RateLimitStore(Protocol):
    async def increment(self, key: str, window_seconds: int) -> int: ...


class RedisRateLimitStore:
    """Atomic shared counter for horizontally scaled API processes."""

    def __init__(self, redis_url: str) -> None:
        self.client = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=RATE_LIMIT_STORE_TIMEOUT_SECONDS,
            socket_timeout=RATE_LIMIT_STORE_TIMEOUT_SECONDS,
            retry_on_timeout=False,
        )

    async def increment(self, key: str, window_seconds: int) -> int:
        pipeline = self.client.pipeline(transaction=True)
        pipeline.incr(key)
        pipeline.expire(key, window_seconds + 1)
        result = await pipeline.execute()
        return int(result[0])


class RateLimitMiddleware:
    """Distributed in production, deterministic local guard elsewhere.

    Production fails closed if the shared counter is unavailable. Every request
    is charged to its connection source before authentication; an unverified
    Authorization value can therefore never create a fresh rate-limit bucket.
    The source address is represented only by a SHA-256 digest.
    """

    def __init__(
        self,
        app: ASGIApp,
        requests: int = 180,
        window_seconds: int = 60,
        redis_url: str = "redis://localhost:6379/0",
        distributed_required: bool = False,
        store: RateLimitStore | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        store_timeout_seconds: float = RATE_LIMIT_STORE_TIMEOUT_SECONDS,
        trusted_proxy_cidrs: list[str] | None = None,
    ) -> None:
        self.app = app
        self.requests = requests
        self.window_seconds = window_seconds
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.distributed_required = distributed_required
        self.store = store or (RedisRateLimitStore(redis_url) if distributed_required else None)
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        if store_timeout_seconds <= 0:
            raise ValueError("store_timeout_seconds must be positive")
        self.store_timeout_seconds = store_timeout_seconds
        self.trusted_proxy_networks = tuple(
            ipaddress.ip_network(value, strict=False) for value in (trusted_proxy_cidrs or [])
        )

    def _client_key(self, scope: Scope) -> str:
        client = scope.get("client")
        source_address = str(client[0]) if client else "unknown"
        try:
            peer = ipaddress.ip_address(source_address)
        except ValueError:
            peer = None
        if peer is not None and any(peer in network for network in self.trusted_proxy_networks):
            headers = scope.get("headers", [])
            forwarded_values = [
                raw_value.decode("latin-1").strip()
                for raw_name, raw_value in headers
                if raw_name.lower() == b"x-forwarded-for"
            ]
            # The reviewed edge must replace (never append to) X-Forwarded-For.
            # Multiple fields, comma-separated chains, ports and malformed IPs
            # collapse safely to the trusted peer's shared bucket.
            if len(forwarded_values) == 1 and "," not in forwarded_values[0]:
                candidate = forwarded_values[0]
                with suppress(ValueError):
                    source_address = str(ipaddress.ip_address(candidate))
        digest = hashlib.sha256(source_address.encode("utf-8")).hexdigest()
        return f"source-ip:{digest}"

    async def _local_count(self, key: str) -> int:
        now = self.monotonic_clock()
        bucket = self.hits[key]
        while bucket and bucket[0] <= now - self.window_seconds:
            bucket.popleft()
        bucket.append(now)
        return len(bucket)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") in {"/health", "/ready"}:
            await self.app(scope, receive, send)
            return

        client_key = self._client_key(scope)
        try:
            if self.store is not None:
                window = int(self.wall_clock() // self.window_seconds)
                async with asyncio.timeout(self.store_timeout_seconds):
                    count = await self.store.increment(
                        f"custombuild:rate-limit:{window}:{client_key}", self.window_seconds
                    )
            else:
                count = await self._local_count(client_key)
        except (RedisError, TimeoutError):
            if not self.distributed_required:
                count = await self._local_count(client_key)
            else:
                response = JSONResponse(
                    {"detail": "Shared rate-limit dependency is unavailable"}, status_code=503
                )
                await response(scope, receive, send)
                return

        if count > self.requests:
            response = JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(self.window_seconds)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


ALLOWED_UPLOAD_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),
    "application/pdf": (b"%PDF-",),
    "image/vnd.dxf": (b"0\nSECTION", b"  0\r\nSECTION"),
    "application/dxf": (b"0\nSECTION", b"  0\r\nSECTION"),
}
ALLOWED_UPLOAD_EXTENSIONS: dict[str, frozenset[str]] = {
    "image/png": frozenset({"png"}),
    "image/jpeg": frozenset({"jpg", "jpeg"}),
    "image/webp": frozenset({"webp"}),
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
    signature_matches = signatures is not None and any(
        content.startswith(signature) for signature in signatures
    )
    if content_type == "image/webp":
        signature_matches = signature_matches and len(content) >= 12 and content[8:12] == b"WEBP"
    if not signature_matches:
        raise ValueError("File signature does not match an allowed media type")
    extension = filename.rpartition(".")[2].lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS[content_type]:
        raise ValueError("Filename extension does not match the declared media type")
