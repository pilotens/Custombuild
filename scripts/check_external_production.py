"""Fail-closed validation for the external-production Compose overlay."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from scripts.source_manifest import build_source_manifest
except ModuleNotFoundError:  # Direct `python scripts/check_external_production.py` execution.
    from source_manifest import build_source_manifest  # type: ignore[import-not-found,no-redef]

INSECURE_MARKERS = ("change-me", "development", "demo-")
PRIVATE_PROXY_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _environment(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment")
    if isinstance(raw, dict):
        return {str(key): "" if value is None else str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        pairs: dict[str, str] = {}
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                pairs[key] = value
        return pairs
    return {}


def _https(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _normalized_https_url(value: str) -> str | None:
    if not _https(value):
        return None
    parsed = urlsplit(value)
    hostname = str(parsed.hostname).lower()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    netloc = rendered_host if port in {None, 443} else f"{rendered_host}:{port}"
    path = parsed.path.rstrip("/")
    return f"https://{netloc}{path}"


def _https_origin(value: str) -> str | None:
    normalized = _normalized_https_url(value)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    return f"https://{parsed.netloc}"


def _insecure(value: str) -> bool:
    lowered = value.lower()
    return not value or any(marker in lowered for marker in INSECURE_MARKERS)


def _insecure_secret(value: str, *, minimum_length: int = 24) -> bool:
    return _insecure(value) or len(value) < minimum_length or value != value.strip()


def _database_identity(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(value.replace("postgresql+psycopg://", "postgresql://", 1))
    except ValueError:
        return None
    if parsed.scheme != "postgresql" or not parsed.hostname:
        return None
    return parsed.username or "", parsed.password or ""


def external_production_issues(
    config: dict[str, Any],
    *,
    expected_dependency_lock_sha256: str | None = None,
    expected_frontend_lock_sha256: str | None = None,
    expected_source_manifest_sha256: str | None = None,
    expected_vcs_ref: str | None = None,
) -> list[str]:
    """Return secret-free deployment contract violations."""
    issues: list[str] = []
    services = _mapping(config.get("services"))
    required = {
        "postgres",
        "redis",
        "object-storage",
        "migrate",
        "api",
        "worker",
        "scheduler",
        "web",
    }
    missing = sorted(required - set(services))
    if missing:
        issues.append(f"required services are missing: {', '.join(missing)}")
        return issues

    for name in ("postgres", "migrate", "api", "worker", "scheduler"):
        if _environment(_mapping(services[name])).get("APP_ENV") != "production":
            issues.append(f"{name} does not run with APP_ENV=production")

    api_env = _environment(_mapping(services["api"]))
    for name in ("migrate", "api", "worker", "scheduler"):
        if (
            _environment(_mapping(services[name])).get("PRODUCTION_FOUR_EYES_REQUIRED")
            != "true"
        ):
            issues.append(f"{name} does not require four-eyes production approval")
    if api_env.get("AUTH_MODE") != "oidc":
        issues.append("api does not require OIDC authentication")
    for key in ("OIDC_ISSUER", "CORS_ORIGINS", "S3_PUBLIC_ENDPOINT"):
        values = [part.strip() for part in api_env.get(key, "").split(",") if part.strip()]
        if not values or any(not _https(value) for value in values):
            issues.append(f"api {key} must contain only HTTPS origins")
    trusted_proxy_values = [
        value.strip()
        for value in api_env.get("TRUSTED_PROXY_CIDRS", "").split(",")
        if value.strip()
    ]
    if not trusted_proxy_values:
        issues.append("api has no trusted TLS-proxy CIDR")
    else:
        try:
            for value in trusted_proxy_values:
                network = ipaddress.ip_network(value, strict=False)
                is_private_proxy = any(
                    network.version == allowed.version
                    and network.network_address in allowed
                    and network.broadcast_address in allowed
                    for allowed in PRIVATE_PROXY_NETWORKS
                )
                if not is_private_proxy:
                    raise ValueError("network is not a private proxy subnet")
        except ValueError:
            issues.append("api TRUSTED_PROXY_CIDRS must contain only private IP networks")

    secrets = {
        "postgres.POSTGRES_PASSWORD": _environment(_mapping(services["postgres"])).get(
            "POSTGRES_PASSWORD", ""
        ),
        "redis.REDIS_PASSWORD": _environment(_mapping(services["redis"])).get(
            "REDIS_PASSWORD", ""
        ),
        "object-storage.AWS_SECRET_ACCESS_KEY": _environment(
            _mapping(services["object-storage"])
        ).get("AWS_SECRET_ACCESS_KEY", ""),
        "api.ARTIFACT_SIGNING_SECRET": api_env.get("ARTIFACT_SIGNING_SECRET", ""),
    }
    postgres_env = _environment(_mapping(services["postgres"]))
    secrets.update(
        {
            "postgres.MIGRATOR_DATABASE_PASSWORD": postgres_env.get(
                "MIGRATOR_DATABASE_PASSWORD", ""
            ),
            "postgres.API_DATABASE_PASSWORD": postgres_env.get("API_DATABASE_PASSWORD", ""),
            "postgres.WORKER_DATABASE_PASSWORD": postgres_env.get(
                "WORKER_DATABASE_PASSWORD", ""
            ),
        }
    )
    for label, value in secrets.items():
        minimum_length = 32 if label == "api.ARTIFACT_SIGNING_SECRET" else 24
        if _insecure_secret(value, minimum_length=minimum_length):
            issues.append(f"{label} is missing, too short, or uses an insecure default")

    expected_postgres_roles = {
        "POSTGRES_USER": "custombuild_bootstrap",
        "MIGRATOR_DATABASE_USER": "custombuild_migrator",
    }
    for key, expected in expected_postgres_roles.items():
        if postgres_env.get(key) != expected:
            issues.append(f"postgres.{key} must be {expected}")

    expected_database_roles = {
        "migrate": "custombuild_migrator",
        "api": "custombuild_api",
        "worker": "custombuild_worker",
        "scheduler": "custombuild_worker",
    }
    for name, expected_role in expected_database_roles.items():
        env = _environment(_mapping(services[name]))
        database_identity = _database_identity(env.get("DATABASE_URL", ""))
        if database_identity is None or database_identity[0] != expected_role:
            issues.append(f"{name}.DATABASE_URL must use the fixed {expected_role} role")
        elif _insecure_secret(database_identity[1]):
            issues.append(f"{name}.DATABASE_URL password is missing, too short, or insecure")
        for key in ("REDIS_URL", "S3_SECRET_KEY"):
            environment_value = env.get(key)
            if environment_value is None:
                continue
            if key == "REDIS_URL":
                try:
                    parsed_redis = urlsplit(environment_value)
                    redis_password = parsed_redis.password or ""
                    redis_valid = parsed_redis.scheme in {"redis", "rediss"} and bool(
                        parsed_redis.hostname
                    )
                except ValueError:
                    redis_password = ""
                    redis_valid = False
                if not redis_valid or _insecure_secret(redis_password):
                    issues.append(f"{name}.{key} password is missing, too short, or insecure")
            elif _insecure_secret(environment_value):
                issues.append(f"{name}.{key} is missing, too short, or insecure")

    web_build = _mapping(_mapping(services["web"]).get("build"))
    web_args = _mapping(web_build.get("args"))
    if web_args.get("NEXT_PUBLIC_DEMO_TOKEN") not in ("", None):
        issues.append("web embeds a development demo token")
    for key in ("NEXT_PUBLIC_API_URL", "NEXT_PUBLIC_OIDC_ISSUER", "NEXT_PUBLIC_OIDC_REDIRECT_URI"):
        if not _https(str(web_args.get(key, ""))):
            issues.append(f"web {key} must be an HTTPS URL")
    if not str(web_args.get("NEXT_PUBLIC_OIDC_CLIENT_ID", "")):
        issues.append("web has no OIDC client id")

    api_issuer = _normalized_https_url(api_env.get("OIDC_ISSUER", ""))
    web_issuer = _normalized_https_url(str(web_args.get("NEXT_PUBLIC_OIDC_ISSUER", "")))
    if api_issuer is not None and web_issuer is not None and api_issuer != web_issuer:
        issues.append("API and web OIDC issuers do not match")

    redirect_value = str(web_args.get("NEXT_PUBLIC_OIDC_REDIRECT_URI", ""))
    redirect_origin = _https_origin(redirect_value)
    try:
        redirect_path = urlsplit(redirect_value).path
    except ValueError:
        redirect_path = ""
    if redirect_origin is not None and redirect_path not in {"", "/"}:
        issues.append("web OIDC callback must use the implemented root route")
    cors_origins = {
        origin
        for value in api_env.get("CORS_ORIGINS", "").split(",")
        if (origin := _https_origin(value.strip())) is not None
    }
    if redirect_origin is not None and redirect_origin not in cors_origins:
        issues.append("web OIDC callback origin is not an approved web origin")

    release_identities: dict[str, tuple[str, str, str, str, str]] = {}
    build_arguments: dict[str, dict[str, Any]] = {}
    for name in ("migrate", "api", "worker", "scheduler", "web"):
        build = _mapping(_mapping(services[name]).get("build"))
        arguments = _mapping(build.get("args"))
        build_arguments[name] = arguments
        version = str(arguments.get("APP_VERSION", ""))
        revision = str(arguments.get("VCS_REF", ""))
        created = str(arguments.get("BUILD_DATE", ""))
        source = str(arguments.get("SOURCE_URL", ""))
        source_manifest_sha256 = str(arguments.get("SOURCE_MANIFEST_SHA256", ""))
        release_identities[name] = (
            version,
            revision,
            created,
            source,
            source_manifest_sha256,
        )
        if _insecure(version) or any(
            marker in version.lower() for marker in ("dirty", "local", "uncommitted", "unknown")
        ):
            issues.append(f"{name} has no immutable application version")
        if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision):
            issues.append(f"{name} has no exact source revision")
        elif expected_vcs_ref is not None and revision != expected_vcs_ref:
            issues.append(f"{name} source revision does not match the checked Git HEAD")
        try:
            parsed_created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            parsed_created = None
        if (
            parsed_created is None
            or parsed_created.tzinfo is None
            or parsed_created.utcoffset() is None
        ):
            issues.append(f"{name} has no timezone-aware build timestamp")
        if not _https(source):
            issues.append(f"{name} has no HTTPS canonical source URL")
        if not re.fullmatch(r"[a-f0-9]{64}", source_manifest_sha256):
            issues.append(f"{name} has no exact source manifest SHA-256")
        elif (
            expected_source_manifest_sha256 is not None
            and source_manifest_sha256 != expected_source_manifest_sha256
        ):
            issues.append(f"{name} source manifest does not match the checked build/control set")
    for name in ("migrate", "api", "worker", "scheduler"):
        lock_sha256 = str(build_arguments[name].get("DEPENDENCY_LOCK_SHA256", ""))
        if not re.fullmatch(r"[a-f0-9]{64}", lock_sha256):
            issues.append(f"{name} has no exact uv.lock SHA-256")
        elif (
            expected_dependency_lock_sha256 is not None
            and lock_sha256 != expected_dependency_lock_sha256
        ):
            issues.append(f"{name} uv.lock SHA-256 does not match the checked source tree")

    frontend_lock_sha256 = str(build_arguments["web"].get("FRONTEND_LOCK_SHA256", ""))
    if not re.fullmatch(r"[a-f0-9]{64}", frontend_lock_sha256):
        issues.append("web has no exact pnpm-lock.yaml SHA-256")
    elif (
        expected_frontend_lock_sha256 is not None
        and frontend_lock_sha256 != expected_frontend_lock_sha256
    ):
        issues.append("web pnpm-lock.yaml SHA-256 does not match the checked source tree")

    if len(set(release_identities.values())) != 1:
        issues.append("application services do not share one exact release identity")

    for name in ("postgres", "redis"):
        if _mapping(services[name]).get("ports"):
            issues.append(f"{name} must not publish host ports")
    for name in ("api", "web", "object-storage"):
        for published in _mapping(services[name]).get("ports", []) or []:
            if (
                isinstance(published, dict)
                and published.get("host_ip") not in ("127.0.0.1", "::1")
            ) or (
                isinstance(published, str)
                and not published.startswith("127.0.0.1:")
            ):
                issues.append(f"{name} publishes a non-loopback host port")
    return issues


def render_compose(repo: Path) -> dict[str, Any]:
    command = [
        "docker",
        "compose",
        "-f",
        str(repo / "compose.yml"),
        "-f",
        str(repo / "compose.external-production.yml"),
        "config",
        "--format",
        "json",
    ]
    completed = subprocess.run(  # noqa: S603 - fixed Docker CLI and reviewed paths only.
        command,
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError("external-production Compose configuration could not be rendered")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("external-production Compose configuration is invalid")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        repo = arguments.repo.resolve()
        expected_lock_sha256 = hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest()
        expected_frontend_lock_sha256 = hashlib.sha256(
            (repo / "pnpm-lock.yaml").read_bytes()
        ).hexdigest()
        expected_source_manifest_sha256 = build_source_manifest(repo)[2]
        git = shutil.which("git")
        if not git:
            raise RuntimeError("Git CLI is not available")
        revision_process = subprocess.run(  # noqa: S603 - resolved Git and fixed argv.
            [git, "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if revision_process.returncode:
            raise RuntimeError("checked source Git revision could not be resolved")
        issues = external_production_issues(
            render_compose(repo),
            expected_dependency_lock_sha256=expected_lock_sha256,
            expected_frontend_lock_sha256=expected_frontend_lock_sha256,
            expected_source_manifest_sha256=expected_source_manifest_sha256,
            expected_vcs_ref=revision_process.stdout.strip(),
        )
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        result = {"status": "BLOCK", "issues": [str(exc)]}
    else:
        result = {"status": "PASS" if not issues else "BLOCK", "issues": issues}
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
