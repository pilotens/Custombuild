from __future__ import annotations

from typing import Any

import pytest
from custombuild_worker import healthcheck


class FakeInspector:
    def __init__(self, response: Any) -> None:
        self.response = response

    def ping(self) -> Any:
        return self.response


def test_healthcheck_requires_the_current_celery_node(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], float]] = []

    def inspect(*, destination: list[str], timeout: float) -> FakeInspector:
        calls.append((destination, timeout))
        return FakeInspector({"celery@worker-1": {"ok": "pong"}})

    monkeypatch.setattr(healthcheck.socket, "gethostname", lambda: "worker-1")
    monkeypatch.setattr(healthcheck.celery_app.control, "inspect", inspect)

    assert healthcheck.worker_is_responsive(timeout_seconds=2.5) is True
    assert calls == [(["celery@worker-1"], 2.5)]


@pytest.mark.parametrize(
    "response",
    (
        None,
        {},
        {"celery@another-worker": {"ok": "pong"}},
        {"celery@worker-1": {"ok": "not-pong"}},
    ),
)
def test_healthcheck_rejects_missing_or_invalid_ping(
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
) -> None:
    monkeypatch.setattr(healthcheck.socket, "gethostname", lambda: "worker-1")
    monkeypatch.setattr(
        healthcheck.celery_app.control,
        "inspect",
        lambda **_kwargs: FakeInspector(response),
    )

    assert healthcheck.worker_is_responsive() is False


def test_healthcheck_process_exits_when_worker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(healthcheck, "worker_is_responsive", lambda: False)

    with pytest.raises(SystemExit, match="1"):
        healthcheck.main()
