from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import app.config as api_config
import pytest
from custombuild_worker import generation_startup, healthcheck, registry_readiness


class FakeInspector:
    def __init__(self, response: Any, queues: Any = None) -> None:
        self.response = response
        self.queues = queues

    def ping(self) -> Any:
        return self.response

    def active_queues(self) -> Any:
        return self.queues


def test_healthcheck_requires_the_current_celery_node(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], float]] = []

    def inspect(*, destination: list[str], timeout: float) -> FakeInspector:
        calls.append((destination, timeout))
        return FakeInspector(
            {"celery@worker-1": {"ok": "pong"}},
            {"celery@worker-1": [{"name": "generation"}]},
        )

    monkeypatch.setenv("CELERY_EXPECTED_QUEUE", "generation")
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
    monkeypatch.setenv("CELERY_EXPECTED_QUEUE", "generation")
    monkeypatch.setattr(healthcheck.socket, "gethostname", lambda: "worker-1")
    monkeypatch.setattr(
        healthcheck.celery_app.control,
        "inspect",
        lambda **_kwargs: FakeInspector(response),
    )

    assert healthcheck.worker_is_responsive() is False


@pytest.mark.parametrize(
    ("expected_queue", "active_queues", "expected"),
    (
        ("generation", [{"name": "generation"}], True),
        ("maintenance", [{"name": "maintenance"}], True),
        ("storage-reaper", [{"name": "storage-reaper"}], True),
        ("generation", [{"name": "maintenance"}], False),
        ("generation", [{"name": "generation"}, {"name": "maintenance"}], False),
        ("generation", [], False),
        ("unknown", [{"name": "unknown"}], False),
    ),
)
def test_healthcheck_requires_exactly_the_configured_queue(
    monkeypatch: pytest.MonkeyPatch,
    expected_queue: str,
    active_queues: list[dict[str, str]],
    expected: bool,
) -> None:
    node_name = "celery@worker-1"
    monkeypatch.setenv("CELERY_EXPECTED_QUEUE", expected_queue)
    monkeypatch.setattr(healthcheck.socket, "gethostname", lambda: "worker-1")
    monkeypatch.setattr(
        healthcheck.celery_app.control,
        "inspect",
        lambda **_kwargs: FakeInspector(
            {node_name: {"ok": "pong"}},
            {node_name: active_queues},
        ),
    )

    assert healthcheck.worker_is_responsive() is expected


def test_healthcheck_process_exits_when_worker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(healthcheck, "worker_is_responsive", lambda: False)

    with pytest.raises(SystemExit, match="1"):
        healthcheck.main()


def _registry_json() -> str:
    return json.dumps(
        {
            "schema_version": "custombuild.joint-retention-trust-registry.v1",
            "issuers": [
                {
                    "issuer_id": "worker-health-lab",
                    "key_id": "ed25519-2026-01",
                    "role": "joint_retention_certifier",
                    "public_key_base64": base64.b64encode(bytes(range(32))).decode(
                        "ascii"
                    ),
                    "not_before": "2026-01-01T00:00:00Z",
                    "not_after": "2028-01-01T00:00:00Z",
                    "revoked_at": None,
                }
            ],
            "revoked_statement_sha256": [],
            "revoked_system_versions": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_generation_registry_readiness_uses_only_worker_settings_and_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        app_env="production",
        joint_retention_trust_registry_json=_registry_json(),
    )
    connection = object()
    calls: list[tuple[object, object, bool]] = []

    @contextmanager
    def registry_connection(_settings: object) -> Any:
        yield connection

    def record_activation(
        executor: object,
        registry: object,
        *,
        production: bool,
    ) -> int:
        calls.append((executor, registry, production))
        return 4

    def must_not_load_api_settings() -> None:
        raise AssertionError("worker registry readiness loaded API-only settings")

    monkeypatch.setattr(api_config, "get_settings", must_not_load_api_settings)
    monkeypatch.setattr(registry_readiness, "_registry_connection", registry_connection)
    monkeypatch.setattr(
        registry_readiness,
        "assert_joint_retention_registry_activated",
        record_activation,
    )

    registry_readiness.require_generation_registry_activation(
        expected_queue="generation",
        settings=settings,  # type: ignore[arg-type]
    )

    assert calls == [(connection, json.loads(_registry_json()), True)]


@pytest.mark.parametrize(
    ("expected_queue", "app_env"),
    (("maintenance", "production"), ("storage-reaper", "production"), ("generation", "test")),
)
def test_registry_readiness_skips_non_generation_or_nonproduction_runtime(
    monkeypatch: pytest.MonkeyPatch,
    expected_queue: str,
    app_env: str,
) -> None:
    monkeypatch.setattr(
        registry_readiness,
        "_registry_connection",
        lambda _settings: (_ for _ in ()).throw(AssertionError("database was opened")),
    )

    registry_readiness.require_generation_registry_activation(
        expected_queue=expected_queue,
        settings=SimpleNamespace(app_env=app_env),  # type: ignore[arg-type]
    )


def test_generation_health_fails_before_celery_ping_on_registry_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_registry(*, expected_queue: str) -> None:
        assert expected_queue == "generation"
        raise RuntimeError("registry mismatch")

    def must_not_inspect(**_kwargs: object) -> None:
        raise AssertionError("Celery readiness was advertised before registry validation")

    monkeypatch.setenv("CELERY_EXPECTED_QUEUE", "generation")
    monkeypatch.setattr(
        healthcheck,
        "require_generation_registry_activation",
        reject_registry,
    )
    monkeypatch.setattr(healthcheck.celery_app.control, "inspect", must_not_inspect)

    assert healthcheck.worker_is_responsive() is False


def test_generation_startup_requires_registry_before_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    def ready(*, expected_queue: str) -> None:
        events.append(("registry", expected_queue))

    def execute(file: str, arguments: tuple[str, ...]) -> None:
        events.append(("exec", file, arguments))

    monkeypatch.setattr(
        generation_startup,
        "require_generation_registry_activation",
        ready,
    )
    monkeypatch.setattr(generation_startup.os, "execvp", execute)

    generation_startup.main(
        ("--loglevel=INFO", "--concurrency=2", "--queues=generation")
    )

    assert events[0] == ("registry", "generation")
    assert events[1] == (
        "exec",
        "celery",
        (
            "celery",
            "--workdir",
            "services/worker",
            "-A",
            "custombuild_worker.tasks:celery_app",
            "worker",
            "--loglevel=INFO",
            "--concurrency=2",
            "--queues=generation",
        ),
    )


def test_generation_startup_does_not_exec_when_registry_is_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_registry(*, expected_queue: str) -> None:
        assert expected_queue == "generation"
        raise RuntimeError("registry mismatch")

    monkeypatch.setattr(
        generation_startup,
        "require_generation_registry_activation",
        reject_registry,
    )
    monkeypatch.setattr(
        generation_startup.os,
        "execvp",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Celery started")),
    )

    with pytest.raises(SystemExit, match="registry readiness failed"):
        generation_startup.main(
            ("--loglevel=INFO", "--concurrency=2", "--queues=generation")
        )
