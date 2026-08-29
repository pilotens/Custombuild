from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import time
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import ExitStack, suppress
from typing import Protocol

from custombuild_manufacturing import MAX_HTTP_REQUEST_BYTES
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .storage import ArtifactStorageUnavailableError, reserve_transient_bytes

RATE_LIMIT_STORE_TIMEOUT_SECONDS = 2.0
# 4,096 messages still permits a 21 MiB body arriving in roughly 5 KiB chunks,
# while placing a small, deterministic ceiling on buffered objects and tokens.
MAX_REQUEST_BODY_FRAGMENTS = 4_096


class CORSResponseHeadersMiddleware(CORSMiddleware):
    """Add simple CORS headers without short-circuiting preflight requests.

    The regular CORS middleware remains inside the request guards so valid
    preflights receive the complete response.  This outer response-only layer
    ensures that guard failures remain browser-readable for allowed origins.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_headers = Headers(scope=scope)
        if "origin" not in request_headers:
            await self.app(scope, receive, send)
            return
        await self.simple_response(scope, receive, send, request_headers=request_headers)

    async def send(
        self,
        message: Message,
        send: Send,
        request_headers: Headers,
    ) -> None:
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            if "access-control-allow-origin" in headers:
                await send(message)
                return
        await super().send(message, send=send, request_headers=request_headers)


class _RequestBodyLimitExceeded(Exception):
    pass


class _RequestBodyLengthMismatch(Exception):
    pass


class _RequestBodyTimeout(Exception):
    pass


class _RequestBodyCapacityExceeded(Exception):
    pass


class _RequestBodyFragmentLimitExceeded(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Bound every request body before JSON or multipart parsing allocates it."""

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int = MAX_HTTP_REQUEST_BYTES,
        idle_timeout_seconds: float = 5.0,
        total_timeout_seconds: float = 30.0,
    ) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("request body limit must be a positive integer")
        self.app = app
        self.max_bytes = max_bytes
        if (
            idle_timeout_seconds <= 0
            or total_timeout_seconds <= 0
            or idle_timeout_seconds > total_timeout_seconds
        ):
            raise ValueError("request body timeouts are invalid")
        self.idle_timeout_seconds = idle_timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_headers = list(scope.get("headers", []))
        content_lengths = [
            value.strip() for name, value in raw_headers if name.lower() == b"content-length"
        ]
        transfer_encodings = [
            value for name, value in raw_headers if name.lower() == b"transfer-encoding"
        ]
        invalid_content_length = bool(content_lengths) and (
            len(content_lengths) != 1
            or not content_lengths[0].isdigit()
            or len(content_lengths[0]) > 20
            or (content_lengths[0] != b"0" and content_lengths[0].startswith(b"0"))
        )
        invalid_transfer_encoding = bool(transfer_encodings) and (
            len(transfer_encodings) != 1 or transfer_encodings[0].strip().lower() != b"chunked"
        )
        if (
            invalid_content_length
            or invalid_transfer_encoding
            or (content_lengths and transfer_encodings)
        ):
            await JSONResponse(
                {"detail": "Request body framing is invalid"},
                status_code=400,
            )(scope, receive, send)
            return
        method = str(scope.get("method", "")).upper()
        body_forbidden = method in {"GET", "HEAD", "OPTIONS"}
        if body_forbidden:
            has_declared_body = any(value != b"0" for value in content_lengths)
            if has_declared_body or transfer_encodings:
                await JSONResponse(
                    {"detail": "Request bodies are not allowed for this method"},
                    status_code=400,
                )(scope, receive, send)
                return
            declared_bytes: int | None = 0
        elif not content_lengths and not transfer_encodings:
            await JSONResponse(
                {"detail": "Content-Length is required for request bodies"},
                status_code=411,
            )(scope, receive, send)
            return
        else:
            declared_bytes = int(content_lengths[0]) if content_lengths else None
        if declared_bytes is not None and declared_bytes > self.max_bytes:
            await JSONResponse(
                {"detail": "Request body exceeds the 21 MiB limit"},
                status_code=413,
            )(scope, receive, send)
            return

        received_bytes = 0
        received_fragments = 0
        started_at = asyncio.get_running_loop().time()
        buffered_chunks: list[bytes] = []

        try:
            with ExitStack() as reservations:
                while True:
                    loop = asyncio.get_running_loop()
                    remaining = self.total_timeout_seconds - (loop.time() - started_at)
                    if remaining <= 0:
                        raise _RequestBodyTimeout
                    try:
                        async with asyncio.timeout(min(self.idle_timeout_seconds, remaining)):
                            message = await receive()
                    except TimeoutError as exc:
                        raise _RequestBodyTimeout from exc
                    if message["type"] != "http.request":
                        raise _RequestBodyLengthMismatch
                    received_fragments += 1
                    if received_fragments > MAX_REQUEST_BODY_FRAGMENTS:
                        raise _RequestBodyFragmentLimitExceeded
                    body = message.get("body", b"")
                    more_body = message.get("more_body", False)
                    if not isinstance(body, bytes) or type(more_body) is not bool:
                        raise _RequestBodyLengthMismatch
                    received_bytes += len(body)
                    if received_bytes > self.max_bytes or (
                        declared_bytes is not None and received_bytes > declared_bytes
                    ):
                        raise _RequestBodyLimitExceeded
                    if body:
                        try:
                            reservations.enter_context(reserve_transient_bytes(len(body)))
                        except ArtifactStorageUnavailableError as exc:
                            raise _RequestBodyCapacityExceeded from exc
                        buffered_chunks.append(body)
                    if not more_body:
                        break
                if declared_bytes is not None and received_bytes != declared_bytes:
                    raise _RequestBodyLengthMismatch

                replay_index = 0

                async def receive_replay() -> Message:
                    nonlocal replay_index
                    if not buffered_chunks:
                        if replay_index == 0:
                            replay_index = 1
                            return {
                                "type": "http.request",
                                "body": b"",
                                "more_body": False,
                            }
                        return await receive()
                    if replay_index < len(buffered_chunks):
                        body = buffered_chunks[replay_index]
                        replay_index += 1
                        return {
                            "type": "http.request",
                            "body": body,
                            "more_body": replay_index < len(buffered_chunks),
                        }
                    return await receive()

                await self.app(scope, receive_replay, send)
        except _RequestBodyCapacityExceeded:
            await JSONResponse(
                {"detail": "Temporary request capacity is exhausted; retry later"},
                status_code=503,
            )(scope, receive, send)
        except _RequestBodyFragmentLimitExceeded:
            await JSONResponse(
                {"detail": "Request body is too fragmented"},
                status_code=413,
            )(scope, receive, send)
        except _RequestBodyLimitExceeded:
            await JSONResponse(
                {"detail": "Request body exceeds its declared or allowed size"},
                status_code=413,
            )(scope, receive, send)
        except _RequestBodyLengthMismatch:
            await JSONResponse(
                {"detail": "Request body length does not match Content-Length"},
                status_code=400,
            )(scope, receive, send)
        except _RequestBodyTimeout:
            await JSONResponse(
                {"detail": "Request body timed out"},
                status_code=408,
            )(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["x-content-type-options"] = "nosniff"
                headers["x-frame-options"] = "DENY"
                headers["referrer-policy"] = "no-referrer"
                headers["permissions-policy"] = "camera=(), microphone=(), geolocation=()"
                headers["content-security-policy"] = (
                    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                )
                cache_control = headers.get("cache-control", "")
                directives = {
                    directive.split("=", 1)[0].strip().casefold()
                    for directive in cache_control.split(",")
                    if directive.strip()
                }
                if "no-store" not in directives:
                    headers["cache-control"] = (
                        f"{cache_control}, no-store" if cache_control else "no-store"
                    )
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
