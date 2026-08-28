from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from enum import IntEnum
from math import isfinite
from threading import Lock
from typing import Any, Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

GLOBAL_ARTIFACT_OPERATION_LIMIT = 8
MIN_ARTIFACT_OPERATION_TTL_SECONDS = 900
MAX_ARTIFACT_OPERATION_TTL_SECONDS = 86_400
REDIS_ARTIFACT_OPERATION_TIMEOUT_SECONDS = 2.0

_REDIS_GLOBAL_KEY = "custombuild:artifact-operations:active"
_REDIS_TENANT_KEY_PREFIX = "custombuild:artifact-operations:tenant:"
_HEX_256 = re.compile(r"[0-9a-f]{64}")

_ACQUIRE_SCRIPT = """
local global_key = KEYS[1]
local tenant_key = KEYS[2]
local owner_token = ARGV[1]
local global_member = ARGV[2]
local ttl_ms = tonumber(ARGV[3])
local global_limit = tonumber(ARGV[4])
local server_time = redis.call('TIME')
local now_ms = (tonumber(server_time[1]) * 1000) + math.floor(tonumber(server_time[2]) / 1000)

redis.call('ZREMRANGEBYSCORE', global_key, '-inf', now_ms)
if redis.call('GET', tenant_key) then
    return {2, tostring(now_ms), '0'}
end
if redis.call('ZCARD', global_key) >= global_limit then
    return {3, tostring(now_ms), '0'}
end

local expires_at_ms = now_ms + ttl_ms
local inserted = redis.call('SET', tenant_key, owner_token, 'PX', ttl_ms, 'NX')
if not inserted then
    return {2, tostring(now_ms), '0'}
end
redis.call('ZADD', global_key, expires_at_ms, global_member)
return {1, tostring(now_ms), tostring(expires_at_ms)}
"""

_RELEASE_SCRIPT = """
local global_key = KEYS[1]
local tenant_key = KEYS[2]
local owner_token = ARGV[1]
local global_member = ARGV[2]
local server_time = redis.call('TIME')
local now_ms = (tonumber(server_time[1]) * 1000) + math.floor(tonumber(server_time[2]) / 1000)

redis.call('ZREMRANGEBYSCORE', global_key, '-inf', now_ms)
if redis.call('GET', tenant_key) ~= owner_token then
    return 0
end
redis.call('DEL', tenant_key)
redis.call('ZREM', global_key, global_member)
return 1
"""

_RENEW_SCRIPT = """
local global_key = KEYS[1]
local tenant_key = KEYS[2]
local owner_token = ARGV[1]
local global_member = ARGV[2]
local ttl_ms = tonumber(ARGV[3])
local server_time = redis.call('TIME')
local now_ms = (tonumber(server_time[1]) * 1000) + math.floor(tonumber(server_time[2]) / 1000)

redis.call('ZREMRANGEBYSCORE', global_key, '-inf', now_ms)
if redis.call('GET', tenant_key) ~= owner_token then
    return {0, tostring(now_ms), '0'}
end
local expires_at_ms = now_ms + ttl_ms
redis.call('PEXPIRE', tenant_key, ttl_ms)
redis.call('ZADD', global_key, expires_at_ms, global_member)
return {1, tostring(now_ms), tostring(expires_at_ms)}
"""

logger = logging.getLogger(__name__)


class ArtifactOperationAcquireStatus(IntEnum):
    ACQUIRED = 1
    TENANT_BUSY = 2
    GLOBAL_CAPACITY = 3


class ArtifactOperationUnavailableError(RuntimeError):
    """The shared lease dependency could not make a trustworthy decision."""


class ArtifactOperationRejectedError(RuntimeError):
    def __init__(self, status: ArtifactOperationAcquireStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class ArtifactOperationBusyError(ArtifactOperationRejectedError):
    def __init__(self) -> None:
        super().__init__(
            ArtifactOperationAcquireStatus.TENANT_BUSY,
            "this tenant already has an active artifact operation",
        )


class ArtifactOperationCapacityError(ArtifactOperationRejectedError):
    def __init__(self) -> None:
        super().__init__(
            ArtifactOperationAcquireStatus.GLOBAL_CAPACITY,
            "global artifact-operation capacity is exhausted",
        )


class ArtifactOperationOwnershipLostError(RuntimeError):
    """The operation no longer owns its tenant lease and must stop."""


@dataclass(frozen=True, slots=True)
class ArtifactOperationAcquireResult:
    status: ArtifactOperationAcquireStatus
    server_time_ms: int
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class ArtifactOperationLease:
    tenant_key_hash: str
    owner_token: str
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class ArtifactOperationRenewResult:
    renewed: bool
    server_time_ms: int
    expires_at_ms: int


class ArtifactOperationLeaseStore(Protocol):
    async def acquire(
        self,
        tenant_key_hash: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> ArtifactOperationAcquireResult: ...

    async def release(self, tenant_key_hash: str, owner_token: str) -> bool: ...

    async def renew(
        self,
        tenant_key_hash: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> ArtifactOperationRenewResult: ...


class _RedisScriptClient(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> Any: ...


def _validate_ttl_seconds(ttl_seconds: int) -> None:
    if (
        type(ttl_seconds) is not int
        or ttl_seconds < MIN_ARTIFACT_OPERATION_TTL_SECONDS
        or ttl_seconds > MAX_ARTIFACT_OPERATION_TTL_SECONDS
    ):
        raise ValueError("artifact operation TTL must be an integer between 900 and 86400 seconds")


def _validate_operation_timeout(timeout_seconds: float) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not isfinite(float(timeout_seconds))
        or not 0 < float(timeout_seconds) <= REDIS_ARTIFACT_OPERATION_TIMEOUT_SECONDS
    ):
        raise ValueError("artifact operation Redis timeout must be between 0 and 2 seconds")
    return float(timeout_seconds)


def _tenant_key_hash(tenant_id: str) -> str:
    if (
        type(tenant_id) is not str
        or not tenant_id
        or tenant_id.strip() != tenant_id
        or len(tenant_id) > 512
    ):
        raise ValueError("artifact operation tenant identity is invalid")
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()


def _validate_lease_identity(tenant_key_hash: str, owner_token: str) -> None:
    if _HEX_256.fullmatch(tenant_key_hash) is None or _HEX_256.fullmatch(owner_token) is None:
        raise ValueError("artifact operation lease identity is invalid")


def _global_member(tenant_key_hash: str, owner_token: str) -> str:
    return f"{tenant_key_hash}:{owner_token}"


class RedisArtifactOperationStore:
    """Atomic cross-replica lease state backed by Redis server time."""

    def __init__(
        self,
        redis_url: str,
        *,
        operation_timeout_seconds: float = REDIS_ARTIFACT_OPERATION_TIMEOUT_SECONDS,
        client: _RedisScriptClient | None = None,
    ) -> None:
        if type(redis_url) is not str or not redis_url:
            raise ValueError("artifact operation Redis URL is required")
        self.operation_timeout_seconds = _validate_operation_timeout(operation_timeout_seconds)
        self.client = (
            client
            if client is not None
            else cast(
                _RedisScriptClient,
                Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=self.operation_timeout_seconds,
                    socket_timeout=self.operation_timeout_seconds,
                    retry_on_timeout=False,
                ),
            )
        )

    @staticmethod
    def _tenant_key(tenant_key_hash: str) -> str:
        if _HEX_256.fullmatch(tenant_key_hash) is None:
            raise ValueError("artifact operation tenant hash is invalid")
        return f"{_REDIS_TENANT_KEY_PREFIX}{tenant_key_hash}"

    async def acquire(
        self,
        tenant_key_hash: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> ArtifactOperationAcquireResult:
        _validate_ttl_seconds(ttl_seconds)
        _validate_lease_identity(tenant_key_hash, owner_token)
        member = _global_member(tenant_key_hash, owner_token)
        try:
            async with asyncio.timeout(self.operation_timeout_seconds):
                raw = await self.client.eval(
                    _ACQUIRE_SCRIPT,
                    2,
                    _REDIS_GLOBAL_KEY,
                    self._tenant_key(tenant_key_hash),
                    owner_token,
                    member,
                    ttl_seconds * 1000,
                    GLOBAL_ARTIFACT_OPERATION_LIMIT,
                )
        except (RedisError, TimeoutError) as exc:
            raise ArtifactOperationUnavailableError(
                "shared artifact-operation lease dependency is unavailable"
            ) from exc
        try:
            if not isinstance(raw, list | tuple) or len(raw) != 3:
                raise ValueError
            status = ArtifactOperationAcquireStatus(int(raw[0]))
            server_time_ms = int(raw[1])
            expires_at_ms = int(raw[2])
            if (
                server_time_ms < 0
                or (
                    status is ArtifactOperationAcquireStatus.ACQUIRED
                    and expires_at_ms != server_time_ms + ttl_seconds * 1000
                )
                or (status is not ArtifactOperationAcquireStatus.ACQUIRED and expires_at_ms != 0)
            ):
                raise ValueError
        except (TypeError, ValueError, IndexError) as exc:
            raise ArtifactOperationUnavailableError(
                "shared artifact-operation lease returned an invalid decision"
            ) from exc
        return ArtifactOperationAcquireResult(status, server_time_ms, expires_at_ms)

    async def renew(
        self,
        tenant_key_hash: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> ArtifactOperationRenewResult:
        _validate_ttl_seconds(ttl_seconds)
        _validate_lease_identity(tenant_key_hash, owner_token)
        try:
            async with asyncio.timeout(self.operation_timeout_seconds):
                raw = await self.client.eval(
                    _RENEW_SCRIPT,
                    2,
                    _REDIS_GLOBAL_KEY,
                    self._tenant_key(tenant_key_hash),
                    owner_token,
                    _global_member(tenant_key_hash, owner_token),
                    ttl_seconds * 1000,
                )
        except (RedisError, TimeoutError) as exc:
            raise ArtifactOperationUnavailableError(
                "shared artifact-operation lease dependency is unavailable"
            ) from exc
        try:
            if not isinstance(raw, list | tuple) or len(raw) != 3:
                raise ValueError
            renewed_value = int(raw[0])
            server_time_ms = int(raw[1])
            expires_at_ms = int(raw[2])
            if (
                renewed_value not in {0, 1}
                or server_time_ms < 0
                or (renewed_value == 1 and expires_at_ms != server_time_ms + ttl_seconds * 1000)
                or (renewed_value == 0 and expires_at_ms != 0)
            ):
                raise ValueError
        except (TypeError, ValueError, IndexError) as exc:
            raise ArtifactOperationUnavailableError(
                "shared artifact-operation lease returned an invalid renewal decision"
            ) from exc
        return ArtifactOperationRenewResult(
            renewed=renewed_value == 1,
            server_time_ms=server_time_ms,
            expires_at_ms=expires_at_ms,
        )

    async def release(self, tenant_key_hash: str, owner_token: str) -> bool:
        _validate_lease_identity(tenant_key_hash, owner_token)
        try:
            async with asyncio.timeout(self.operation_timeout_seconds):
                raw = await self.client.eval(
                    _RELEASE_SCRIPT,
                    2,
                    _REDIS_GLOBAL_KEY,
                    self._tenant_key(tenant_key_hash),
                    owner_token,
                    _global_member(tenant_key_hash, owner_token),
                )
        except (RedisError, TimeoutError) as exc:
            raise ArtifactOperationUnavailableError(
                "shared artifact-operation lease dependency is unavailable"
            ) from exc
        try:
            released = int(raw)
        except (TypeError, ValueError) as exc:
            raise ArtifactOperationUnavailableError(
                "shared artifact-operation lease returned an invalid release decision"
            ) from exc
        if released not in {0, 1}:
            raise ArtifactOperationUnavailableError(
                "shared artifact-operation lease returned an invalid release decision"
            )
        return released == 1


class InMemoryArtifactOperationStore:
    """Shareable deterministic lease state for development and unit tests."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = Lock()
        self._by_tenant: dict[str, tuple[str, int]] = {}
        self._global: dict[str, tuple[str, str, int]] = {}

    def _now_ms(self) -> int:
        current = self._clock()
        if isinstance(current, bool) or not isinstance(current, int | float):
            raise ArtifactOperationUnavailableError("in-memory lease clock is invalid")
        seconds = float(current)
        if not isfinite(seconds) or seconds < 0:
            raise ArtifactOperationUnavailableError("in-memory lease clock is invalid")
        return int(seconds * 1000)

    def _remove_stale(self, now_ms: int) -> None:
        for member, (tenant_key_hash, owner_token, expires_at_ms) in tuple(self._global.items()):
            if expires_at_ms > now_ms:
                continue
            self._global.pop(member, None)
            if self._by_tenant.get(tenant_key_hash) == (owner_token, expires_at_ms):
                self._by_tenant.pop(tenant_key_hash, None)
        for tenant_key_hash, (_owner_token, expires_at_ms) in tuple(self._by_tenant.items()):
            if expires_at_ms <= now_ms:
                self._by_tenant.pop(tenant_key_hash, None)

    async def acquire(
        self,
        tenant_key_hash: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> ArtifactOperationAcquireResult:
        _validate_ttl_seconds(ttl_seconds)
        _validate_lease_identity(tenant_key_hash, owner_token)
        with self._lock:
            now_ms = self._now_ms()
            self._remove_stale(now_ms)
            if tenant_key_hash in self._by_tenant:
                return ArtifactOperationAcquireResult(
                    ArtifactOperationAcquireStatus.TENANT_BUSY,
                    now_ms,
                    0,
                )
            if len(self._global) >= GLOBAL_ARTIFACT_OPERATION_LIMIT:
                return ArtifactOperationAcquireResult(
                    ArtifactOperationAcquireStatus.GLOBAL_CAPACITY,
                    now_ms,
                    0,
                )
            expires_at_ms = now_ms + ttl_seconds * 1000
            member = _global_member(tenant_key_hash, owner_token)
            self._by_tenant[tenant_key_hash] = (owner_token, expires_at_ms)
            self._global[member] = (tenant_key_hash, owner_token, expires_at_ms)
            return ArtifactOperationAcquireResult(
                ArtifactOperationAcquireStatus.ACQUIRED,
                now_ms,
                expires_at_ms,
            )

    async def release(self, tenant_key_hash: str, owner_token: str) -> bool:
        _validate_lease_identity(tenant_key_hash, owner_token)
        with self._lock:
            self._remove_stale(self._now_ms())
            current = self._by_tenant.get(tenant_key_hash)
            if current is None or current[0] != owner_token:
                return False
            self._by_tenant.pop(tenant_key_hash, None)
            self._global.pop(_global_member(tenant_key_hash, owner_token), None)
            return True

    async def renew(
        self,
        tenant_key_hash: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> ArtifactOperationRenewResult:
        _validate_ttl_seconds(ttl_seconds)
        _validate_lease_identity(tenant_key_hash, owner_token)
        with self._lock:
            now_ms = self._now_ms()
            self._remove_stale(now_ms)
            current = self._by_tenant.get(tenant_key_hash)
            if current is None or current[0] != owner_token:
                return ArtifactOperationRenewResult(False, now_ms, 0)
            old_expiry = current[1]
            expires_at_ms = now_ms + ttl_seconds * 1000
            self._by_tenant[tenant_key_hash] = (owner_token, expires_at_ms)
            member = _global_member(tenant_key_hash, owner_token)
            if self._global.get(member) != (tenant_key_hash, owner_token, old_expiry):
                raise ArtifactOperationUnavailableError(
                    "in-memory lease accounting is inconsistent"
                )
            self._global[member] = (tenant_key_hash, owner_token, expires_at_ms)
            return ArtifactOperationRenewResult(True, now_ms, expires_at_ms)


class ArtifactOperationLeaseManager:
    """Tenant-exclusive, globally bounded artifact-operation lease manager."""

    def __init__(
        self,
        store: ArtifactOperationLeaseStore,
        *,
        ttl_seconds: int = MIN_ARTIFACT_OPERATION_TTL_SECONDS,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        _validate_ttl_seconds(ttl_seconds)
        maximum_heartbeat_interval = min(float(ttl_seconds) / 3, 60.0)
        if heartbeat_interval_seconds is None:
            heartbeat_interval = maximum_heartbeat_interval
        elif (
            isinstance(heartbeat_interval_seconds, bool)
            or not isinstance(heartbeat_interval_seconds, int | float)
            or not isfinite(float(heartbeat_interval_seconds))
            or not 0 < float(heartbeat_interval_seconds) <= maximum_heartbeat_interval
        ):
            raise ValueError(
                "artifact operation heartbeat must be positive and no longer than TTL/3 or 60s"
            )
        else:
            heartbeat_interval = float(heartbeat_interval_seconds)
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.heartbeat_interval_seconds = heartbeat_interval

    async def acquire(self, tenant_id: str) -> ArtifactOperationLease:
        tenant_key_hash = _tenant_key_hash(tenant_id)
        owner_token = secrets.token_hex(32)
        result = await self.store.acquire(tenant_key_hash, owner_token, self.ttl_seconds)
        if result.status is ArtifactOperationAcquireStatus.TENANT_BUSY:
            raise ArtifactOperationBusyError()
        if result.status is ArtifactOperationAcquireStatus.GLOBAL_CAPACITY:
            raise ArtifactOperationCapacityError()
        if result.status is not ArtifactOperationAcquireStatus.ACQUIRED:
            raise ArtifactOperationUnavailableError(
                "artifact-operation lease returned an unknown decision"
            )
        return ArtifactOperationLease(tenant_key_hash, owner_token, result.expires_at_ms)

    async def release(self, lease: ArtifactOperationLease) -> bool:
        if not isinstance(lease, ArtifactOperationLease):
            raise ValueError("artifact operation lease is invalid")
        return await self.store.release(lease.tenant_key_hash, lease.owner_token)

    async def renew(self, lease: ArtifactOperationLease) -> ArtifactOperationLease:
        if not isinstance(lease, ArtifactOperationLease):
            raise ValueError("artifact operation lease is invalid")
        result = await self.store.renew(
            lease.tenant_key_hash,
            lease.owner_token,
            self.ttl_seconds,
        )
        if not result.renewed:
            raise ArtifactOperationOwnershipLostError(
                "artifact operation no longer owns its tenant lease"
            )
        return ArtifactOperationLease(
            lease.tenant_key_hash,
            lease.owner_token,
            result.expires_at_ms,
        )

    @asynccontextmanager
    async def lease(self, tenant_id: str) -> AsyncIterator[ArtifactOperationLease]:
        """Hold and renew a lease; Redis release failures fall back to its TTL.

        Heartbeat failures cancel the operation task and are translated back to
        the concrete availability/ownership error.  A release dependency outage
        is logged rather than raised from dependency teardown after a response;
        the bounded Redis TTL remains the fail-safe cleanup path.
        """

        current = await self.acquire(tenant_id)
        owner_task = asyncio.current_task()
        if owner_task is None:  # pragma: no cover - an async context always has a task
            await self.release(current)
            raise RuntimeError("artifact operation lease requires an asyncio task")
        stop_heartbeat = asyncio.Event()
        heartbeat_failures: list[Exception] = []

        async def maintain_lease() -> None:
            while True:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop_heartbeat.wait(),
                        timeout=self.heartbeat_interval_seconds,
                    )
                if stop_heartbeat.is_set():
                    return
                try:
                    await self.renew(current)
                except (
                    ArtifactOperationUnavailableError,
                    ArtifactOperationOwnershipLostError,
                ) as exc:
                    heartbeat_failures.append(exc)
                    owner_task.cancel("artifact operation lease heartbeat failed")
                    return
                except Exception:
                    heartbeat_failures.append(
                        ArtifactOperationUnavailableError(
                            "artifact operation lease heartbeat failed"
                        )
                    )
                    owner_task.cancel("artifact operation lease heartbeat failed")
                    return

        heartbeat = asyncio.create_task(
            maintain_lease(),
            name="artifact-operation-lease-heartbeat",
        )
        operation_failed = False
        try:
            try:
                yield current
            except asyncio.CancelledError as exc:
                operation_failed = True
                if heartbeat_failures:
                    owner_task.uncancel()
                    raise heartbeat_failures[0] from exc
                raise
            except BaseException:
                operation_failed = True
                raise
        finally:
            async def cleanup_lease() -> None:
                # Release lives in a separate task so repeated cancellation of the
                # request task cannot strand a distributed lease until its TTL.
                stop_heartbeat.set()
                heartbeat_cancelled: asyncio.CancelledError | None = None
                try:
                    await heartbeat
                except asyncio.CancelledError as exc:
                    if not heartbeat_failures:
                        heartbeat_cancelled = exc
                try:
                    released = await self.release(current)
                except ArtifactOperationUnavailableError:
                    logger.warning(
                        "artifact_operation_release_unavailable tenant_key_hash=%s",
                        current.tenant_key_hash,
                    )
                else:
                    if not released and not operation_failed and not heartbeat_failures:
                        raise ArtifactOperationOwnershipLostError(
                            "artifact operation lease ownership was lost before release"
                        )
                    if not released:
                        logger.warning(
                            "artifact_operation_release_not_owned tenant_key_hash=%s",
                            current.tenant_key_hash,
                        )
                if heartbeat_cancelled is not None:
                    raise heartbeat_cancelled
                if heartbeat_failures and not operation_failed:
                    raise heartbeat_failures[0]

            cleanup = asyncio.create_task(
                cleanup_lease(),
                name="artifact-operation-lease-cleanup",
            )
            cleanup_cancellations = 0
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cleanup_cancellations += 1
                    owner_task.uncancel()
            cleanup.result()
            if cleanup_cancellations:
                for _ in range(cleanup_cancellations):
                    owner_task.cancel()
                raise asyncio.CancelledError
