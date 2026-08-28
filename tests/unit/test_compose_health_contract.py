from pathlib import Path

import yaml

COMPOSE = Path("compose.yml")
WORKFLOWS = (
    Path(".github/workflows/ci.yml"),
    Path(".github/workflows/prod-ci.yml"),
)


def test_scheduler_healthcheck_is_shell_free_and_runs_as_the_container_user() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    scheduler = compose["services"]["scheduler"]
    healthcheck = scheduler["healthcheck"]["test"]

    assert healthcheck[:2] == ["CMD", "/opt/custombuild-venv/bin/python"]
    assert healthcheck[2] == "-c"
    assert "celerybeat-schedule*" in healthcheck[3]
    assert "st_mtime <= 300" in healthcheck[3]
    assert scheduler.get("user") is None
    assert "CMD-SHELL" not in healthcheck
    worker_image = Path("services/worker/Dockerfile").read_text(encoding="utf-8")
    assert "USER 65532:65532" in worker_image


def test_compose_acceptance_waits_for_every_healthcheck() -> None:
    for workflow_path in WORKFLOWS:
        workflow = workflow_path.read_text(encoding="utf-8")

        assert "docker compose --file compose.yml up --build --detach" in workflow
        assert "--wait --wait-timeout 900" in workflow


def test_ci_replaces_postgres_and_proves_recovery_before_writers() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "Prove PostgreSQL replacement reruns the recovery barrier" in workflow
    assert 'up --detach --force-recreate postgres' in workflow
    assert "maintenance_epoch" in workflow
    assert "recovery_database_started_at = pg_postmaster_start_time()" in workflow
    assert "current_epoch > before_epoch" in workflow
    assert "started >= completed" in workflow
    for service in ("storage-capacity-attestor", "api", "worker", "scheduler"):
        assert f"healthy {service}" in workflow


def test_storage_recovery_is_a_required_one_shot_startup_barrier() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    recovery = services["storage-recovery"]

    assert services["postgres"]["restart"] == "no"
    assert services["migrate"]["depends_on"]["postgres"] == {
        "condition": "service_healthy",
        "restart": True,
    }
    assert recovery["command"] == ["python", "-m", "scripts.storage_recovery"]
    assert recovery["restart"] == "no"
    assert (
        recovery["environment"]["DATABASE_URL"]
        == (services["migrate"]["environment"]["DATABASE_URL"])
    )
    assert recovery["depends_on"] == {
        "migrate": {"condition": "service_completed_successfully", "restart": True},
        "object-storage": {"condition": "service_healthy"},
    }
    assert services["storage-capacity-attestor"]["depends_on"]["storage-recovery"] == {
        "condition": "service_completed_successfully",
        "restart": True,
    }
    for writer in ("api", "worker", "scheduler"):
        assert services[writer]["depends_on"]["storage-capacity-attestor"] == {
            "condition": "service_healthy",
            "restart": True,
        }


def test_external_overlay_preserves_the_database_restart_fence() -> None:
    external = yaml.safe_load(
        Path("compose.external-production.yml").read_text(encoding="utf-8")
    )
    services = external["services"]

    assert services["postgres"]["restart"] == "no"
    assert services["migrate"]["depends_on"]["postgres"]["restart"] is True
    assert services["storage-recovery"]["depends_on"]["migrate"]["restart"] is True
    assert (
        services["storage-capacity-attestor"]["depends_on"]["storage-recovery"]["restart"]
        is True
    )
    for writer in ("api", "worker", "scheduler"):
        assert services[writer]["depends_on"]["storage-capacity-attestor"]["restart"] is True


def test_prod_runtime_probes_execute_as_each_nonroot_container_user() -> None:
    workflow = Path(".github/workflows/prod-ci.yml").read_text(encoding="utf-8")

    assert "process.getuid() !== 65532" in workflow
    assert workflow.count("assert os.getuid() == 65532, os.getuid()") == 3
    assert "exec -T scheduler" in workflow


def test_prod_runtime_evidence_binds_config_ids_refs_and_scan_manifests() -> None:
    workflow = Path(".github/workflows/prod-ci.yml").read_text(encoding="utf-8")

    assert 'schema_version "custombuild.runtime-release-evidence.v3"' in workflow
    assert 'api_id="$(image_id "$api_ref")"' in workflow
    assert 'test "$api_id" = "$(runtime_id storage-recovery)"' in workflow
    assert 'test "$api_id" = "$(runtime_id storage-capacity-attestor)"' in workflow
    assert 'volume_init_id="$(image_id "$volume_init_ref")"' in workflow
    assert ".source.target.userInput" in workflow
    assert ".source.target.manifestDigest" in workflow
    assert "^sha256:[a-f0-9]{64}$" in workflow
    assert workflow.count("image_reference:") == 7
    assert workflow.count("scan_input:") == 7
    assert workflow.count("manifest_digest:") == 7


def test_cd_runtime_evidence_binds_api_auxiliary_services() -> None:
    workflow = Path(".github/workflows/cd.yml").read_text(encoding="utf-8")

    assert 'test "$api_id" = "$(runtime_id storage-recovery)"' in workflow
    assert 'test "$api_id" = "$(runtime_id storage-capacity-attestor)"' in workflow
