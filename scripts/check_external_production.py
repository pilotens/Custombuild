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
INSECURE_S3_ACCESS_KEYS = frozenset({"custombuild", "minioadmin"})
RAW_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
S3_ACCESS_KEY_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
S3_BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
VOLUME_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,254}\Z")
MAX_DATABASE_INTEGER = 2**63 - 1
CAPACITY_ENVIRONMENT_KEYS = (
    "STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256",
    "STORAGE_CAPACITY_VOLUME_IDENTITY",
    "STORAGE_CAPACITY_PROVISIONED_BYTES",
    "STORAGE_CAPACITY_METADATA_OVERHEAD_BYTES",
    "STORAGE_CAPACITY_EMERGENCY_RESERVE_BYTES",
    "STORAGE_CAPACITY_HEADROOM_BYTES",
    "STORAGE_CAPACITY_BYTE_LIMIT",
    "STORAGE_CAPACITY_OBJECT_LIMIT",
    "STORAGE_CAPACITY_DEPLOY_DESCRIPTOR_SHA256",
    "STORAGE_CAPACITY_MAX_AGE_SECONDS",
)
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


def _compose_tcp_port(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 65_535 else None
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,4}", value) is None:
        return None
    port = int(value)
    return port if port <= 65_535 else None


def _https(value: str) -> bool:
    if value != value.strip() or re.search(r"[\x00-\x1f\x7f]", value) or "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and "@" not in parsed.netloc
            and "?" not in value
            and "#" not in value
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
    return (
        _insecure(value)
        or len(value) < minimum_length
        or value != value.strip()
        or RAW_CONTROL_PATTERN.search(value) is not None
    )


def _canonical_s3_access_key(value: str) -> bool:
    return (
        value == value.strip()
        and RAW_CONTROL_PATTERN.search(value) is None
        and S3_ACCESS_KEY_PATTERN.fullmatch(value) is not None
        and value.lower() not in INSECURE_S3_ACCESS_KEYS
    )


def _canonical_s3_bucket(value: str) -> bool:
    try:
        ipv4_literal = ipaddress.ip_address(value).version == 4
    except ValueError:
        ipv4_literal = False
    return (
        value == value.strip()
        and RAW_CONTROL_PATTERN.search(value) is None
        and S3_BUCKET_PATTERN.fullmatch(value) is not None
        and ".." not in value
        and not ipv4_literal
    )


def _database_identity(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(value.replace("postgresql+psycopg://", "postgresql://", 1))
    except ValueError:
        return None
    if parsed.scheme != "postgresql" or not parsed.hostname:
        return None
    return parsed.username or "", parsed.password or ""


def _capacity_integer(value: str) -> int | None:
    if re.fullmatch(r"[1-9][0-9]{0,18}", value) is None:
        return None
    parsed = int(value)
    return parsed if parsed <= MAX_DATABASE_INTEGER else None


def _mount_at(service: dict[str, Any], target: str) -> dict[str, Any] | None:
    for mount in service.get("volumes", []) or []:
        if isinstance(mount, dict) and mount.get("target") == target:
            return mount
        if isinstance(mount, str):
            parts = mount.split(":")
            if len(parts) >= 2 and parts[1] == target:
                return {
                    "source": parts[0],
                    "target": parts[1],
                    "read_only": len(parts) >= 3 and parts[2] == "ro",
                    "type": "volume" if not parts[0].startswith(("/", ".")) else "bind",
                }
    return None


def _dependency_condition(service: dict[str, Any], dependency: str) -> str:
    value = _mapping(service.get("depends_on")).get(dependency)
    if not isinstance(value, dict) or value.get("required", True) is False:
        return ""
    return str(value.get("condition", ""))


def _dependency_restarts(service: dict[str, Any], dependency: str) -> bool:
    value = _mapping(service.get("depends_on")).get(dependency)
    return (
        isinstance(value, dict)
        and value.get("required", True) is not False
        and value.get("restart") is True
    )


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
        "storage-recovery",
        "storage-capacity-attestor",
        "api",
        "worker",
        "scheduler",
        "web",
    }
    missing = sorted(required - set(services))
    if missing:
        issues.append(f"required services are missing: {', '.join(missing)}")
        return issues

    for name in (
        "postgres",
        "migrate",
        "storage-recovery",
        "storage-capacity-attestor",
        "api",
        "worker",
        "scheduler",
        "web",
    ):
        if _environment(_mapping(services[name])).get("APP_ENV") != "production":
            issues.append(f"{name} does not run with APP_ENV=production")

    api_env = _environment(_mapping(services["api"]))
    for name in ("migrate", "storage-recovery", "api", "worker", "scheduler"):
        if _environment(_mapping(services[name])).get("PRODUCTION_FOUR_EYES_REQUIRED") != "true":
            issues.append(f"{name} does not require four-eyes production approval")
    if api_env.get("AUTH_MODE") != "oidc":
        issues.append("api does not require OIDC authentication")
    for key in ("OIDC_ISSUER", "CORS_ORIGINS"):
        values = [part.strip() for part in api_env.get(key, "").split(",") if part.strip()]
        if not values or any(not _https(value) for value in values):
            issues.append(f"api {key} must contain only HTTPS origins")
    storage_env = _environment(_mapping(services["object-storage"]))
    backup_endpoint = storage_env.get("S3_BACKUP_ENDPOINT", "")
    backup_binding: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int] | None = None
    try:
        parsed_backup = urlsplit(backup_endpoint)
        backup_host = ipaddress.ip_address(parsed_backup.hostname or "")
        backup_endpoint_valid = (
            backup_endpoint == backup_endpoint.strip()
            and re.search(r"[\\\x00-\x20\x7f]", backup_endpoint) is None
            and parsed_backup.scheme == "http"
            and backup_host.is_loopback
            and parsed_backup.username is None
            and parsed_backup.password is None
            and parsed_backup.port is not None
            and 1 <= parsed_backup.port <= 65_535
            and parsed_backup.path in {"", "/"}
            and not parsed_backup.query
            and not parsed_backup.fragment
        )
        if backup_endpoint_valid and parsed_backup.port is not None:
            backup_binding = (backup_host, parsed_backup.port)
    except ValueError:
        backup_endpoint_valid = False
    if not backup_endpoint_valid:
        issues.append("object-storage S3_BACKUP_ENDPOINT must be an explicit loopback URL")
    elif backup_binding is not None:
        port_matches = False
        for published in _mapping(services["object-storage"]).get("ports", []) or []:
            if not isinstance(published, dict):
                continue
            raw_published = published.get("published")
            raw_target = published.get("target")
            published_port = _compose_tcp_port(raw_published)
            target_port = _compose_tcp_port(raw_target)
            if published_port is None or target_port is None:
                continue
            try:
                published_host = ipaddress.ip_address(str(published.get("host_ip", "")))
            except ValueError:
                continue
            if (
                (published_host, published_port) == backup_binding
                and target_port == 8333
                and str(published.get("protocol", "tcp")).lower() == "tcp"
            ):
                port_matches = True
                break
        if not port_matches:
            issues.append("object-storage S3_BACKUP_ENDPOINT does not match its loopback S3 port")
    canonical_s3_endpoint = "http://object-storage:8333"
    storage_s3_identity = {
        "S3_ACCESS_KEY": storage_env.get("AWS_ACCESS_KEY_ID", ""),
        "S3_SECRET_KEY": storage_env.get("AWS_SECRET_ACCESS_KEY", ""),
        "S3_BUCKET": storage_env.get("S3_BUCKET", ""),
    }
    if not _canonical_s3_access_key(storage_s3_identity["S3_ACCESS_KEY"]):
        issues.append("object-storage.AWS_ACCESS_KEY_ID must be a canonical header-safe access key")
    if not _canonical_s3_bucket(storage_s3_identity["S3_BUCKET"]):
        issues.append("object-storage.S3_BUCKET must be a canonical S3 DNS name")
    for name in (
        "storage-recovery",
        "storage-capacity-attestor",
        "api",
        "worker",
        "scheduler",
    ):
        service_env = _environment(_mapping(services[name]))
        if service_env.get("S3_ENDPOINT") != canonical_s3_endpoint:
            issues.append(f"{name} S3_ENDPOINT must be exactly {canonical_s3_endpoint}")
        for key, expected_value in storage_s3_identity.items():
            if not expected_value or service_env.get(key) != expected_value:
                issues.append(f"{name} {key} does not match object-storage")

    postgres = _mapping(services["postgres"])
    postgres_env = _environment(postgres)
    migrate = _mapping(services["migrate"])
    migrate_env = _environment(migrate)
    if str(postgres.get("restart", "")) != "no":
        issues.append("postgres automatic restart can bypass the storage-recovery barrier")
    if _dependency_condition(migrate, "postgres") != "service_healthy":
        issues.append("migrate does not wait for healthy PostgreSQL")
    if not _dependency_restarts(migrate, "postgres"):
        issues.append("migrate does not restart after a Compose-managed PostgreSQL update")
    api_image = str(_mapping(services["api"]).get("image", ""))
    recovery = _mapping(services["storage-recovery"])
    recovery_env = _environment(recovery)
    recovery_command = recovery.get("command")
    if (
        not isinstance(recovery_command, list)
        or len(recovery_command) < 3
        or recovery_command[:3] != ["python", "-m", "scripts.storage_recovery"]
        or recovery.get("entrypoint")
    ):
        issues.append("storage-recovery does not run the fixed one-shot command")
    recovery_image = str(recovery.get("image", ""))
    if re.fullmatch(r"[^@\s]+@sha256:[a-f0-9]{64}", recovery_image) is None:
        issues.append("storage-recovery image is not digest pinned")
    if api_image and recovery_image != api_image:
        issues.append("storage-recovery does not use the exact API image")
    if str(recovery.get("user", "")) != "65532:65532":
        issues.append("storage-recovery must run as the fixed non-root user")
    if recovery.get("read_only") is not True:
        issues.append("storage-recovery root filesystem is not read-only")
    if "ALL" not in (recovery.get("cap_drop") or []):
        issues.append("storage-recovery does not drop all Linux capabilities")
    if recovery.get("cap_add"):
        issues.append("storage-recovery adds Linux capabilities")
    if recovery.get("privileged") is True:
        issues.append("storage-recovery is privileged")
    if "no-new-privileges:true" not in (recovery.get("security_opt") or []):
        issues.append("storage-recovery does not enforce no-new-privileges")
    recovery_pids_limit = recovery.get("pids_limit")
    if (
        isinstance(recovery_pids_limit, bool)
        or not isinstance(recovery_pids_limit, int)
        or recovery_pids_limit < 1
    ):
        issues.append("storage-recovery has no positive PID limit")
    recovery_networks = recovery.get("networks") or {}
    recovery_network_names = (
        set(recovery_networks) if isinstance(recovery_networks, dict | list) else set()
    )
    if recovery_network_names != {"backend"}:
        issues.append("storage-recovery must attach only to the backend network")
    if str(recovery.get("restart", "")) != "no":
        issues.append("storage-recovery is not an explicit one-shot service")
    if recovery.get("volumes"):
        issues.append("storage-recovery must not mount storage volumes")
    if recovery.get("ports"):
        issues.append("storage-recovery must not publish host ports")
    if _dependency_condition(recovery, "migrate") != "service_completed_successfully":
        issues.append("storage-recovery does not wait for completed migrations")
    if not _dependency_restarts(recovery, "migrate"):
        issues.append("storage-recovery does not restart after a Compose-managed migration")
    if _dependency_condition(recovery, "object-storage") != "service_healthy":
        issues.append("storage-recovery does not wait for healthy object storage")
    recovery_database_url = recovery_env.get("DATABASE_URL", "")
    recovery_database_identity = _database_identity(recovery_database_url)
    if (
        recovery_database_identity is None
        or recovery_database_identity[0] != "custombuild_migrator"
    ):
        issues.append("storage-recovery.DATABASE_URL must use the fixed custombuild_migrator role")
    elif _insecure_secret(recovery_database_identity[1]):
        issues.append("storage-recovery.DATABASE_URL password is missing, too short, or insecure")
    elif recovery_database_identity[1] != postgres_env.get("MIGRATOR_DATABASE_PASSWORD", ""):
        issues.append(
            "storage-recovery.DATABASE_URL password does not match the provisioned migrator secret"
        )
    if recovery_database_url != migrate_env.get("DATABASE_URL", ""):
        issues.append("storage-recovery.DATABASE_URL does not exactly match migrate")

    attestor = _mapping(services["storage-capacity-attestor"])
    attestor_env = _environment(attestor)
    attestor_command = attestor.get("command")
    if (
        not isinstance(attestor_command, list)
        or len(attestor_command) < 3
        or attestor_command[:3] != ["python", "-m", "scripts.storage_capacity_preflight"]
        or "scripts.storage_capacity_development" in attestor_command
    ):
        issues.append("storage-capacity-attestor does not run the strict preflight")
    attestor_image = str(attestor.get("image", ""))
    if re.fullmatch(r"[^@\s]+@sha256:[a-f0-9]{64}", attestor_image) is None:
        issues.append("storage-capacity-attestor image is not digest pinned")
    if api_image and attestor_image != api_image:
        issues.append("storage-capacity-attestor does not use the exact API image")
    if str(attestor.get("user", "")) in {"", "0", "0:0", "root"}:
        issues.append("storage-capacity-attestor must run as an explicit non-root user")
    if attestor.get("read_only") is not True:
        issues.append("storage-capacity-attestor root filesystem is not read-only")
    if "ALL" not in (attestor.get("cap_drop") or []):
        issues.append("storage-capacity-attestor does not drop all Linux capabilities")
    if "no-new-privileges:true" not in (attestor.get("security_opt") or []):
        issues.append("storage-capacity-attestor does not enforce no-new-privileges")
    pids_limit = attestor.get("pids_limit")
    if isinstance(pids_limit, bool) or not isinstance(pids_limit, int) or pids_limit < 1:
        issues.append("storage-capacity-attestor has no positive PID limit")
    raw_networks = attestor.get("networks") or {}
    network_names = set(raw_networks) if isinstance(raw_networks, dict | list) else set()
    if network_names != {"backend"}:
        issues.append("storage-capacity-attestor must attach only to the backend network")
    healthcheck = _mapping(attestor.get("healthcheck"))
    if "capacity-heartbeat.json" not in " ".join(
        str(value) for value in healthcheck.get("test", []) or []
    ):
        issues.append("storage-capacity-attestor health does not verify its heartbeat")
    if _dependency_condition(attestor, "storage-recovery") != "service_completed_successfully":
        issues.append("storage-capacity-attestor does not wait for completed storage recovery")
    if not _dependency_restarts(attestor, "storage-recovery"):
        issues.append(
            "storage-capacity-attestor does not restart after Compose-managed storage recovery"
        )
    if _dependency_condition(attestor, "object-storage") != "service_healthy":
        issues.append("storage-capacity-attestor does not wait for healthy object storage")
    storage_mount = _mount_at(attestor, "/storage-volume")
    if (
        storage_mount is None
        or storage_mount.get("type") != "volume"
        or storage_mount.get("source") != "object-storage-data"
        or storage_mount.get("read_only") is not True
    ):
        issues.append("storage-capacity-attestor does not read the exact storage volume")
    evidence_mount = _mount_at(attestor, "/evidence")
    if evidence_mount is None or evidence_mount.get("type") != "bind":
        issues.append("storage-capacity-attestor evidence is not durably bind-mounted")

    attestor_database_identity = _database_identity(attestor_env.get("DATABASE_URL", ""))
    if (
        attestor_database_identity is None
        or attestor_database_identity[0] != "custombuild_storage_attestor"
    ):
        issues.append(
            "storage-capacity-attestor.DATABASE_URL must use the fixed "
            "custombuild_storage_attestor role"
        )
    elif _insecure_secret(attestor_database_identity[1]):
        issues.append(
            "storage-capacity-attestor.DATABASE_URL password is missing, too short, or insecure"
        )
    elif attestor_database_identity[1] != postgres_env.get(
        "CAPACITY_ATTESTOR_DATABASE_PASSWORD", ""
    ):
        issues.append(
            "storage-capacity-attestor.DATABASE_URL password does not match the "
            "provisioned storage-attestor secret"
        )

    capacity_environments = {
        name: _environment(_mapping(services[name]))
        for name in ("storage-capacity-attestor", "api", "worker", "scheduler")
    }
    for key in CAPACITY_ENVIRONMENT_KEYS:
        capacity_values = {
            environment.get(key, "") for environment in capacity_environments.values()
        }
        if len(capacity_values) != 1:
            issues.append(f"storage capacity field {key} differs between writers and attestor")
    if (
        SHA256_PATTERN.fullmatch(attestor_env.get("STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256", ""))
        is None
    ):
        issues.append("storage capacity operator-config SHA-256 is invalid")
    if (
        SHA256_PATTERN.fullmatch(attestor_env.get("STORAGE_CAPACITY_DEPLOY_DESCRIPTOR_SHA256", ""))
        is None
    ):
        issues.append("storage capacity deploy-descriptor SHA-256 is invalid")
    volume_identity = attestor_env.get("STORAGE_CAPACITY_VOLUME_IDENTITY", "")
    if VOLUME_IDENTITY_PATTERN.fullmatch(volume_identity) is None:
        issues.append("storage capacity volume identity is invalid")
    if attestor_env.get("OBJECT_STORAGE_VOLUME_NAME") != volume_identity:
        issues.append("storage capacity volume identity does not match the mounted volume")
    volume_definition = _mapping(_mapping(config.get("volumes")).get("object-storage-data"))
    if (
        volume_definition.get("external") is not True
        or volume_definition.get("name") != volume_identity
    ):
        issues.append("object storage is not bound to the exact external volume identity")
    if attestor_env.get("STORAGE_CAPACITY_MAX_AGE_SECONDS") != "600":
        issues.append("storage capacity maximum age must be exactly 600 seconds")
    capacity_numbers = {
        key: _capacity_integer(attestor_env.get(key, ""))
        for key in (
            "STORAGE_CAPACITY_PROVISIONED_BYTES",
            "STORAGE_CAPACITY_METADATA_OVERHEAD_BYTES",
            "STORAGE_CAPACITY_EMERGENCY_RESERVE_BYTES",
            "STORAGE_CAPACITY_HEADROOM_BYTES",
            "STORAGE_CAPACITY_BYTE_LIMIT",
            "STORAGE_CAPACITY_OBJECT_LIMIT",
        )
    }
    if any(value is None for value in capacity_numbers.values()):
        issues.append("storage capacity limits must be positive database integers")
    else:
        provisioned = capacity_numbers["STORAGE_CAPACITY_PROVISIONED_BYTES"]
        metadata = capacity_numbers["STORAGE_CAPACITY_METADATA_OVERHEAD_BYTES"]
        emergency = capacity_numbers["STORAGE_CAPACITY_EMERGENCY_RESERVE_BYTES"]
        headroom = capacity_numbers["STORAGE_CAPACITY_HEADROOM_BYTES"]
        byte_limit = capacity_numbers["STORAGE_CAPACITY_BYTE_LIMIT"]
        assert provisioned is not None
        assert metadata is not None
        assert emergency is not None
        assert headroom is not None
        assert byte_limit is not None
        if headroom != metadata + emergency:
            issues.append("storage capacity headroom does not equal its reserved components")
        if headroom >= provisioned or byte_limit > provisioned - headroom:
            issues.append("storage capacity logical limit exceeds physical usable capacity")
    for name in ("api", "worker", "scheduler"):
        service = _mapping(services[name])
        if _dependency_condition(service, "storage-capacity-attestor") != "service_healthy":
            issues.append(f"{name} does not wait for healthy storage capacity evidence")
        if not _dependency_restarts(service, "storage-capacity-attestor"):
            issues.append(f"{name} does not restart after Compose-managed capacity attestation")
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
        "redis.REDIS_PASSWORD": _environment(_mapping(services["redis"])).get("REDIS_PASSWORD", ""),
        "object-storage.AWS_SECRET_ACCESS_KEY": _environment(
            _mapping(services["object-storage"])
        ).get("AWS_SECRET_ACCESS_KEY", ""),
        "api.ARTIFACT_SIGNING_SECRET": api_env.get("ARTIFACT_SIGNING_SECRET", ""),
    }
    secrets.update(
        {
            "postgres.MIGRATOR_DATABASE_PASSWORD": postgres_env.get(
                "MIGRATOR_DATABASE_PASSWORD", ""
            ),
            "postgres.API_DATABASE_PASSWORD": postgres_env.get("API_DATABASE_PASSWORD", ""),
            "postgres.WORKER_DATABASE_PASSWORD": postgres_env.get("WORKER_DATABASE_PASSWORD", ""),
            "postgres.CAPACITY_ATTESTOR_DATABASE_PASSWORD": postgres_env.get(
                "CAPACITY_ATTESTOR_DATABASE_PASSWORD", ""
            ),
        }
    )
    for label, value in secrets.items():
        minimum_length = 32 if label == "api.ARTIFACT_SIGNING_SECRET" else 24
        if _insecure_secret(value, minimum_length=minimum_length):
            issues.append(f"{label} is missing, too short, or uses an insecure default")
    attestor_password = postgres_env.get("CAPACITY_ATTESTOR_DATABASE_PASSWORD", "")
    if attestor_password and attestor_password in {
        postgres_env.get("POSTGRES_PASSWORD", ""),
        postgres_env.get("MIGRATOR_DATABASE_PASSWORD", ""),
        postgres_env.get("API_DATABASE_PASSWORD", ""),
        postgres_env.get("WORKER_DATABASE_PASSWORD", ""),
    }:
        issues.append(
            "postgres.CAPACITY_ATTESTOR_DATABASE_PASSWORD must be unique to the "
            "storage-attestor role"
        )

    expected_postgres_roles = {
        "POSTGRES_USER": "custombuild_bootstrap",
        "MIGRATOR_DATABASE_USER": "custombuild_migrator",
        "CAPACITY_ATTESTOR_DATABASE_USER": "custombuild_storage_attestor",
    }
    for key, expected in expected_postgres_roles.items():
        if postgres_env.get(key) != expected:
            issues.append(f"postgres.{key} must be {expected}")

    expected_database_roles = {
        "migrate": "custombuild_migrator",
        "storage-recovery": "custombuild_migrator",
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

    web_service = _mapping(services["web"])
    web_build = _mapping(web_service.get("build"))
    web_args = _mapping(web_build.get("args"))
    web_env = _environment(web_service)
    runtime_keys = {
        "CUSTOMBUILD_WEB_API_URL",
        "CUSTOMBUILD_WEB_DEMO_TOKEN",
        "CUSTOMBUILD_WEB_OIDC_ISSUER",
        "CUSTOMBUILD_WEB_OIDC_CLIENT_ID",
        "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI",
    }
    legacy_build_keys = {
        "NEXT_PUBLIC_API_URL",
        "NEXT_PUBLIC_DEMO_TOKEN",
        "NEXT_PUBLIC_OIDC_ISSUER",
        "NEXT_PUBLIC_OIDC_CLIENT_ID",
        "NEXT_PUBLIC_OIDC_REDIRECT_URI",
    }
    if runtime_keys.intersection(web_args) or legacy_build_keys.intersection(web_args):
        issues.append("web public runtime configuration is baked into image build arguments")
    if legacy_build_keys.intersection(web_env):
        issues.append("web uses legacy NEXT_PUBLIC runtime variables")
    if web_env.get("CUSTOMBUILD_WEB_DEMO_TOKEN") not in ("", None):
        issues.append("web exposes a development demo token in production")

    web_api_url = web_env.get("CUSTOMBUILD_WEB_API_URL", "")
    try:
        web_api_path = urlsplit(web_api_url).path
    except ValueError:
        web_api_path = "invalid"
    if not _https(web_api_url) or web_api_path not in {"", "/"}:
        issues.append("web CUSTOMBUILD_WEB_API_URL must be an exact HTTPS origin")
    for key in ("CUSTOMBUILD_WEB_OIDC_ISSUER", "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI"):
        if not _https(web_env.get(key, "")):
            issues.append(f"web {key} must be an HTTPS URL")
    web_client_id = web_env.get("CUSTOMBUILD_WEB_OIDC_CLIENT_ID", "")
    if not web_client_id or web_client_id != web_client_id.strip():
        issues.append("web has no OIDC client id")

    api_issuer = _normalized_https_url(api_env.get("OIDC_ISSUER", ""))
    web_issuer = _normalized_https_url(web_env.get("CUSTOMBUILD_WEB_OIDC_ISSUER", ""))
    if api_issuer is not None and web_issuer is not None and api_issuer != web_issuer:
        issues.append("API and web OIDC issuers do not match")

    redirect_value = web_env.get("CUSTOMBUILD_WEB_OIDC_REDIRECT_URI", "")
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
    for name in (
        "migrate",
        "storage-recovery",
        "api",
        "worker",
        "scheduler",
        "web",
    ):
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
    for name in ("migrate", "storage-recovery", "api", "worker", "scheduler"):
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

    for name in ("postgres", "redis", "storage-recovery"):
        if _mapping(services[name]).get("ports"):
            issues.append(f"{name} must not publish host ports")
    for name in ("api", "web", "object-storage"):
        for published in _mapping(services[name]).get("ports", []) or []:
            if (
                isinstance(published, dict) and published.get("host_ip") not in ("127.0.0.1", "::1")
            ) or (isinstance(published, str) and not published.startswith("127.0.0.1:")):
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
