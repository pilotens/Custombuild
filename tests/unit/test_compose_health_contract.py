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


def test_prod_runtime_probes_execute_as_each_nonroot_container_user() -> None:
    workflow = Path(".github/workflows/prod-ci.yml").read_text(encoding="utf-8")

    assert "process.getuid() !== 65532" in workflow
    assert workflow.count("assert os.getuid() == 65532, os.getuid()") == 3
    assert "exec -T scheduler" in workflow


def test_prod_runtime_evidence_binds_config_ids_refs_and_scan_manifests() -> None:
    workflow = Path(".github/workflows/prod-ci.yml").read_text(encoding="utf-8")

    assert 'schema_version "custombuild.runtime-release-evidence.v3"' in workflow
    assert 'api_id="$(image_id "$api_ref")"' in workflow
    assert 'volume_init_id="$(image_id "$volume_init_ref")"' in workflow
    assert ".source.target.userInput" in workflow
    assert ".source.target.manifestDigest" in workflow
    assert "^sha256:[a-f0-9]{64}$" in workflow
    assert workflow.count("image_reference:") == 7
    assert workflow.count("scan_input:") == 7
    assert workflow.count("manifest_digest:") == 7
