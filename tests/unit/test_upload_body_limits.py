from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from app import security, storage
from app.main import app, settings
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _scope(*headers: tuple[bytes, bytes]) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/projects/example/evidence",
        "raw_path": b"/v1/projects/example/evidence",
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }


def _run_request(
    middleware: ASGIApp,
    scope: Scope,
    messages: list[Message],
) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return sent


def _status(messages: list[Message]) -> int:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return int(start["status"])


def _response_headers(messages: list[Message]) -> dict[bytes, bytes]:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return {name.lower(): value for name, value in start["headers"]}


def _preflight_scope(*headers: tuple[bytes, bytes], client: str = "127.0.0.1") -> Scope:
    scope = _scope(
        (b"origin", settings.allowed_origins[0].encode("ascii")),
        (b"access-control-request-method", b"POST"),
        *headers,
    )
    scope["method"] = "OPTIONS"
    scope["client"] = (client, 12345)
    return scope


def _assert_browser_error_headers(messages: list[Message]) -> None:
    headers = _response_headers(messages)
    assert headers[b"access-control-allow-origin"] == settings.allowed_origins[0].encode("ascii")
    assert b"X-Request-ID" in headers[b"access-control-expose-headers"]
    assert b"X-Content-Type-Options" in headers[b"access-control-expose-headers"]
    assert b"Origin" in headers[b"vary"]
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-request-id"]


def _consumer(calls: list[bytes]) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    async def app(_scope: Scope, receive: Receive, send: Send) -> None:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        calls.append(bytes(body))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


def test_security_headers_preserve_one_stronger_cache_control_field() -> None:
    async def cached_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (
                        b"cache-control",
                        b"private, no-store, no-transform, max-age=0",
                    )
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    sent = _run_request(
        security.SecurityHeadersMiddleware(cached_app),
        _scope(),
        [{"type": "http.request", "body": b"", "more_body": False}],
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    cache_headers = [value for name, value in start["headers"] if name.lower() == b"cache-control"]

    assert cache_headers == [b"private, no-store, no-transform, max-age=0"]


def test_request_limit_accepts_only_an_exact_bounded_declared_body() -> None:
    calls: list[bytes] = []
    middleware = security.RequestBodyLimitMiddleware(_consumer(calls), max_bytes=8)
    messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]

    sent = _run_request(
        middleware,
        _scope(
            (b"content-type", b"multipart/form-data; boundary=exact"),
            (b"content-length", b"6"),
        ),
        messages,
    )

    assert _status(sent) == 204
    assert calls == [b"abcdef"]


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    (
        (((b"content-type", b"multipart/form-data; boundary=x"),), 411),
        (
            (
                (b"content-type", b"multipart/form-data; boundary=x"),
                (b"content-length", b"9"),
            ),
            413,
        ),
        (
            (
                (b"content-type", b"application/json"),
                (b"content-length", b"9"),
            ),
            413,
        ),
        (
            (
                (b"content-type", b"multipart/form-data; boundary=x"),
                (b"content-length", b"06"),
            ),
            400,
        ),
        (
            (
                (b"content-type", b"application/json"),
                (b"content-length", b"6\xff"),
            ),
            400,
        ),
        (
            (
                (b"content-type", b"application/json"),
                (b"content-length", b"9" * 5_000),
            ),
            400,
        ),
        (
            (
                (b"content-type", b"multipart/form-data; boundary=x"),
                (b"content-length", b"6"),
                (b"transfer-encoding", b"chunked"),
            ),
            400,
        ),
    ),
)
def test_request_limit_rejects_missing_oversize_or_ambiguous_lengths_before_reading(
    headers: tuple[tuple[bytes, bytes], ...],
    expected_status: int,
) -> None:
    calls: list[bytes] = []
    middleware = security.RequestBodyLimitMiddleware(_consumer(calls), max_bytes=8)

    sent = _run_request(
        middleware,
        _scope(*headers),
        [{"type": "http.request", "body": b"unread", "more_body": False}],
    )

    assert _status(sent) == expected_status
    assert calls == []


def test_request_limit_rejects_actual_bytes_beyond_the_declared_length() -> None:
    calls: list[bytes] = []
    middleware = security.RequestBodyLimitMiddleware(_consumer(calls), max_bytes=8)

    sent = _run_request(
        middleware,
        _scope(
            (b"content-type", b"multipart/form-data; boundary=x"),
            (b"content-length", b"4"),
        ),
        [{"type": "http.request", "body": b"forged", "more_body": False}],
    )

    assert _status(sent) == 413
    assert calls == []


def test_request_limit_bounds_chunked_json_without_a_declared_length() -> None:
    calls: list[bytes] = []
    middleware = security.RequestBodyLimitMiddleware(_consumer(calls), max_bytes=8)

    sent = _run_request(
        middleware,
        _scope(
            (b"content-type", b"application/json"),
            (b"transfer-encoding", b"chunked"),
        ),
        [
            {"type": "http.request", "body": b'{"a"', "more_body": True},
            {"type": "http.request", "body": b":1}", "more_body": False},
        ],
    )

    assert _status(sent) == 204
    assert calls == [b'{"a":1}']


def test_request_limit_rejects_a_get_body_before_downstream_processing() -> None:
    calls: list[bytes] = []
    middleware = security.RequestBodyLimitMiddleware(_consumer(calls), max_bytes=8)
    scope = _scope((b"content-length", b"1"))
    scope["method"] = "GET"

    sent = _run_request(
        middleware,
        scope,
        [{"type": "http.request", "body": b"x", "more_body": False}],
    )

    assert _status(sent) == 400
    assert calls == []


@pytest.mark.parametrize("method", ("GET", "HEAD", "OPTIONS"))
@pytest.mark.parametrize(
    "headers",
    (
        ((b"content-length", b""),),
        ((b"content-length", b"0"), (b"content-length", b"0")),
    ),
)
def test_body_forbidden_methods_reject_ambiguous_zero_length_framing(
    method: str,
    headers: tuple[tuple[bytes, bytes], ...],
) -> None:
    calls: list[bytes] = []
    middleware = security.RequestBodyLimitMiddleware(_consumer(calls), max_bytes=8)
    scope = _scope(*headers)
    scope["method"] = method

    sent = _run_request(
        middleware,
        scope,
        [{"type": "http.request", "body": b"", "more_body": False}],
    )

    assert _status(sent) == 400
    assert calls == []


def test_request_limit_shares_capacity_with_verified_download_spools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bytes] = []
    budget = storage._TransientByteBudget(10)
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)
    middleware = security.RequestBodyLimitMiddleware(_consumer(calls), max_bytes=8)

    with storage.reserve_transient_bytes(6):
        sent = _run_request(
            middleware,
            _scope(
                (b"content-type", b"multipart/form-data; boundary=x"),
                (b"content-length", b"5"),
            ),
            [{"type": "http.request", "body": b"12345", "more_body": False}],
        )
        assert budget.reserved_bytes == 6

    assert _status(sent) == 503
    assert calls == []
    assert budget.reserved_bytes == 0


def test_request_body_idle_timeout_releases_the_shared_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = storage._TransientByteBudget(10)
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)
    calls: list[bytes] = []
    middleware = security.RequestBodyLimitMiddleware(
        _consumer(calls),
        max_bytes=8,
        idle_timeout_seconds=0.01,
        total_timeout_seconds=0.02,
    )
    sent: list[Message] = []
    receive_calls = 0

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {"type": "http.request", "body": b"x", "more_body": True}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    scope = _scope(
        (b"content-type", b"application/json"),
        (b"content-length", b"8"),
    )
    asyncio.run(middleware(scope, receive, send))

    assert _status(sent) == 408
    assert calls == []
    assert budget.reserved_bytes == 0


def test_bodyless_route_cannot_succeed_before_the_declared_body_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = storage._TransientByteBudget(10)
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)
    downstream_called = False
    sent: list[Message] = []
    receive_calls = 0

    async def bodyless_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        nonlocal downstream_called
        downstream_called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {"type": "http.request", "body": b"x", "more_body": True}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = security.RequestBodyLimitMiddleware(
        bodyless_app,
        max_bytes=8,
        idle_timeout_seconds=0.01,
        total_timeout_seconds=0.02,
    )
    asyncio.run(
        middleware(
            _scope((b"content-type", b"application/json"), (b"content-length", b"8")),
            receive,
            send,
        )
    )

    assert _status(sent) == 408
    assert downstream_called is False
    assert budget.reserved_bytes == 0


def test_slow_declared_bodies_reserve_only_bytes_actually_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = storage._TransientByteBudget(10)
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)
    middleware = security.RequestBodyLimitMiddleware(
        _consumer([]),
        max_bytes=8,
        idle_timeout_seconds=1,
        total_timeout_seconds=2,
    )

    async def exercise() -> None:
        started = [asyncio.Event() for _ in range(4)]

        async def attack(index: int) -> None:
            first = True

            async def receive() -> Message:
                nonlocal first
                if first:
                    first = False
                    started[index].set()
                    return {"type": "http.request", "body": b"x", "more_body": True}
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def send(_message: Message) -> None:
                return

            await middleware(
                _scope(
                    (b"content-type", b"application/json"),
                    (b"content-length", b"8"),
                ),
                receive,
                send,
            )

        tasks = [asyncio.create_task(attack(index)) for index in range(4)]
        await asyncio.gather(*(event.wait() for event in started))
        await asyncio.sleep(0)
        assert budget.reserved_bytes == 4
        with storage.reserve_transient_bytes(6):
            assert budget.reserved_bytes == 10
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())
    assert budget.reserved_bytes == 0


def test_request_limit_rejects_excessive_microfragmentation_with_bounded_overhead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fragment_count = security.MAX_REQUEST_BODY_FRAGMENTS + 1
    budget = storage._TransientByteBudget(fragment_count)
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)
    calls: list[bytes] = []
    middleware = security.RequestBodyLimitMiddleware(
        _consumer(calls),
        max_bytes=fragment_count,
    )
    messages: list[Message] = [
        {"type": "http.request", "body": b"x", "more_body": True} for _ in range(fragment_count)
    ]
    messages[-1]["more_body"] = False

    sent = _run_request(
        middleware,
        _scope(
            (b"content-type", b"application/octet-stream"),
            (b"content-length", str(fragment_count).encode("ascii")),
        ),
        messages,
    )

    assert _status(sent) == 413
    assert calls == []
    assert len(messages) == 0
    assert budget.reserved_bytes == 0


def test_downstream_timeout_is_not_misreported_as_a_body_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = storage._TransientByteBudget(10)
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)

    async def downstream_timeout(_scope: Scope, receive: Receive, _send: Send) -> None:
        message = await receive()
        assert message["type"] == "http.request"
        raise TimeoutError("downstream operation timed out")

    middleware = security.RequestBodyLimitMiddleware(downstream_timeout, max_bytes=8)

    with pytest.raises(TimeoutError, match="downstream operation"):
        _run_request(
            middleware,
            _scope(
                (b"content-type", b"application/json"),
                (b"content-length", b"4"),
            ),
            [{"type": "http.request", "body": b"null", "more_body": False}],
        )

    assert budget.reserved_bytes == 0


def test_real_app_oversize_response_keeps_cors_security_and_request_headers() -> None:
    sent = _run_request(
        app,
        _scope(
            (b"origin", b"http://localhost:3000"),
            (b"content-type", b"application/json"),
            (b"content-length", str(21 * 1024 * 1024 + 1).encode("ascii")),
        ),
        [{"type": "http.request", "body": b"", "more_body": False}],
    )

    assert _status(sent) == 413
    start = next(message for message in sent if message["type"] == "http.response.start")
    headers = {name.lower(): value for name, value in start["headers"]}
    assert headers[b"access-control-allow-origin"] == b"http://localhost:3000"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-request-id"]


@pytest.mark.parametrize(
    ("headers", "messages", "expected_status"),
    (
        (
            ((b"content-length", b"1"),),
            [{"type": "http.request", "body": b"x", "more_body": False}],
            400,
        ),
        (
            (
                (
                    b"content-length",
                    str(security.MAX_HTTP_REQUEST_BYTES + 1).encode("ascii"),
                ),
            ),
            [{"type": "http.request", "body": b"", "more_body": False}],
            400,
        ),
        (
            ((b"content-length", b"0"),),
            [
                {"type": "http.request", "body": b"", "more_body": True},
                {"type": "http.disconnect"},
            ],
            400,
        ),
    ),
)
def test_real_app_preflight_cannot_bypass_body_guards(
    headers: tuple[tuple[bytes, bytes], ...],
    messages: list[Message],
    expected_status: int,
) -> None:
    sent = _run_request(app, _preflight_scope(*headers), messages)

    assert _status(sent) == expected_status
    assert _status(sent) != 200
    _assert_browser_error_headers(sent)


def test_real_app_preflights_are_charged_to_the_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    middleware = app.middleware_stack
    while not isinstance(middleware, security.RateLimitMiddleware):
        middleware = middleware.app
    assert middleware.store is None
    middleware.hits.clear()
    monkeypatch.setattr(middleware, "requests", 2)

    responses = [
        _run_request(
            app,
            _preflight_scope((b"content-length", b"0"), client="192.0.2.251"),
            [{"type": "http.request", "body": b"", "more_body": False}],
        )
        for _ in range(3)
    ]

    assert [_status(response) for response in responses] == [200, 200, 429]
    allowed_headers = _response_headers(responses[0])
    assert b"POST" in allowed_headers[b"access-control-allow-methods"]
    assert allowed_headers[b"vary"] == b"Origin"
    assert allowed_headers[b"x-content-type-options"] == b"nosniff"
    assert allowed_headers[b"x-request-id"]
    headers = _response_headers(responses[-1])
    assert headers[b"retry-after"] == str(settings.rate_limit_window_seconds).encode("ascii")
    _assert_browser_error_headers(responses[-1])


def test_real_app_guard_error_does_not_reflect_an_untrusted_origin() -> None:
    scope = _preflight_scope((b"content-length", b"1"), client="192.0.2.252")
    headers = list(scope["headers"])
    headers[0] = (b"origin", b"https://attacker.example")
    scope["headers"] = headers

    sent = _run_request(
        app,
        scope,
        [{"type": "http.request", "body": b"x", "more_body": False}],
    )

    assert _status(sent) == 400
    response_headers = _response_headers(sent)
    assert b"access-control-allow-origin" not in response_headers
    assert response_headers[b"x-content-type-options"] == b"nosniff"
    assert response_headers[b"x-request-id"]
