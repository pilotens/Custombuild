from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pytest
from app import artifact_operations
from redis.exceptions import RedisError


class RecordingRedisClient:
    def __init__(self, *responses: object) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, int, tuple[object, ...]]] = []

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        self.calls.append((script, numkeys, keys_and_args))
        return self.responses.popleft()


class FailingRedisClient:
    async def eval(self, _script: str, _numkeys: int, *_args: object) -> object:
        raise RedisError("private Redis failure")


class HangingRedisClient:
    async def eval(self, _script: str, _numkeys: int, *_args: object) -> object:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class RenewUnavailableStore(artifact_operations.InMemoryArtifactOperationStore):
    def __init__(self) -> None:
        super().__init__()
        self.renew_calls = 0

    async def renew(
        self,
        tenant_key_hash: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> artifact_operations.ArtifactOperationRenewResult:
        self.renew_calls += 1
        raise artifact_operations.ArtifactOperationUnavailableError("renew unavailable")


class ReleaseUnavailableStore(artifact_operations.InMemoryArtifactOperationStore):
    async def release(self, tenant_key_hash: str, owner_token: str) -> bool:
        raise artifact_operations.ArtifactOperationUnavailableError("release unavailable")


class BlockingReleaseStore(artifact_operations.InMemoryArtifactOperationStore):
    def __init__(self) -> None:
        super().__init__()
        self.release_started = asyncio.Event()
        self.allow_release = asyncio.Event()

    async def release(self, tenant_key_hash: str, owner_token: str) -> bool:
        self.release_started.set()
        await self.allow_release.wait()
        return await super().release(tenant_key_hash, owner_token)


def test_redis_store_configures_strict_socket_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = RecordingRedisClient()

    def from_url(redis_url: str, **kwargs: object) -> RecordingRedisClient:
        captured["redis_url"] = redis_url
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(artifact_operations.Redis, "from_url", from_url)

    store = artifact_operations.RedisArtifactOperationStore(
        "redis://redis:6379/0",
        operation_timeout_seconds=1.5,
    )

    assert store.client is sentinel
    assert captured == {
        "redis_url": "redis://redis:6379/0",
        "decode_responses": True,
        "socket_connect_timeout": 1.5,
        "socket_timeout": 1.5,
        "retry_on_timeout": False,
    }


@pytest.mark.parametrize("ttl_seconds", (True, 899, 900.0, 86_401))
def test_manager_rejects_invalid_or_unsafe_lease_ttl(ttl_seconds: object) -> None:
    with pytest.raises(ValueError, match="TTL"):
        artifact_operations.ArtifactOperationLeaseManager(
            artifact_operations.InMemoryArtifactOperationStore(),
            ttl_seconds=ttl_seconds,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("heartbeat_seconds", (True, 0, -1, 60.01, float("inf")))
def test_manager_rejects_invalid_heartbeat_interval(heartbeat_seconds: object) -> None:
    with pytest.raises(ValueError, match="heartbeat"):
        artifact_operations.ArtifactOperationLeaseManager(
            artifact_operations.InMemoryArtifactOperationStore(),
            heartbeat_interval_seconds=heartbeat_seconds,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("timeout_seconds", (True, 0, -1, 2.01, float("inf")))
def test_redis_store_rejects_unbounded_operation_timeout(timeout_seconds: object) -> None:
    with pytest.raises(ValueError, match="timeout"):
        artifact_operations.RedisArtifactOperationStore(
            "redis://redis:6379/0",
            operation_timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
            client=RecordingRedisClient(),
        )


def test_redis_acquire_is_one_atomic_server_timed_hashed_tenant_decision() -> None:
    server_now_ms = 2_000_000_000_000
    expires_at_ms = server_now_ms + 900_000
    client = RecordingRedisClient([1, str(server_now_ms), str(expires_at_ms)])
    store = artifact_operations.RedisArtifactOperationStore(
        "redis://unused",
        client=client,
    )
    manager = artifact_operations.ArtifactOperationLeaseManager(store)
    raw_tenant = "tenant/customer-sensitive-id"

    lease = asyncio.run(manager.acquire(raw_tenant))

    assert lease.expires_at_ms == expires_at_ms
    assert lease.tenant_key_hash == hashlib.sha256(raw_tenant.encode()).hexdigest()
    assert raw_tenant not in repr(client.calls)
    assert len(client.calls) == 1
    script, numkeys, arguments = client.calls[0]
    assert numkeys == 2
    assert "redis.call('TIME')" in script
    assert "ZREMRANGEBYSCORE" in script
    assert "ZCARD" in script
    assert "'PX', ttl_ms, 'NX'" in script
    assert "ZADD" in script
    assert arguments[-2:] == (900_000, 8)
    assert arguments[1].endswith(lease.tenant_key_hash)
    assert lease.owner_token in arguments
    assert f"{lease.tenant_key_hash}:{lease.owner_token}" in arguments


def test_redis_release_is_one_atomic_owner_compare_delete() -> None:
    client = RecordingRedisClient(1)
    store = artifact_operations.RedisArtifactOperationStore(
        "redis://unused",
        client=client,
    )
    tenant_hash = "a" * 64
    owner_token = "b" * 64

    released = asyncio.run(store.release(tenant_hash, owner_token))

    assert released is True
    assert len(client.calls) == 1
    script, numkeys, arguments = client.calls[0]
    assert numkeys == 2
    assert "redis.call('TIME')" in script
    assert "ZREMRANGEBYSCORE" in script
    assert "redis.call('GET', tenant_key) ~= owner_token" in script
    assert "redis.call('DEL', tenant_key)" in script
    assert "redis.call('ZREM', global_key, global_member)" in script
    assert arguments[-2:] == (owner_token, f"{tenant_hash}:{owner_token}")


def test_redis_renew_is_one_atomic_server_timed_owner_compare_extension() -> None:
    now_ms = 2_000_000_000_000
    expires_at_ms = now_ms + 900_000
    client = RecordingRedisClient([1, str(now_ms), str(expires_at_ms)])
    store = artifact_operations.RedisArtifactOperationStore(
        "redis://unused",
        client=client,
    )
    tenant_hash = "a" * 64
    owner_token = "b" * 64

    renewed = asyncio.run(store.renew(tenant_hash, owner_token, 900))

    assert renewed == artifact_operations.ArtifactOperationRenewResult(
        True,
        now_ms,
        expires_at_ms,
    )
    assert len(client.calls) == 1
    script, numkeys, arguments = client.calls[0]
    assert numkeys == 2
    assert "redis.call('TIME')" in script
    assert "ZREMRANGEBYSCORE" in script
    assert "redis.call('GET', tenant_key) ~= owner_token" in script
    assert "redis.call('PEXPIRE', tenant_key, ttl_ms)" in script
    assert "redis.call('ZADD', global_key, expires_at_ms, global_member)" in script
    assert arguments[-3:] == (owner_token, f"{tenant_hash}:{owner_token}", 900_000)


def test_manager_renew_reports_owner_change() -> None:
    client = RecordingRedisClient([0, "2000000000000", "0"])
    store = artifact_operations.RedisArtifactOperationStore(
        "redis://unused",
        client=client,
    )
    manager = artifact_operations.ArtifactOperationLeaseManager(store)
    lease = artifact_operations.ArtifactOperationLease("a" * 64, "b" * 64, 1)

    with pytest.raises(artifact_operations.ArtifactOperationOwnershipLostError):
        asyncio.run(manager.renew(lease))


@pytest.mark.parametrize(
    ("decision", "error_type", "status"),
    (
        (
            [2, "2000000000000", "0"],
            artifact_operations.ArtifactOperationBusyError,
            artifact_operations.ArtifactOperationAcquireStatus.TENANT_BUSY,
        ),
        (
            [3, "2000000000000", "0"],
            artifact_operations.ArtifactOperationCapacityError,
            artifact_operations.ArtifactOperationAcquireStatus.GLOBAL_CAPACITY,
        ),
    ),
)
def test_manager_distinguishes_tenant_busy_from_global_capacity(
    decision: list[str | int],
    error_type: type[artifact_operations.ArtifactOperationRejectedError],
    status: artifact_operations.ArtifactOperationAcquireStatus,
) -> None:
    store = artifact_operations.RedisArtifactOperationStore(
        "redis://unused",
        client=RecordingRedisClient(decision),
    )
    manager = artifact_operations.ArtifactOperationLeaseManager(store)

    with pytest.raises(error_type) as exc_info:
        asyncio.run(manager.acquire("tenant-a"))

    assert exc_info.value.status is status


@pytest.mark.parametrize("client", (FailingRedisClient(), HangingRedisClient()))
def test_redis_dependency_failure_or_timeout_fails_closed(client: object) -> None:
    store = artifact_operations.RedisArtifactOperationStore(
        "redis://unused",
        operation_timeout_seconds=0.01,
        client=client,  # type: ignore[arg-type]
    )
    manager = artifact_operations.ArtifactOperationLeaseManager(store)
    started = time.monotonic()

    with pytest.raises(artifact_operations.ArtifactOperationUnavailableError):
        asyncio.run(manager.acquire("tenant-a"))

    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("decision", (None, [], [1, "bad", "response"], [99, "1", "2"]))
def test_redis_invalid_decision_fails_closed(decision: object) -> None:
    store = artifact_operations.RedisArtifactOperationStore(
        "redis://unused",
        client=RecordingRedisClient(decision),
    )

    with pytest.raises(artifact_operations.ArtifactOperationUnavailableError):
        asyncio.run(store.acquire("a" * 64, "b" * 64, 900))


def test_two_managers_share_tenant_exclusivity_and_release() -> None:
    async def exercise() -> None:
        shared = artifact_operations.InMemoryArtifactOperationStore()
        first = artifact_operations.ArtifactOperationLeaseManager(shared)
        second = artifact_operations.ArtifactOperationLeaseManager(shared)
        lease = await first.acquire("tenant-a")

        with pytest.raises(artifact_operations.ArtifactOperationBusyError) as exc_info:
            await second.acquire("tenant-a")
        assert (
            exc_info.value.status is artifact_operations.ArtifactOperationAcquireStatus.TENANT_BUSY
        )

        assert await first.release(lease) is True
        replacement = await second.acquire("tenant-a")
        assert replacement.owner_token != lease.owner_token
        assert await second.release(replacement) is True

    asyncio.run(exercise())


def test_concurrent_replica_acquires_produce_one_tenant_owner_and_eight_global_owners() -> None:
    async def exercise() -> None:
        shared_tenant_store = artifact_operations.InMemoryArtifactOperationStore()
        tenant_managers = [
            artifact_operations.ArtifactOperationLeaseManager(shared_tenant_store)
            for _ in range(12)
        ]
        tenant_results = await asyncio.gather(
            *(manager.acquire("same-tenant") for manager in tenant_managers),
            return_exceptions=True,
        )
        tenant_leases = [
            result
            for result in tenant_results
            if isinstance(result, artifact_operations.ArtifactOperationLease)
        ]
        tenant_rejections = [
            result
            for result in tenant_results
            if isinstance(result, artifact_operations.ArtifactOperationBusyError)
        ]
        assert len(tenant_leases) == 1
        assert len(tenant_rejections) == 11
        assert await tenant_managers[0].release(tenant_leases[0]) is True

        shared_global_store = artifact_operations.InMemoryArtifactOperationStore()
        global_managers = [
            artifact_operations.ArtifactOperationLeaseManager(shared_global_store)
            for _ in range(16)
        ]
        global_results = await asyncio.gather(
            *(manager.acquire(f"tenant-{index}") for index, manager in enumerate(global_managers)),
            return_exceptions=True,
        )
        global_leases = [
            result
            for result in global_results
            if isinstance(result, artifact_operations.ArtifactOperationLease)
        ]
        capacity_rejections = [
            result
            for result in global_results
            if isinstance(result, artifact_operations.ArtifactOperationCapacityError)
        ]
        assert len(global_leases) == artifact_operations.GLOBAL_ARTIFACT_OPERATION_LIMIT
        assert len(capacity_rejections) == 8
        for lease in global_leases:
            assert await global_managers[0].release(lease) is True

    asyncio.run(exercise())


def test_in_memory_store_is_shared_safely_across_event_loop_threads() -> None:
    shared = artifact_operations.InMemoryArtifactOperationStore()
    managers = (
        artifact_operations.ArtifactOperationLeaseManager(shared),
        artifact_operations.ArtifactOperationLeaseManager(shared),
    )

    def acquire(index: int) -> object:
        try:
            return asyncio.run(managers[index].acquire("same-tenant"))
        except artifact_operations.ArtifactOperationBusyError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, range(2)))

    leases = [
        result
        for result in results
        if isinstance(result, artifact_operations.ArtifactOperationLease)
    ]
    rejections = [
        result
        for result in results
        if isinstance(result, artifact_operations.ArtifactOperationBusyError)
    ]
    assert len(leases) == len(rejections) == 1
    assert asyncio.run(managers[0].release(leases[0])) is True


def test_two_managers_share_the_exact_global_capacity_of_eight() -> None:
    async def exercise() -> None:
        shared = artifact_operations.InMemoryArtifactOperationStore()
        managers = (
            artifact_operations.ArtifactOperationLeaseManager(shared),
            artifact_operations.ArtifactOperationLeaseManager(shared),
        )
        leases = [
            await managers[index % 2].acquire(f"tenant-{index}")
            for index in range(artifact_operations.GLOBAL_ARTIFACT_OPERATION_LIMIT)
        ]

        with pytest.raises(artifact_operations.ArtifactOperationCapacityError) as exc_info:
            await managers[1].acquire("tenant-over-capacity")
        assert (
            exc_info.value.status
            is artifact_operations.ArtifactOperationAcquireStatus.GLOBAL_CAPACITY
        )

        assert await managers[0].release(leases[3]) is True
        replacement = await managers[1].acquire("tenant-over-capacity")
        assert await managers[1].release(replacement) is True
        for index, lease in enumerate(leases):
            if index != 3:
                assert await managers[index % 2].release(lease) is True

    asyncio.run(exercise())


def test_expired_owner_cannot_delete_replacement_and_stale_capacity_is_reclaimed() -> None:
    async def exercise() -> None:
        clock = [100.0]
        shared = artifact_operations.InMemoryArtifactOperationStore(clock=lambda: clock[0])
        first = artifact_operations.ArtifactOperationLeaseManager(shared)
        second = artifact_operations.ArtifactOperationLeaseManager(shared)
        old = await first.acquire("tenant-a")
        for index in range(1, artifact_operations.GLOBAL_ARTIFACT_OPERATION_LIMIT):
            await first.acquire(f"tenant-{index}")

        clock[0] += 900.0
        replacement = await second.acquire("tenant-a")
        assert await first.release(old) is False
        with pytest.raises(artifact_operations.ArtifactOperationBusyError):
            await first.acquire("tenant-a")
        assert await second.release(replacement) is True

    asyncio.run(exercise())


def test_heartbeat_extends_shared_lease_past_its_original_expiry_and_stops_on_exit() -> None:
    async def exercise() -> None:
        clock = [100.0]
        shared = artifact_operations.InMemoryArtifactOperationStore(clock=lambda: clock[0])
        first = artifact_operations.ArtifactOperationLeaseManager(
            shared,
            heartbeat_interval_seconds=0.01,
        )
        second = artifact_operations.ArtifactOperationLeaseManager(shared)

        async with first.lease("tenant-a"):
            clock[0] = 999.0
            await asyncio.sleep(0.03)
            clock[0] = 1_001.0
            with pytest.raises(artifact_operations.ArtifactOperationBusyError):
                await second.acquire("tenant-a")

        replacement = await second.acquire("tenant-a")
        assert await second.release(replacement) is True

    asyncio.run(exercise())


def test_heartbeat_owner_change_cancels_and_surfaces_to_operation_caller() -> None:
    async def exercise() -> None:
        clock = [100.0]
        shared = artifact_operations.InMemoryArtifactOperationStore(clock=lambda: clock[0])
        first = artifact_operations.ArtifactOperationLeaseManager(
            shared,
            heartbeat_interval_seconds=0.01,
        )
        second = artifact_operations.ArtifactOperationLeaseManager(shared)
        replacement: artifact_operations.ArtifactOperationLease | None = None

        with pytest.raises(artifact_operations.ArtifactOperationOwnershipLostError):
            async with first.lease("tenant-a"):
                clock[0] = 1_000.0
                replacement = await second.acquire("tenant-a")
                await asyncio.sleep(1)

        assert replacement is not None
        assert await second.release(replacement) is True

    asyncio.run(exercise())


def test_heartbeat_dependency_failure_is_exposed_promptly_and_task_is_joined() -> None:
    async def exercise() -> None:
        store = RenewUnavailableStore()
        manager = artifact_operations.ArtifactOperationLeaseManager(
            store,
            heartbeat_interval_seconds=0.01,
        )
        started = time.monotonic()

        with pytest.raises(artifact_operations.ArtifactOperationUnavailableError):
            async with manager.lease("tenant-a"):
                await asyncio.sleep(1)

        renew_calls = store.renew_calls
        await asyncio.sleep(0.03)
        assert store.renew_calls == renew_calls == 1
        assert time.monotonic() - started < 0.5

    asyncio.run(exercise())


def test_context_logs_release_outage_and_relies_on_bounded_ttl(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        manager = artifact_operations.ArtifactOperationLeaseManager(
            ReleaseUnavailableStore(),
            heartbeat_interval_seconds=0.1,
        )
        async with manager.lease("tenant-a"):
            pass

    with caplog.at_level(logging.WARNING, logger=artifact_operations.__name__):
        asyncio.run(exercise())

    assert "artifact_operation_release_unavailable" in caplog.text


def test_async_lease_context_releases_after_operation_failure() -> None:
    async def exercise() -> None:
        shared = artifact_operations.InMemoryArtifactOperationStore()
        first = artifact_operations.ArtifactOperationLeaseManager(shared)
        second = artifact_operations.ArtifactOperationLeaseManager(shared)

        with pytest.raises(RuntimeError, match="operation failed"):
            async with first.lease("tenant-a"):
                raise RuntimeError("operation failed")

        replacement = await second.acquire("tenant-a")
        assert await second.release(replacement) is True

    asyncio.run(exercise())


def test_repeated_task_cancellation_cannot_interrupt_owned_lease_release() -> None:
    async def exercise() -> None:
        store = BlockingReleaseStore()
        manager = artifact_operations.ArtifactOperationLeaseManager(store)
        operation_started = asyncio.Event()

        async def operation() -> None:
            async with manager.lease("tenant-a"):
                operation_started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(operation())
        await operation_started.wait()
        task.cancel()
        await store.release_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        store.allow_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        replacement = await manager.acquire("tenant-a")
        store.allow_release.set()
        assert await manager.release(replacement) is True

    asyncio.run(exercise())


@pytest.mark.parametrize("tenant_id", ("", " tenant", "tenant ", "x" * 513, None, True))
def test_manager_rejects_invalid_tenant_identity(tenant_id: object) -> None:
    manager = artifact_operations.ArtifactOperationLeaseManager(
        artifact_operations.InMemoryArtifactOperationStore()
    )

    with pytest.raises(ValueError, match="tenant identity"):
        asyncio.run(manager.acquire(tenant_id))  # type: ignore[arg-type]
