from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.check_external_production import external_production_issues


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
    return {
        "services": {
            "postgres": {
                "environment": {
                    "APP_ENV": "production",
                    "POSTGRES_USER": "custombuild_bootstrap",
                    "POSTGRES_PASSWORD": "strong-postgres-bootstrap-secret",
                    "MIGRATOR_DATABASE_USER": "custombuild_migrator",
                    "MIGRATOR_DATABASE_PASSWORD": "strong-postgres-migrator-secret",
                    "API_DATABASE_PASSWORD": "strong-postgres-api-secret",
                    "WORKER_DATABASE_PASSWORD": "strong-postgres-worker-secret",
                }
            },
            "redis": {"environment": {"REDIS_PASSWORD": "strong-production-redis-secret"}},
            "object-storage": {
                "environment": {"AWS_SECRET_ACCESS_KEY": "strong-production-s3-secret"},
                "ports": [{"host_ip": "127.0.0.1", "published": "9000", "target": 8333}],
            },
            "migrate": {
                "build": common_build,
                "environment": {
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "DATABASE_URL": (
                        "postgresql://custombuild_migrator:strong-migrator-database-secret"
                        "@postgres/db"
                    ),
                },
            },
            "api": {
                "build": common_build,
                "environment": {
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "AUTH_MODE": "oidc",
                    "OIDC_ISSUER": "https://identity.example.test/",
                    "TRUSTED_PROXY_CIDRS": "172.20.0.0/24",
                    "CORS_ORIGINS": "https://app.example.test",
                    "S3_PUBLIC_ENDPOINT": "https://files.example.test",
                    "DATABASE_URL": (
                        "postgresql://custombuild_api:strong-api-database-secret@postgres/db"
                    ),
                    "REDIS_URL": "redis://:strong-production-redis-secret@redis:6379/0",
                    "S3_SECRET_KEY": "strong-production-s3-secret",
                    "ARTIFACT_SIGNING_SECRET": "strong-signing-secret-over-32-bytes",
                },
                "ports": [{"host_ip": "127.0.0.1", "published": "8000", "target": 8000}],
            },
            "worker": {
                "build": common_build,
                "environment": {
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "DATABASE_URL": (
                        "postgresql://custombuild_worker:strong-worker-database-secret"
                        "@postgres/db"
                    ),
                    "REDIS_URL": "redis://:strong-production-redis-secret@redis:6379/0",
                    "S3_SECRET_KEY": "strong-production-s3-secret",
                },
            },
            "scheduler": {
                "build": common_build,
                "environment": {
                    "APP_ENV": "production",
                    "PRODUCTION_FOUR_EYES_REQUIRED": "true",
                    "DATABASE_URL": (
                        "postgresql://custombuild_worker:strong-worker-database-secret"
                        "@postgres/db"
                    ),
                    "REDIS_URL": "redis://:strong-production-redis-secret@redis:6379/0",
                    "S3_SECRET_KEY": "strong-production-s3-secret",
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
        }
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
    config["services"]["web"]["environment"]["CUSTOMBUILD_WEB_DEMO_TOKEN"] = (
        "demo-owner"  # noqa: S105 - intentionally insecure negative fixture.
    )
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
        "api.ARTIFACT_SIGNING_SECRET is missing, too short, or uses an insecure default"
        in issues
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
    config["services"]["worker"]["build"] = deepcopy(
        config["services"]["worker"]["build"]
    )
    config["services"]["worker"]["build"]["args"]["DEPENDENCY_LOCK_SHA256"] = "unknown"
    config["services"]["web"]["build"]["args"]["FRONTEND_LOCK_SHA256"] = "unknown"
    config["services"]["scheduler"]["build"] = deepcopy(
        config["services"]["scheduler"]["build"]
    )
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
    config["services"]["postgres"]["environment"]["POSTGRES_USER"] = (
        "custombuild_migrator"
    )
    config["services"]["api"]["environment"]["DATABASE_URL"] = (
        "postgresql://custombuild_worker:short@postgres/db"
    )
    config["services"]["worker"]["environment"]["REDIS_URL"] = (
        "redis://:x@redis-with-a-very-long-hostname:6379/0"
    )
    config["services"]["object-storage"]["environment"]["AWS_SECRET_ACCESS_KEY"] = (
        "x"  # noqa: S105 - intentionally insecure negative fixture.
    )

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
