from __future__ import annotations

import asyncio
import time

import pytest
from app import security
from app.security import (
    RATE_LIMIT_STORE_TIMEOUT_SECONDS,
    RateLimitMiddleware,
    RedisRateLimitStore,
)
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


async def ok(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def unauthorized(_request: Request) -> JSONResponse:
    return JSONResponse({"detail": "Invalid authentication token"}, status_code=401)


class CountingStore:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.keys: list[str] = []

    async def increment(self, key: str, _window_seconds: int) -> int:
        self.keys.append(key)
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


class FailingStore:
    async def increment(self, _key: str, _window_seconds: int) -> int:
        raise RedisConnectionError("offline")


class HangingStore:
    def __init__(self) -> None:
        self.calls = 0

    async def increment(self, _key: str, _window_seconds: int) -> int:
        self.calls += 1
        await asyncio.Event().wait()
        return 1


def test_local_rate_limit_has_retry_after_and_exempts_health() -> None:
    app = Starlette(routes=[Route("/resource", ok), Route("/health", ok)])
    app.add_middleware(RateLimitMiddleware, requests=2, window_seconds=30)
    with TestClient(app) as client:
        assert client.get("/resource").status_code == 200
        assert client.get("/resource").status_code == 200
        blocked = client.get("/resource")
        assert client.get("/health").status_code == 200

    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "30"


def test_production_uses_shared_source_counter_without_storing_raw_identity() -> None:
    store = CountingStore()
    app = Starlette(routes=[Route("/resource", ok)])
    app.add_middleware(
        RateLimitMiddleware,
        requests=1,
        distributed_required=True,
        store=store,
        wall_clock=lambda: 120.0,
    )
    headers = {"Authorization": "Bearer very-secret-token"}
    with TestClient(app) as client:
        assert client.get("/resource", headers=headers).status_code == 200
        assert client.get("/resource", headers=headers).status_code == 429

    assert len(set(store.keys)) == 1
    assert "very-secret-token" not in store.keys[0]
    assert "testclient" not in store.keys[0]
    assert store.keys[0].startswith("custombuild:rate-limit:2:source-ip:")


def test_rotating_unverified_bearer_values_cannot_escape_the_source_limit() -> None:
    store = CountingStore()
    app = Starlette(routes=[Route("/resource", unauthorized)])
    app.add_middleware(
        RateLimitMiddleware,
        requests=2,
        distributed_required=True,
        store=store,
        wall_clock=lambda: 120.0,
    )

    with TestClient(app) as client:
        responses = [
            client.get(
                "/resource",
                headers={"Authorization": f"Bearer attacker-token-{index}"},
            )
            for index in range(3)
        ]

    assert [response.status_code for response in responses] == [401, 401, 429]
    assert len(set(store.keys)) == 1
    assert all("attacker-token" not in key for key in store.keys)


def test_trusted_single_hop_proxy_separates_real_client_buckets() -> None:
    store = CountingStore()
    app = Starlette(routes=[Route("/resource", ok)])
    app.add_middleware(
        RateLimitMiddleware,
        requests=1,
        distributed_required=True,
        store=store,
        trusted_proxy_cidrs=["10.0.0.0/24"],
        wall_clock=lambda: 120.0,
    )

    with TestClient(app, client=("10.0.0.10", 50000)) as client:
        first = client.get("/resource", headers={"X-Forwarded-For": "198.51.100.10"})
        second = client.get("/resource", headers={"X-Forwarded-For": "198.51.100.11"})
        blocked = client.get("/resource", headers={"X-Forwarded-For": "198.51.100.10"})

    assert [first.status_code, second.status_code, blocked.status_code] == [200, 200, 429]
    assert len(set(store.keys)) == 2
    assert all("198.51.100" not in key for key in store.keys)


def test_untrusted_peer_cannot_spoof_forwarding_to_rotate_buckets() -> None:
    store = CountingStore()
    app = Starlette(routes=[Route("/resource", ok)])
    app.add_middleware(
        RateLimitMiddleware,
        requests=1,
        distributed_required=True,
        store=store,
        trusted_proxy_cidrs=["10.0.0.0/24"],
        wall_clock=lambda: 120.0,
    )

    with TestClient(app, client=("203.0.113.8", 50000)) as client:
        first = client.get("/resource", headers={"X-Forwarded-For": "198.51.100.10"})
        blocked = client.get("/resource", headers={"X-Forwarded-For": "198.51.100.11"})

    assert [first.status_code, blocked.status_code] == [200, 429]
    assert len(set(store.keys)) == 1


@pytest.mark.parametrize(
    "forwarded",
    ["198.51.100.10, 198.51.100.11", "198.51.100.10:1234", "not-an-ip", ""],
)
def test_ambiguous_or_malformed_trusted_proxy_header_collapses_to_peer_bucket(
    forwarded: str,
) -> None:
    store = CountingStore()
    app = Starlette(routes=[Route("/resource", ok)])
    app.add_middleware(
        RateLimitMiddleware,
        requests=1,
        distributed_required=True,
        store=store,
        trusted_proxy_cidrs=["10.0.0.0/24"],
        wall_clock=lambda: 120.0,
    )

    with TestClient(app, client=("10.0.0.10", 50000)) as client:
        first = client.get("/resource", headers={"X-Forwarded-For": forwarded})
        blocked = client.get("/resource", headers={"X-Forwarded-For": "also-invalid"})

    assert [first.status_code, blocked.status_code] == [200, 429]
    assert len(set(store.keys)) == 1


def test_production_fails_closed_when_shared_counter_is_unavailable() -> None:
    app = Starlette(routes=[Route("/resource", ok)])
    app.add_middleware(
        RateLimitMiddleware,
        distributed_required=True,
        store=FailingStore(),
    )
    with TestClient(app) as client:
        response = client.get("/resource")

    assert response.status_code == 503
    assert response.json()["detail"] == "Shared rate-limit dependency is unavailable"


def test_production_bounds_a_stalled_shared_counter_and_remains_serviceable() -> None:
    store = HangingStore()
    app = Starlette(routes=[Route("/resource", ok)])
    app.add_middleware(
        RateLimitMiddleware,
        distributed_required=True,
        store=store,
        store_timeout_seconds=0.02,
    )

    started = time.monotonic()
    with TestClient(app) as client:
        first = client.get("/resource")
        second = client.get("/resource")
    elapsed = time.monotonic() - started

    assert [first.status_code, second.status_code] == [503, 503]
    assert first.json() == {"detail": "Shared rate-limit dependency is unavailable"}
    assert store.calls == 2
    assert elapsed < 0.5


def test_redis_store_configures_bounded_connect_and_read_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_from_url(_url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(security.Redis, "from_url", fake_from_url)
    RedisRateLimitStore("redis://:secret@redis:6379/0")

    assert captured["socket_connect_timeout"] == RATE_LIMIT_STORE_TIMEOUT_SECONDS
    assert captured["socket_timeout"] == RATE_LIMIT_STORE_TIMEOUT_SECONDS
    assert captured["retry_on_timeout"] is False
