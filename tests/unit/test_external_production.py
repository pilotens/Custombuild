from __future__ import annotations

from copy import deepcopy

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
                    "POSTGRES_PASSWORD": "strong-postgres-secret",
                }
            },
            "redis": {"environment": {"REDIS_PASSWORD": "strong-redis-secret"}},
            "object-storage": {
                "environment": {"AWS_SECRET_ACCESS_KEY": "strong-s3-secret"},
                "ports": [{"host_ip": "127.0.0.1", "published": "9000", "target": 8333}],
            },
            "migrate": {
                "build": common_build,
                "environment": {
                    "APP_ENV": "production",
                    "DATABASE_URL": "postgresql://migrator:strong@postgres/db",
                },
            },
            "api": {
                "build": common_build,
                "environment": {
                    "APP_ENV": "production",
                    "AUTH_MODE": "oidc",
                    "OIDC_ISSUER": "https://identity.example.test/",
                    "TRUSTED_PROXY_CIDRS": "172.20.0.0/24",
                    "CORS_ORIGINS": "https://app.example.test",
                    "S3_PUBLIC_ENDPOINT": "https://files.example.test",
                    "DATABASE_URL": "postgresql://api:strong@postgres/db",
                    "REDIS_URL": "redis://:strong@redis:6379/0",
                    "S3_SECRET_KEY": "strong-s3-secret",
                    "ARTIFACT_SIGNING_SECRET": "strong-signing-secret-over-32-bytes",
                },
                "ports": [{"host_ip": "127.0.0.1", "published": "8000", "target": 8000}],
            },
            "worker": {
                "build": common_build,
                "environment": {
                    "APP_ENV": "production",
                    "DATABASE_URL": "postgresql://worker:strong@postgres/db",
                    "REDIS_URL": "redis://:strong@redis:6379/0",
                    "S3_SECRET_KEY": "strong-s3-secret",
                },
            },
            "scheduler": {
                "build": common_build,
                "environment": {
                    "APP_ENV": "production",
                    "DATABASE_URL": "postgresql://worker:strong@postgres/db",
                    "REDIS_URL": "redis://:strong@redis:6379/0",
                    "S3_SECRET_KEY": "strong-s3-secret",
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
                        "NEXT_PUBLIC_API_URL": "https://api.example.test",
                        "NEXT_PUBLIC_DEMO_TOKEN": "",
                        "NEXT_PUBLIC_OIDC_ISSUER": "https://identity.example.test/",
                        "NEXT_PUBLIC_OIDC_CLIENT_ID": "custombuild-web",
                        "NEXT_PUBLIC_OIDC_REDIRECT_URI": "https://app.example.test/",
                    }
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
    config["services"]["web"]["build"]["args"]["NEXT_PUBLIC_DEMO_TOKEN"] = (
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
    assert "api.ARTIFACT_SIGNING_SECRET is missing or uses an insecure default" in issues
    assert "web embeds a development demo token" in issues
    assert "api has no exact source revision" in issues
    assert "api has no timezone-aware build timestamp" in issues
    assert "api has no HTTPS canonical source URL" in issues
    assert "postgres must not publish host ports" in issues


def test_rejects_oidc_issuer_drift_and_an_unimplemented_callback_route() -> None:
    config = deepcopy(valid_config())
    web_args = config["services"]["web"]["build"]["args"]
    web_args["NEXT_PUBLIC_OIDC_ISSUER"] = "https://other-identity.example.test/"
    web_args["NEXT_PUBLIC_OIDC_REDIRECT_URI"] = "https://app.example.test/callback"

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
    web_args = config["services"]["web"]["build"]["args"]
    web_args["NEXT_PUBLIC_OIDC_ISSUER"] = "https://identity.example.test"
    web_args["NEXT_PUBLIC_OIDC_REDIRECT_URI"] = "https://other-app.example.test/"

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
    assert "api source manifest does not match the checked build context" in issues
    assert "api source revision does not match the checked Git HEAD" in issues


def test_rejects_non_loopback_publication_and_missing_service() -> None:
    config = valid_config()
    config["services"]["api"]["ports"][0]["host_ip"] = "0.0.0.0"  # noqa: S104
    del config["services"]["scheduler"]

    issues = external_production_issues(config)

    assert issues == ["required services are missing: scheduler"]
