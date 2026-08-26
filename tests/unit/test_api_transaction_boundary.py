from __future__ import annotations

import asyncio
from collections.abc import Iterator

from app.api import SessionDep
from app.repository import tenant_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Scope


def test_tenant_transaction_finishes_before_response_is_sent() -> None:
    events: list[str] = []
    test_app = FastAPI()

    def transactional_session() -> Iterator[object]:
        events.append("transaction_started")
        yield object()
        events.append("transaction_committed")

    test_app.dependency_overrides[tenant_session] = transactional_session

    @test_app.get("/")
    def endpoint(_session: SessionDep) -> dict[str, bool]:
        events.append("handler_returned")
        return {"ok": True}

    request_received = False

    async def receive() -> Message:
        nonlocal request_received
        if not request_received:
            request_received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            events.append("response_started")

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }

    asyncio.run(test_app(scope, receive, send))

    assert events == [
        "transaction_started",
        "handler_returned",
        "transaction_committed",
        "response_started",
    ]


def test_tenant_transaction_commit_failure_cannot_return_success() -> None:
    test_app = FastAPI()

    def failing_transactional_session() -> Iterator[object]:
        yield object()
        raise RuntimeError("commit failed")

    test_app.dependency_overrides[tenant_session] = failing_transactional_session

    @test_app.get("/")
    def endpoint(_session: SessionDep) -> dict[str, bool]:
        return {"ok": True}

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/")

    assert response.status_code == 500
