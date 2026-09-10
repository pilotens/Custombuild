#!/usr/bin/env python3
"""Validate external Internet-production evidence for a promoted Custombuild build.

Repository tests can prove application behavior. They cannot prove the state of
DNS, TLS, the identity provider, a secret manager, off-site backups, alert
routing or the target runtime. This verifier turns those external facts into an
explicit checksum-bound deployment gate without pretending to provision them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "custombuild.production-platform-evidence.v1"
SHA256 = re.compile(r"^[a-f0-9]{64}$")
REQUIRED_CONTROLS = (
    "PUBLIC_TLS",
    "OIDC_PKCE",
    "SECRET_MANAGER",
    "DATABASE_ENCRYPTION",
    "OBJECT_STORAGE_ENCRYPTION",
    "OFFSITE_BACKUP",
    "RESTORE_DRILL",
    "CENTRAL_LOGGING",
    "DISTRIBUTED_TRACING",
    "ALERT_DELIVERY",
    "RUNTIME_DIGEST_MATCH",
    "INGRESS_PROXY",
    "RATE_LIMIT_SOURCE_IP",
    "ROLLBACK_PROCEDURE",
    "INCIDENT_OWNER",
    "CAPACITY_TEST",
)
TOP_LEVEL_KEYS = {
    "schema_version",
    "environment_id",
    "git_revision",
    "deploy_descriptor_sha256",
    "verified_at",
    "verified_by",
    "controls",
}
CONTROL_KEYS = {
    "code",
    "evidence_id",
    "subject_sha256",
    "document_sha256",
    "issuer",
    "issued_at",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _nonblank(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a canonical non-blank string")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def validate(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != TOP_LEVEL_KEYS:
        raise ValueError("platform evidence has an unexpected top-level schema")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported platform evidence schema")
    _nonblank(payload["environment_id"], "environment_id")
    _digest(payload["git_revision"], "git_revision")
    _digest(payload["deploy_descriptor_sha256"], "deploy_descriptor_sha256")
    _nonblank(payload["verified_at"], "verified_at")
    _nonblank(payload["verified_by"], "verified_by")
    controls = payload["controls"]
    if not isinstance(controls, list):
        raise ValueError("controls must be an array")
    by_code: dict[str, dict[str, Any]] = {}
    evidence_ids: set[str] = set()
    for control in controls:
        if not isinstance(control, dict) or set(control) != CONTROL_KEYS:
            raise ValueError("platform control has an unexpected schema")
        code = _nonblank(control["code"], "control.code")
        if code in by_code:
            raise ValueError(f"duplicate platform control: {code}")
        if code not in REQUIRED_CONTROLS:
            raise ValueError(f"unknown platform control: {code}")
        evidence_id = _nonblank(control["evidence_id"], "control.evidence_id")
        if evidence_id in evidence_ids:
            raise ValueError("platform evidence IDs must be unique")
        evidence_ids.add(evidence_id)
        _digest(control["subject_sha256"], "control.subject_sha256")
        _digest(control["document_sha256"], "control.document_sha256")
        _nonblank(control["issuer"], "control.issuer")
        _nonblank(control["issued_at"], "control.issued_at")
        by_code[code] = control
    missing = sorted(set(REQUIRED_CONTROLS) - set(by_code))
    if missing:
        raise ValueError("missing production platform controls: " + ", ".join(missing))
    canonical = {
        **payload,
        "controls": [by_code[code] for code in sorted(by_code)],
    }
    fingerprint = hashlib.sha256(_canonical_bytes(canonical)).hexdigest()
    return {
        "status": "PASS",
        "schema_version": SCHEMA_VERSION,
        "environment_id": payload["environment_id"],
        "git_revision": payload["git_revision"],
        "deploy_descriptor_sha256": payload["deploy_descriptor_sha256"],
        "control_count": len(controls),
        "evidence_fingerprint": fingerprint,
        "internet_production_evidence_complete": True,
        "deployment_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--expect-git-revision")
    parser.add_argument("--expect-deploy-descriptor-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.evidence.read_text(encoding="utf-8"))
        result = validate(payload)
        if args.expect_git_revision and result["git_revision"] != args.expect_git_revision:
            raise ValueError("platform evidence is bound to another Git revision")
        if (
            args.expect_deploy_descriptor_sha256
            and result["deploy_descriptor_sha256"] != args.expect_deploy_descriptor_sha256
        ):
            raise ValueError("platform evidence is bound to another deployment descriptor")
        code = 0
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "status": "BLOCK",
            "internet_production_evidence_complete": False,
            "deployment_performed": False,
            "error": str(exc),
        }
        code = 1
    rendered = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
