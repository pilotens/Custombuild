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


def test_compose_workers_are_isolated_to_exact_celery_queues() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]

    generation = services["worker"]
    maintenance = services["maintenance-worker"]
    storage_reaper = services["storage-reaper-worker"]
    scheduler = services["scheduler"]
    assert "--queues=generation" in generation["command"]
    assert "--queues=maintenance" not in generation["command"]
    assert generation["environment"]["CELERY_EXPECTED_QUEUE"] == "generation"
    assert "--queues=maintenance" in maintenance["command"]
    assert "--queues=generation" not in maintenance["command"]
    assert "--concurrency=1" in maintenance["command"]
    assert maintenance["environment"]["CELERY_EXPECTED_QUEUE"] == "maintenance"
    assert "--queues=storage-reaper" not in maintenance["command"]
    assert "--queues=storage-reaper" in storage_reaper["command"]
    assert "--queues=maintenance" not in storage_reaper["command"]
    assert "--queues=generation" not in storage_reaper["command"]
    assert "--concurrency=1" in storage_reaper["command"]
    assert storage_reaper["environment"]["CELERY_EXPECTED_QUEUE"] == "storage-reaper"
    assert "beat" in scheduler["command"]
    assert "worker" not in scheduler["command"]


def test_compose_acceptance_waits_for_every_healthcheck() -> None:
    for workflow_path in WORKFLOWS:
        workflow = workflow_path.read_text(encoding="utf-8")

        assert "docker compose --file compose.yml up --build --detach" in workflow
        assert "--wait --wait-timeout 900" in workflow


def test_ci_replaces_postgres_and_proves_recovery_before_writers() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    operations = Path("docs/OPERATIONS.md").read_text(encoding="utf-8")

    assert "Prove PostgreSQL replacement reruns the recovery barrier" in workflow
    assert 'stop --timeout 60 "${database_clients[@]}"' in workflow
    assert 'up --detach --force-recreate "${replacement_services[@]}"' in workflow
    database_client_block = workflow.split("database_clients=(", 1)[1].split(")", 1)[0]
    expected_clients = (
        "api",
        "worker",
        "maintenance-worker",
        "storage-reaper-worker",
        "scheduler",
        "storage-capacity-attestor",
    )
    actual_clients = tuple(database_client_block.split())
    assert actual_clients == expected_clients
    assert workflow.index('stop --timeout 60 "${database_clients[@]}"') < workflow.index(
        'up --detach --force-recreate "${replacement_services[@]}"'
    )
    replacement_block = workflow.split("replacement_services=(", 1)[1].split(")", 1)[0]
    expected_services = (
        "postgres",
        "migrate",
        "storage-recovery",
        "storage-capacity-attestor",
        "api",
        "worker",
        "maintenance-worker",
        "storage-reaper-worker",
        "scheduler",
    )
    actual_services = tuple(replacement_block.split())
    assert actual_services == expected_services
    documented_services = tuple(
        operations.split("--force-recreate", 1)[1].split("\n", 2)[1].split()
    )
    assert documented_services == expected_services
    recovery_section = operations.split("PostgreSQL deliberately uses", 1)[1].split(
        "The deploy descriptor", 1
    )[0]
    documented_clients = tuple(
        recovery_section.split("stop --timeout 60", 1)[1].splitlines()[0].split()
    )
    assert documented_clients == expected_clients
    assert "--env-file artifacts/deploy-images.env" in recovery_section
    assert "--file compose.external-production.yml" in recovery_section
    assert "--file compose.registry.yml" in recovery_section
    assert recovery_section.count('"${production_compose[@]}" up --no-build') == 2
    assert "or `--no-build`" in recovery_section
    assert "new canonical capacity operator config" in recovery_section
    assert "new protected path" in recovery_section
    assert "change only `requested_at`" in recovery_section
    assert "STORAGE_CAPACITY_OPERATOR_CONFIG_PATH" in recovery_section
    assert "STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256" in recovery_section
    assert "Do not rewrite or reuse" in recovery_section
    assert "targets `postgres` alone" in operations
    assert ".State.FinishedAt" in workflow
    assert "finished <= postgres_started" in workflow
    assert "postgres_started <= completed" in workflow
    assert "maintenance_epoch" in workflow
    assert "recovery_database_started_at = pg_postmaster_start_time()" in workflow
    assert "current_epoch > before_epoch" in workflow
    assert "started >= completed" in workflow
    for service in (
        "storage-capacity-attestor",
        "api",
        "worker",
        "maintenance-worker",
        "storage-reaper-worker",
        "scheduler",
    ):
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
    for writer in (
        "api",
        "worker",
        "maintenance-worker",
        "storage-reaper-worker",
        "scheduler",
    ):
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
    for writer in (
        "api",
        "worker",
        "maintenance-worker",
        "storage-reaper-worker",
        "scheduler",
    ):
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
