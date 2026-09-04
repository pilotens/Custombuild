from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts.check_external_production import external_production_issues, render_compose


def valid_config() -> dict:
    common_build = {
        "args": {
            "APP_VERSION": "1.0.0",
            "VCS_REF": "a" * 40,
            "BUILD_DATE": "2026-08-11T12:00:00Z",
            "SOURCE_URL": "https://github.com/pilotens/Custombuild",
            "SOURCE_MANIFEST_SHA256": "c" * 64,
            "DEPENDENCY_LOCK_SHA256": "b" * 64,
        }
    }
    capacity_environment = {
        "STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256": "e" * 64,
        "STORAGE_CAPACITY_VOLUME_IDENTITY": "provider-volume-0001",
        "STORAGE_CAPACITY_PROVISIONED_BYTES": "107374182400",
        "STORAGE_CAPACITY_METADATA_OVERHEAD_BYTES": "1073741824",
        "STORAGE_CAPACITY_EMERGENCY_RESERVE_BYTES": "4294967296",
        "STORAGE_CAPACITY_HEADROOM_BYTES": "5368709120",
        "STORAGE_CAPACITY_BYTE_LIMIT": "102005473280",
        "STORAGE_CAPACITY_OBJECT_LIMIT": "1000000",
        "STORAGE_CAPACITY_DEPLOY_DESCRIPTOR_SHA256": "f" * 64,
        "STORAGE_CAPACITY_MAX_AGE_SECONDS": "600",
    }
    return {
        "volumes": {
            "object-storage-data": {
                "external": True,
                "name": "provider-volume-0001",
            }
        },
        "services": {
            "postgres": {
                "restart": "no",
                "environment": {
                    "APP_ENV": "production",
                    "POSTGRES_USER": "custombuild_bootstrap",
                    "POSTGRES_PASSWORD": "strong-postgres-bootstrap-secret",
                    "MIGRATOR_DATABASE_USER": "custombuild_migrator",
                    "MIGRATOR_DATABASE_PASSWORD": "strong-postgres-migrator-secret",
                    "API_DATABASE_PASSWORD": "strong-postgres-api-secret",
                    "WORKER_DATABASE_PASSWORD": "strong-postgres-worker-secret",
                    "CAPACITY_ATTESTOR_DATABASE_USER": "custombuild_storage_attestor",
                    "CAPACITY_ATTESTOR_DATABASE_PASSWORD": (
                        "strong-postgres-capacity-attestor-secret"
                    ),
                }
            },
            "redis": {"environment": {"REDIS_PASSWORD": "strong-production-redis-secret"}},
            "object-storage": {
                "environment": {
                    "AWS_ACCESS_KEY_ID": "production-s3-access",
                    "AWS_SECRET_ACCESS_KEY": "strong-production-s3-secret",
                    "S3_BUCKET": "production-artifacts",
                    "S3_BACKUP_ENDPOINT": "http://127.0.0.1:9000",
                },
                "ports": [{"host_ip": "127.0.0.1", "published": "9000", "target": 8333}],
            },
            "migrate": {
                "build": common_build,
                "environment": {
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "DATABASE_URL": (
                        "postgresql://custombuild_migrator:strong-postgres-migrator-secret"
                        "@postgres/db"
                    ),
                },
                "depends_on": {
                    "postgres": {"condition": "service_healthy", "restart": True}
                },
            },
            "storage-recovery": {
                "image": "ghcr.io/pilotens/custombuild-api@sha256:" + "a" * 64,
                "build": common_build,
                "command": ["python", "-m", "scripts.storage_recovery"],
                "environment": {
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "DATABASE_URL": (
                        "postgresql://custombuild_migrator:"
                        "strong-postgres-migrator-secret@postgres/db"
                    ),
                    "S3_SECRET_KEY": "strong-production-s3-secret",
                    "S3_ENDPOINT": "http://object-storage:8333",
                    "S3_ACCESS_KEY": "production-s3-access",
                    "S3_BUCKET": "production-artifacts",
                },
                "user": "65532:65532",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "pids_limit": 64,
                "restart": "no",
                "networks": {"backend": None},
                "depends_on": {
                    "migrate": {
                        "condition": "service_completed_successfully",
                        "restart": True,
                    },
                    "object-storage": {"condition": "service_healthy"},
                },
            },
            "storage-capacity-attestor": {
                "image": "ghcr.io/pilotens/custombuild-api@sha256:" + "a" * 64,
                "command": [
                    "python",
                    "-m",
                    "scripts.storage_capacity_preflight",
                ],
                "environment": {
                    **capacity_environment,
                    "APP_ENV": "production",
                    "OBJECT_STORAGE_VOLUME_NAME": "provider-volume-0001",
                    "DATABASE_URL": (
                        "postgresql://custombuild_storage_attestor:"
                        "strong-postgres-capacity-attestor-secret"
                        "@postgres/db"
                    ),
                    "S3_SECRET_KEY": "strong-production-s3-secret",
                    "S3_ENDPOINT": "http://object-storage:8333",
                    "S3_ACCESS_KEY": "production-s3-access",
                    "S3_BUCKET": "production-artifacts",
                },
                "user": "65532:65532",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "pids_limit": 64,
                "networks": {"backend": None},
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        "check capacity-heartbeat.json",
                    ]
                },
                "depends_on": {
                    "storage-recovery": {
                        "condition": "service_completed_successfully",
                        "restart": True,
                    },
                    "object-storage": {"condition": "service_healthy"},
                },
                "volumes": [
                    {
                        "type": "volume",
                        "source": "object-storage-data",
                        "target": "/storage-volume",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": "/srv/custombuild/capacity-evidence",
                        "target": "/evidence",
                        "read_only": False,
                    },
                ],
            },
            "api": {
                "image": "ghcr.io/pilotens/custombuild-api@sha256:" + "a" * 64,
                "build": common_build,
                "environment": {
                    **capacity_environment,
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "AUTH_MODE": "oidc",
                    "OIDC_ISSUER": "https://identity.example.test/",
                    "TRUSTED_PROXY_CIDRS": "172.20.0.0/24",
                    "CORS_ORIGINS": "https://app.example.test",
                    "DATABASE_URL": (
                        "postgresql://custombuild_api:strong-api-database-secret@postgres/db"
                    ),
                    "REDIS_URL": "redis://:strong-production-redis-secret@redis:6379/0",
                    "S3_SECRET_KEY": "strong-production-s3-secret",
                    "S3_ENDPOINT": "http://object-storage:8333",
                    "S3_ACCESS_KEY": "production-s3-access",
                    "S3_BUCKET": "production-artifacts",
                    "ARTIFACT_SIGNING_SECRET": "strong-signing-secret-over-32-bytes",
                },
                "depends_on": {
                    "storage-capacity-attestor": {
                        "condition": "service_healthy",
                        "restart": True,
                    }
                },
                "ports": [{"host_ip": "127.0.0.1", "published": "8000", "target": 8000}],
            },
            "worker": {
                "build": common_build,
                "command": [
                    "python",
                    "-m",
                    "custombuild_worker.generation_startup",
                    "--loglevel=INFO",
                    "--concurrency=2",
                    "--queues=generation",
                ],
                "environment": {
                    **capacity_environment,
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "DATABASE_URL": (
                        "postgresql://custombuild_worker:strong-worker-database-secret@postgres/db"
                    ),
                    "REDIS_URL": "redis://:strong-production-redis-secret@redis:6379/0",
                    "S3_SECRET_KEY": "strong-production-s3-secret",
                    "S3_ENDPOINT": "http://object-storage:8333",
                    "S3_ACCESS_KEY": "production-s3-access",
                    "S3_BUCKET": "production-artifacts",
                    "CELERY_EXPECTED_QUEUE": "generation",
                },
                "depends_on": {
                    "storage-capacity-attestor": {
                        "condition": "service_healthy",
                        "restart": True,
                    }
                },
            },
            "maintenance-worker": {
                "build": common_build,
                "command": [
                    "celery",
                    "worker",
                    "--concurrency=1",
                    "--queues=maintenance",
                ],
                "environment": {
                    **capacity_environment,
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "DATABASE_URL": (
                        "postgresql://custombuild_worker:strong-worker-database-secret@postgres/db"
                    ),
                    "REDIS_URL": "redis://:strong-production-redis-secret@redis:6379/0",
                    "S3_SECRET_KEY": "strong-production-s3-secret",
                    "S3_ENDPOINT": "http://object-storage:8333",
                    "S3_ACCESS_KEY": "production-s3-access",
                    "S3_BUCKET": "production-artifacts",
                    "CELERY_EXPECTED_QUEUE": "maintenance",
                },
                "depends_on": {
                    "storage-capacity-attestor": {
                        "condition": "service_healthy",
                        "restart": True,
                    }
                },
            },
            "storage-reaper-worker": {
                "build": common_build,
                "command": [
                    "celery",
                    "worker",
                    "--concurrency=1",
                    "--queues=storage-reaper",
                ],
                "environment": {
                    **capacity_environment,
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "DATABASE_URL": (
                        "postgresql://custombuild_worker:strong-worker-database-secret@postgres/db"
                    ),
                    "REDIS_URL": "redis://:strong-production-redis-secret@redis:6379/0",
                    "S3_SECRET_KEY": "strong-production-s3-secret",
                    "S3_ENDPOINT": "http://object-storage:8333",
                    "S3_ACCESS_KEY": "production-s3-access",
                    "S3_BUCKET": "production-artifacts",
                    "CELERY_EXPECTED_QUEUE": "storage-reaper",
                },
                "depends_on": {
                    "storage-capacity-attestor": {
                        "condition": "service_healthy",
                        "restart": True,
                    }
                },
            },
            "scheduler": {
                "build": common_build,
                "command": ["celery", "beat", "--loglevel=WARNING"],
                "environment": {
                    **capacity_environment,
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "DATABASE_URL": (
                        "postgresql://custombuild_worker:strong-worker-database-secret@postgres/db"
                    ),
                    "REDIS_URL": "redis://:strong-production-redis-secret@redis:6379/0",
                    "S3_SECRET_KEY": "strong-production-s3-secret",
                    "S3_ENDPOINT": "http://object-storage:8333",
                    "S3_ACCESS_KEY": "production-s3-access",
                    "S3_BUCKET": "production-artifacts",
                },
                "depends_on": {
                    "storage-capacity-attestor": {
                        "condition": "service_healthy",
                        "restart": True,
                    }
                },
            },
            "web": {
                "build": {
                    "args": {
                        **{
                            key: value
                            for key, value in common_build["args"].items()
                            if key != "DEPENDENCY_LOCK_SHA256"
                        },
                        "FRONTEND_LOCK_SHA256": "d" * 64,
                    }
                },
                "environment": {
                    "APP_ENV": "production",
                    "CUSTOMBUILD_WEB_API_URL": "https://api.example.test",
                    "CUSTOMBUILD_WEB_DEMO_TOKEN": "",
                    "CUSTOMBUILD_WEB_OIDC_ISSUER": "https://identity.example.test/",
                    "CUSTOMBUILD_WEB_OIDC_CLIENT_ID": "custombuild-web",
                    "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI": "https://app.example.test/",
                },
                "ports": [{"host_ip": "127.0.0.1", "published": "3000", "target": 3000}],
            },
        },
    }


def test_accepts_fail_closed_external_production_contract() -> None:
    assert (
        external_production_issues(
            valid_config(),
            expected_dependency_lock_sha256="b" * 64,
            expected_frontend_lock_sha256="d" * 64,
            expected_source_manifest_sha256="c" * 64,
            expected_vcs_ref="a" * 40,
        )
        == []
    )


def test_rejects_generation_worker_that_bypasses_registry_startup_gate() -> None:
    config = valid_config()
    config["services"]["worker"]["command"] = [
        "celery",
        "worker",
        "--loglevel=INFO",
        "--concurrency=2",
        "--queues=generation",
    ]

    assert (
        "worker does not consume only the generation queue"
        in external_production_issues(config)
    )


def test_rejects_missing_cross_routed_or_unbound_maintenance_worker() -> None:
    missing = valid_config()
    del missing["services"]["maintenance-worker"]
    assert external_production_issues(missing) == [
        "required services are missing: maintenance-worker"
    ]

    cross_routed = valid_config()
    maintenance = cross_routed["services"]["maintenance-worker"]
    maintenance["command"] = [
        "celery",
        "worker",
        "--concurrency=2",
        "--queues=generation",
    ]
    maintenance["environment"]["CELERY_EXPECTED_QUEUE"] = "generation"
    issues = external_production_issues(cross_routed)
    assert "maintenance-worker is not the singleton maintenance consumer" in issues
    assert "maintenance-worker consumes the generation queue" in issues
    assert "maintenance-worker health is not bound to its exact Celery queue" in issues


def test_external_attestor_inherits_one_no_new_privileges_option() -> None:
    base = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))
    external = yaml.safe_load(
        Path("compose.external-production.yml").read_text(encoding="utf-8")
    )
    base_options = base["services"]["storage-capacity-attestor"]["security_opt"]
    overlay_options = external["services"]["storage-capacity-attestor"].get(
        "security_opt", []
    )

    assert base_options + overlay_options == ["no-new-privileges:true"]


def test_external_auxiliary_services_share_the_descriptor_api_image() -> None:
    external = yaml.safe_load(
        Path("compose.external-production.yml").read_text(encoding="utf-8")
    )
    services = external["services"]

    assert services["api"]["image"] == services["storage-recovery"]["image"]
    assert services["api"]["image"] == services["storage-capacity-attestor"]["image"]


def test_rendered_auxiliary_services_reject_api_image_digest_drift() -> None:
    config = deepcopy(valid_config())
    config["services"]["storage-recovery"]["image"] = (
        "ghcr.io/pilotens/custombuild-api@sha256:" + "b" * 64
    )
    config["services"]["storage-capacity-attestor"]["image"] = (
        "ghcr.io/pilotens/custombuild-api@sha256:" + "c" * 64
    )

    issues = external_production_issues(config)

    assert "storage-recovery does not use the exact API image" in issues
    assert "storage-capacity-attestor does not use the exact API image" in issues


def test_rejects_attestor_without_no_new_privileges() -> None:
    config = deepcopy(valid_config())
    config["services"]["storage-capacity-attestor"]["security_opt"] = []

    assert (
        "storage-capacity-attestor does not enforce no-new-privileges"
        in external_production_issues(config)
    )


def test_render_failure_includes_bounded_sanitized_compose_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stderr = (
        "x" * 3_000
        + " POSTGRES_PASSWORD=top-secret-value "
        + "services.storage-capacity-attestor.security_opt items at 0 and 1 are equal\x00"
    )

    def failed_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr("scripts.check_external_production.subprocess.run", failed_run)

    with pytest.raises(RuntimeError) as error:
        render_compose(tmp_path)

    message = str(error.value)
    assert len(message) <= 2_120
    assert "top-secret-value" not in message
    assert "POSTGRES_PASSWORD=<redacted>" in message
    assert "\x00" not in message
    assert "services.storage-capacity-attestor.security_opt items at 0 and 1 are equal" in message


def test_rejects_mutable_or_privileged_storage_recovery() -> None:
    config = deepcopy(valid_config())
    recovery = config["services"]["storage-recovery"]
    recovery.update(
        {
            "image": "ghcr.io/pilotens/custombuild-api:latest",
            "command": "python -m scripts.storage_recovery",
            "entrypoint": ["/bin/sh", "-c"],
            "user": "0:0",
            "read_only": False,
            "cap_drop": [],
            "cap_add": ["SYS_ADMIN"],
            "privileged": True,
            "security_opt": [],
            "pids_limit": 0,
            "restart": "unless-stopped",
            "networks": ["backend", "edge"],
            "volumes": ["object-storage-data:/data"],
            "ports": ["127.0.0.1:9999:9999"],
        }
    )

    issues = external_production_issues(config)

    for expected in (
        "storage-recovery does not run the fixed one-shot command",
        "storage-recovery image is not digest pinned",
        "storage-recovery does not use the exact API image",
        "storage-recovery must run as the fixed non-root user",
        "storage-recovery root filesystem is not read-only",
        "storage-recovery does not drop all Linux capabilities",
        "storage-recovery adds Linux capabilities",
        "storage-recovery is privileged",
        "storage-recovery does not enforce no-new-privileges",
        "storage-recovery has no positive PID limit",
        "storage-recovery must attach only to the backend network",
        "storage-recovery is not an explicit one-shot service",
        "storage-recovery must not mount storage volumes",
        "storage-recovery must not publish host ports",
    ):
        assert expected in issues


def test_rejects_optional_or_broken_storage_recovery_chain() -> None:
    config = deepcopy(valid_config())
    config["services"]["storage-recovery"]["depends_on"]["migrate"]["required"] = False
    config["services"]["storage-recovery"]["depends_on"].pop("object-storage")
    config["services"]["storage-capacity-attestor"]["depends_on"]["storage-recovery"][
        "required"
    ] = False

    issues = external_production_issues(config)

    assert "storage-recovery does not wait for completed migrations" in issues
    assert "storage-recovery does not wait for healthy object storage" in issues
    assert "storage-capacity-attestor does not wait for completed storage recovery" in issues


@pytest.mark.parametrize(
    ("service", "dependency", "expected_issue"),
    (
        (
            "migrate",
            "postgres",
            "migrate does not restart after a Compose-managed PostgreSQL update",
        ),
        (
            "storage-recovery",
            "migrate",
            "storage-recovery does not restart after a Compose-managed migration",
        ),
        (
            "storage-capacity-attestor",
            "storage-recovery",
            "storage-capacity-attestor does not restart after Compose-managed storage recovery",
        ),
        (
            "api",
            "storage-capacity-attestor",
            "api does not restart after Compose-managed capacity attestation",
        ),
        (
            "worker",
            "storage-capacity-attestor",
            "worker does not restart after Compose-managed capacity attestation",
        ),
        (
            "scheduler",
            "storage-capacity-attestor",
            "scheduler does not restart after Compose-managed capacity attestation",
        ),
    ),
)
def test_rejects_broken_compose_managed_database_restart_chain(
    service: str,
    dependency: str,
    expected_issue: str,
) -> None:
    config = deepcopy(valid_config())
    config["services"][service]["depends_on"][dependency]["restart"] = False

    assert expected_issue in external_production_issues(config)


def test_rejects_automatic_postgres_restart_behind_recovery_barrier() -> None:
    config = deepcopy(valid_config())
    config["services"]["postgres"]["restart"] = "unless-stopped"

    assert (
        "postgres automatic restart can bypass the storage-recovery barrier"
        in external_production_issues(config)
    )


def test_rejects_non_migrator_or_different_storage_recovery_database() -> None:
    config = deepcopy(valid_config())
    config["services"]["storage-recovery"]["environment"]["DATABASE_URL"] = (
        "postgresql://custombuild_worker:strong-postgres-worker-secret@postgres/db"
    )

    issues = external_production_issues(config)

    assert "storage-recovery.DATABASE_URL must use the fixed custombuild_migrator role" in issues
    assert "storage-recovery.DATABASE_URL does not exactly match migrate" in issues

    config = deepcopy(valid_config())
    config["services"]["storage-recovery"]["environment"]["DATABASE_URL"] = (
        "postgresql://custombuild_migrator:different-strong-migrator-secret@postgres/db"
    )

    issues = external_production_issues(config)

    assert (
        "storage-recovery.DATABASE_URL password does not match the provisioned migrator secret"
    ) in issues
    assert "storage-recovery.DATABASE_URL does not exactly match migrate" in issues


def test_rejects_capacity_drift_mutable_attestor_and_missing_dependency() -> None:
    config = deepcopy(valid_config())
    attestor = config["services"]["storage-capacity-attestor"]
    attestor["image"] = "ghcr.io/pilotens/custombuild-api:latest"
    attestor["command"] = [
        "python",
        "-m",
        "scripts.storage_capacity_development",
    ]
    attestor["environment"]["STORAGE_CAPACITY_HEADROOM_BYTES"] = "1"
    config["services"]["worker"]["environment"]["STORAGE_CAPACITY_BYTE_LIMIT"] = "999"
    del config["services"]["api"]["depends_on"]["storage-capacity-attestor"]

    issues = external_production_issues(config)

    assert "storage-capacity-attestor does not run the strict preflight" in issues
    assert "storage-capacity-attestor image is not digest pinned" in issues
    assert (
        "storage capacity field STORAGE_CAPACITY_BYTE_LIMIT differs between writers and attestor"
        in issues
    )
    assert "storage capacity headroom does not equal its reserved components" in issues
    assert "api does not wait for healthy storage capacity evidence" in issues


def test_rejects_bind_mount_disguised_as_the_object_storage_volume() -> None:
    config = deepcopy(valid_config())
    storage_mount = config["services"]["storage-capacity-attestor"]["volumes"][0]
    storage_mount["type"] = "bind"

    assert (
        "storage-capacity-attestor does not read the exact storage volume"
        in external_production_issues(config)
    )


def test_rejects_attestor_image_that_differs_from_the_api_image() -> None:
    config = deepcopy(valid_config())
    config["services"]["storage-capacity-attestor"]["image"] = (
        "ghcr.io/pilotens/custombuild-api@sha256:" + "b" * 64
    )
    assert (
        "storage-capacity-attestor does not use the exact API image"
        in external_production_issues(config)
    )


def test_rejects_migrator_or_mismatched_secret_for_capacity_attestor() -> None:
    config = deepcopy(valid_config())
    config["services"]["storage-capacity-attestor"]["environment"]["DATABASE_URL"] = (
        "postgresql://custombuild_migrator:strong-migrator-database-secret@postgres/db"
    )

    issues = external_production_issues(config)

    assert (
        "storage-capacity-attestor.DATABASE_URL must use the fixed "
        "custombuild_storage_attestor role"
    ) in issues

    config = deepcopy(valid_config())
    config["services"]["storage-capacity-attestor"]["environment"]["DATABASE_URL"] = (
        "postgresql://custombuild_storage_attestor:"
        "different-strong-capacity-attestor-secret@postgres/db"
    )

    assert (
        "storage-capacity-attestor.DATABASE_URL password does not match the "
        "provisioned storage-attestor secret"
    ) in external_production_issues(config)

    config = deepcopy(valid_config())
    shared = config["services"]["postgres"]["environment"]["MIGRATOR_DATABASE_PASSWORD"]
    config["services"]["postgres"]["environment"]["CAPACITY_ATTESTOR_DATABASE_PASSWORD"] = shared
    config["services"]["storage-capacity-attestor"]["environment"]["DATABASE_URL"] = (
        f"postgresql://custombuild_storage_attestor:{shared}@postgres/db"
    )

    assert (
        "postgres.CAPACITY_ATTESTOR_DATABASE_PASSWORD must be unique to the storage-attestor role"
    ) in external_production_issues(config)


def test_rejects_demo_auth_insecure_origins_and_public_datastores() -> None:
    config = deepcopy(valid_config())
    config["services"]["api"]["environment"].update(
        {
            "AUTH_MODE": "development",
            "OIDC_ISSUER": "http://identity.example.test",
            "CORS_ORIGINS": "http://app.example.test",
            "ARTIFACT_SIGNING_SECRET": "change-me-signing",
        }
    )
    config["services"]["web"]["environment"]["CUSTOMBUILD_WEB_DEMO_TOKEN"] = "demo-owner"  # noqa: S105 - intentionally insecure negative fixture.
    config["services"]["api"]["build"]["args"].update(
        {"VCS_REF": "uncommitted", "BUILD_DATE": "unknown", "SOURCE_URL": "http://source.test"}
    )
    config["services"]["postgres"]["ports"] = [
        {"host_ip": "0.0.0.0", "target": 5432}  # noqa: S104
    ]

    issues = external_production_issues(config)

    assert "api does not require OIDC authentication" in issues
    assert "api OIDC_ISSUER must contain only HTTPS origins" in issues
    assert "api CORS_ORIGINS must contain only HTTPS origins" in issues
    assert (
        "api.ARTIFACT_SIGNING_SECRET is missing, too short, or uses an insecure default" in issues
    )
    assert "web exposes a development demo token in production" in issues
    assert "api has no exact source revision" in issues
    assert "api has no timezone-aware build timestamp" in issues
    assert "api has no HTTPS canonical source URL" in issues
    assert "postgres must not publish host ports" in issues


def test_rejects_oidc_issuer_drift_and_an_unimplemented_callback_route() -> None:
    config = deepcopy(valid_config())
    web_env = config["services"]["web"]["environment"]
    web_env["CUSTOMBUILD_WEB_OIDC_ISSUER"] = "https://other-identity.example.test/"
    web_env["CUSTOMBUILD_WEB_OIDC_REDIRECT_URI"] = "https://app.example.test/callback"

    issues = external_production_issues(config)

    assert "API and web OIDC issuers do not match" in issues
    assert "web OIDC callback must use the implemented root route" in issues


def test_rejects_missing_or_invalid_trusted_proxy_networks() -> None:
    missing = deepcopy(valid_config())
    missing["services"]["api"]["environment"]["TRUSTED_PROXY_CIDRS"] = ""
    invalid = deepcopy(valid_config())
    invalid["services"]["api"]["environment"]["TRUSTED_PROXY_CIDRS"] = "not-a-cidr"
    public = deepcopy(valid_config())
    public["services"]["api"]["environment"]["TRUSTED_PROXY_CIDRS"] = "0.0.0.0/0"

    assert "api has no trusted TLS-proxy CIDR" in external_production_issues(missing)
    expected = "api TRUSTED_PROXY_CIDRS must contain only private IP networks"
    assert expected in external_production_issues(invalid)
    assert expected in external_production_issues(public)


@pytest.mark.parametrize(
    "endpoint",
    (
        "",
        "http://object-storage:8333",
        "http://0.0.0.0:9000",
        "https://127.0.0.1:9000",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "\x00http://127.0.0.1:9000",
        "\nhttp://127.0.0.1:9000",
        "http://127.0.0.1:\t9000",
        "http:\\//127.0.0.1:9000",
        "http://user@127.0.0.1:9000",
        "http://127.0.0.1:9000/path",
    ),
)
def test_rejects_non_loopback_or_ambiguous_backup_endpoints(endpoint: str) -> None:
    config = deepcopy(valid_config())
    config["services"]["object-storage"]["environment"]["S3_BACKUP_ENDPOINT"] = endpoint

    assert (
        "object-storage S3_BACKUP_ENDPOINT must be an explicit loopback URL"
        in external_production_issues(config)
    )


@pytest.mark.parametrize(
    ("endpoint", "ports"),
    (
        (
            "http://127.0.0.1:9999",
            [{"host_ip": "127.0.0.1", "published": "9000", "target": 8333}],
        ),
        (
            "http://127.0.0.1:9000",
            [{"host_ip": "127.0.0.1", "published": "9000", "target": 9001}],
        ),
        (
            "http://127.0.0.1:9000",
            [{"host_ip": "::1", "published": "9000", "target": 8333}],
        ),
        (
            "http://127.0.0.1:9000",
            [{"host_ip": "127.0.0.1", "published": 0, "target": 8333}],
        ),
        (
            "http://127.0.0.1:9000",
            [{"host_ip": "127.0.0.1", "published": 65_536, "target": 8333}],
        ),
        (
            "http://127.0.0.1:9000",
            [{"host_ip": "127.0.0.1", "published": True, "target": 8333}],
        ),
        (
            "http://127.0.0.1:9000",
            [{"host_ip": "127.0.0.1", "published": "-1", "target": 8333}],
        ),
        (
            "http://127.0.0.1:9000",
            [{"host_ip": "127.0.0.1", "published": 9000, "target": 0}],
        ),
        (
            "http://127.0.0.1:9000",
            [{"host_ip": "127.0.0.1", "published": 9000, "target": 65_536}],
        ),
        (
            "http://127.0.0.1:9000",
            [{"host_ip": "127.0.0.1", "published": 9000, "target": True}],
        ),
        (
            "http://127.0.0.1:9000",
            [{"host_ip": "127.0.0.1", "published": 9000, "target": "-1"}],
        ),
    ),
)
def test_backup_endpoint_must_match_the_exact_published_s3_socket(
    endpoint: str,
    ports: list[dict[str, object]],
) -> None:
    config = deepcopy(valid_config())
    config["services"]["object-storage"]["environment"]["S3_BACKUP_ENDPOINT"] = endpoint
    config["services"]["object-storage"]["ports"] = ports

    assert (
        "object-storage S3_BACKUP_ENDPOINT does not match its loopback S3 port"
        in external_production_issues(config)
    )


@pytest.mark.parametrize(
    "service",
    ("storage-recovery", "storage-capacity-attestor", "api", "worker", "scheduler"),
)
@pytest.mark.parametrize(
    ("field", "value", "expected_issue"),
    (
        (
            "S3_ENDPOINT",
            "https://attacker.example.test",
            "S3_ENDPOINT must be exactly http://object-storage:8333",
        ),
        ("S3_ACCESS_KEY", "attacker-access", "S3_ACCESS_KEY does not match object-storage"),
        (
            "S3_SECRET_KEY",
            "attacker-secret-that-is-long-enough",
            "S3_SECRET_KEY does not match object-storage",
        ),
        ("S3_BUCKET", "attacker-bucket", "S3_BUCKET does not match object-storage"),
    ),
)
def test_application_s3_clients_are_bound_to_the_internal_storage_identity(
    service: str,
    field: str,
    value: str,
    expected_issue: str,
) -> None:
    config = deepcopy(valid_config())
    config["services"][service]["environment"][field] = value

    assert f"{service} {expected_issue}" in external_production_issues(config)


@pytest.mark.parametrize(
    ("storage_key", "service_key", "value", "expected_issue"),
    (
        (
            "AWS_ACCESS_KEY_ID",
            "S3_ACCESS_KEY",
            " production-s3-access",
            "object-storage.AWS_ACCESS_KEY_ID must be a canonical header-safe access key",
        ),
        (
            "AWS_ACCESS_KEY_ID",
            "S3_ACCESS_KEY",
            "production\naccess",
            "object-storage.AWS_ACCESS_KEY_ID must be a canonical header-safe access key",
        ),
        (
            "AWS_ACCESS_KEY_ID",
            "S3_ACCESS_KEY",
            "production:access",
            "object-storage.AWS_ACCESS_KEY_ID must be a canonical header-safe access key",
        ),
        (
            "AWS_SECRET_ACCESS_KEY",
            "S3_SECRET_KEY",
            " strong-production-s3-secret",
            "object-storage.AWS_SECRET_ACCESS_KEY is missing, too short, or uses an "
            "insecure default",
        ),
        (
            "AWS_SECRET_ACCESS_KEY",
            "S3_SECRET_KEY",
            "strong-production\x00s3-secret-value",
            "object-storage.AWS_SECRET_ACCESS_KEY is missing, too short, or uses an "
            "insecure default",
        ),
        (
            "S3_BUCKET",
            "S3_BUCKET",
            " production-artifacts",
            "object-storage.S3_BUCKET must be a canonical S3 DNS name",
        ),
        (
            "S3_BUCKET",
            "S3_BUCKET",
            "production\nartifacts",
            "object-storage.S3_BUCKET must be a canonical S3 DNS name",
        ),
        (
            "S3_BUCKET",
            "S3_BUCKET",
            "arn:aws:s3:::production-artifacts",
            "object-storage.S3_BUCKET must be a canonical S3 DNS name",
        ),
        (
            "S3_BUCKET",
            "S3_BUCKET",
            "production/artifacts",
            "object-storage.S3_BUCKET must be a canonical S3 DNS name",
        ),
        (
            "S3_BUCKET",
            "S3_BUCKET",
            "a" * 256,
            "object-storage.S3_BUCKET must be a canonical S3 DNS name",
        ),
        (
            "S3_BUCKET",
            "S3_BUCKET",
            "Production-Artifacts",
            "object-storage.S3_BUCKET must be a canonical S3 DNS name",
        ),
        (
            "S3_BUCKET",
            "S3_BUCKET",
            "production_artifacts",
            "object-storage.S3_BUCKET must be a canonical S3 DNS name",
        ),
        (
            "S3_BUCKET",
            "S3_BUCKET",
            "..",
            "object-storage.S3_BUCKET must be a canonical S3 DNS name",
        ),
        (
            "S3_BUCKET",
            "S3_BUCKET",
            "a..b",
            "object-storage.S3_BUCKET must be a canonical S3 DNS name",
        ),
        (
            "S3_BUCKET",
            "S3_BUCKET",
            "192.0.2.1",
            "object-storage.S3_BUCKET must be a canonical S3 DNS name",
        ),
    ),
)
def test_shared_s3_identity_must_be_canonical_before_exact_matching(
    storage_key: str,
    service_key: str,
    value: str,
    expected_issue: str,
) -> None:
    config = deepcopy(valid_config())
    config["services"]["object-storage"]["environment"][storage_key] = value
    for service in ("api", "worker", "scheduler"):
        config["services"][service]["environment"][service_key] = value

    assert expected_issue in external_production_issues(config)


def test_rejects_callback_on_an_unapproved_origin_and_accepts_issuer_slash_drift() -> None:
    config = deepcopy(valid_config())
    web_env = config["services"]["web"]["environment"]
    web_env["CUSTOMBUILD_WEB_OIDC_ISSUER"] = "https://identity.example.test"
    web_env["CUSTOMBUILD_WEB_OIDC_REDIRECT_URI"] = "https://other-app.example.test/"

    issues = external_production_issues(config)

    assert "API and web OIDC issuers do not match" not in issues
    assert "web OIDC callback origin is not an approved web origin" in issues


def test_rejects_missing_lock_identity_and_cross_service_drift() -> None:
    config = deepcopy(valid_config())
    config["services"]["api"]["build"] = deepcopy(config["services"]["api"]["build"])
    config["services"]["api"]["build"]["args"]["VCS_REF"] = "c" * 40
    config["services"]["worker"]["build"] = deepcopy(config["services"]["worker"]["build"])
    config["services"]["worker"]["build"]["args"]["DEPENDENCY_LOCK_SHA256"] = "unknown"
    config["services"]["web"]["build"]["args"]["FRONTEND_LOCK_SHA256"] = "unknown"
    config["services"]["scheduler"]["build"] = deepcopy(config["services"]["scheduler"]["build"])
    config["services"]["scheduler"]["build"]["args"]["SOURCE_MANIFEST_SHA256"] = "unknown"

    issues = external_production_issues(config)

    assert "worker has no exact uv.lock SHA-256" in issues
    assert "web has no exact pnpm-lock.yaml SHA-256" in issues
    assert "scheduler has no exact source manifest SHA-256" in issues
    assert "application services do not share one exact release identity" in issues


def test_rejects_a_well_formed_but_wrong_lock_hash() -> None:
    issues = external_production_issues(
        valid_config(),
        expected_dependency_lock_sha256="c" * 64,
        expected_frontend_lock_sha256="e" * 64,
        expected_source_manifest_sha256="f" * 64,
        expected_vcs_ref="f" * 40,
    )

    assert "api uv.lock SHA-256 does not match the checked source tree" in issues
    assert "web pnpm-lock.yaml SHA-256 does not match the checked source tree" in issues
    assert "api source manifest does not match the checked build/control set" in issues
    assert "api source revision does not match the checked Git HEAD" in issues


def test_rejects_non_loopback_publication_and_missing_service() -> None:
    config = valid_config()
    config["services"]["api"]["ports"][0]["host_ip"] = "0.0.0.0"  # noqa: S104
    del config["services"]["scheduler"]

    issues = external_production_issues(config)

    assert issues == ["required services are missing: scheduler"]


def test_rejects_short_secrets_and_role_substitution() -> None:
    config = deepcopy(valid_config())
    config["services"]["postgres"]["environment"]["POSTGRES_USER"] = "custombuild_migrator"
    config["services"]["api"]["environment"]["DATABASE_URL"] = (
        "postgresql://custombuild_worker:short@postgres/db"
    )
    config["services"]["worker"]["environment"]["REDIS_URL"] = (
        "redis://:x@redis-with-a-very-long-hostname:6379/0"
    )
    config["services"]["object-storage"]["environment"]["AWS_SECRET_ACCESS_KEY"] = "x"  # noqa: S105 - intentionally insecure negative fixture.

    issues = external_production_issues(config)

    assert "postgres.POSTGRES_USER must be custombuild_bootstrap" in issues
    assert "api.DATABASE_URL must use the fixed custombuild_api role" in issues
    assert "worker.REDIS_URL password is missing, too short, or insecure" in issues
    assert any("object-storage.AWS_SECRET_ACCESS_KEY" in issue for issue in issues)


def test_rejects_disabled_or_missing_four_eyes_production_approval() -> None:
    config = deepcopy(valid_config())
    config["services"]["api"]["environment"]["PRODUCTION_FOUR_EYES_REQUIRED"] = "false"
    del config["services"]["worker"]["environment"]["PRODUCTION_FOUR_EYES_REQUIRED"]

    issues = external_production_issues(config)

    assert "api does not require four-eyes production approval" in issues
    assert "worker does not require four-eyes production approval" in issues


def test_rejects_public_web_configuration_baked_into_the_image() -> None:
    config = deepcopy(valid_config())
    config["services"]["web"]["build"]["args"]["CUSTOMBUILD_WEB_API_URL"] = (
        "https://api.other-environment.test"
    )

    issues = external_production_issues(config)

    assert "web public runtime configuration is baked into image build arguments" in issues


def test_rejects_legacy_variables_and_a_non_origin_runtime_api() -> None:
    config = deepcopy(valid_config())
    web_env = config["services"]["web"]["environment"]
    web_env["NEXT_PUBLIC_API_URL"] = "https://api.example.test"
    web_env["CUSTOMBUILD_WEB_API_URL"] = "https://api.example.test/v1"

    issues = external_production_issues(config)

    assert "web uses legacy NEXT_PUBLIC runtime variables" in issues
    assert "web CUSTOMBUILD_WEB_API_URL must be an exact HTTPS origin" in issues


def test_rejects_web_without_explicit_production_runtime_mode() -> None:
    config = deepcopy(valid_config())
    config["services"]["web"]["environment"]["APP_ENV"] = "development"

    assert "web does not run with APP_ENV=production" in external_production_issues(config)


@pytest.mark.parametrize(
    ("key", "value", "expected_issue"),
    (
        (
            "CUSTOMBUILD_WEB_API_URL",
            "https://user:password@api.example.test",
            "web CUSTOMBUILD_WEB_API_URL must be an exact HTTPS origin",
        ),
        (
            "CUSTOMBUILD_WEB_API_URL",
            "https://api.example.test?tenant=other",
            "web CUSTOMBUILD_WEB_API_URL must be an exact HTTPS origin",
        ),
        (
            "CUSTOMBUILD_WEB_API_URL",
            "https://api.example.test#other",
            "web CUSTOMBUILD_WEB_API_URL must be an exact HTTPS origin",
        ),
        (
            "CUSTOMBUILD_WEB_API_URL",
            "https://api.example.test\n.evil.test",
            "web CUSTOMBUILD_WEB_API_URL must be an exact HTTPS origin",
        ),
        (
            "CUSTOMBUILD_WEB_API_URL",
            "\x00https://api.example.test",
            "web CUSTOMBUILD_WEB_API_URL must be an exact HTTPS origin",
        ),
        (
            "CUSTOMBUILD_WEB_API_URL",
            " https://api.example.test",
            "web CUSTOMBUILD_WEB_API_URL must be an exact HTTPS origin",
        ),
        (
            "CUSTOMBUILD_WEB_API_URL",
            "https://api.example.test\\evil.test",
            "web CUSTOMBUILD_WEB_API_URL must be an exact HTTPS origin",
        ),
        (
            "CUSTOMBUILD_WEB_OIDC_ISSUER",
            "https://user:password@identity.example.test/",
            "web CUSTOMBUILD_WEB_OIDC_ISSUER must be an HTTPS URL",
        ),
        (
            "CUSTOMBUILD_WEB_OIDC_ISSUER",
            "https://identity.example.test/?tenant=other",
            "web CUSTOMBUILD_WEB_OIDC_ISSUER must be an HTTPS URL",
        ),
        (
            "CUSTOMBUILD_WEB_OIDC_ISSUER",
            "https://identity.example.test/#other",
            "web CUSTOMBUILD_WEB_OIDC_ISSUER must be an HTTPS URL",
        ),
        (
            "CUSTOMBUILD_WEB_OIDC_ISSUER",
            "https://identity.example.test\t.evil.test",
            "web CUSTOMBUILD_WEB_OIDC_ISSUER must be an HTTPS URL",
        ),
        (
            "CUSTOMBUILD_WEB_OIDC_ISSUER",
            "https://identity.example.test\\evil.test",
            "web CUSTOMBUILD_WEB_OIDC_ISSUER must be an HTTPS URL",
        ),
        (
            "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI",
            "https://user:password@app.example.test/",
            "web CUSTOMBUILD_WEB_OIDC_REDIRECT_URI must be an HTTPS URL",
        ),
        (
            "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI",
            "https://app.example.test/?code=other",
            "web CUSTOMBUILD_WEB_OIDC_REDIRECT_URI must be an HTTPS URL",
        ),
        (
            "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI",
            "https://app.example.test/#callback",
            "web CUSTOMBUILD_WEB_OIDC_REDIRECT_URI must be an HTTPS URL",
        ),
        (
            "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI",
            "https://app.example.test/\r.evil.test",
            "web CUSTOMBUILD_WEB_OIDC_REDIRECT_URI must be an HTTPS URL",
        ),
        (
            "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI",
            "https://app.example.test\\evil.test",
            "web CUSTOMBUILD_WEB_OIDC_REDIRECT_URI must be an HTTPS URL",
        ),
    ),
)
def test_rejects_credentials_queries_and_fragments_in_public_web_urls(
    key: str,
    value: str,
    expected_issue: str,
) -> None:
    config = deepcopy(valid_config())
    web_env = config["services"]["web"]["environment"]
    web_env[key] = value

    issues = external_production_issues(config)

    assert expected_issue in issues
