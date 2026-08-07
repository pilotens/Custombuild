"""Live Compose acceptance for the complete bookcase production vertical.

This script deliberately uses only Python's standard library and public HTTP
endpoints. It must be run against the real Compose API, worker, PostgreSQL,
Redis and object storage; it never mutates the database or queue directly.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

NORDIC_TOKEN: Final = "demo-nordic-owner"  # noqa: S105 - documented non-secret dev token
ATELIER_TOKEN: Final = "demo-atelier-owner"  # noqa: S105 - documented non-secret dev token
NORDIC_ORGANIZATION_ID: Final = "11111111-1111-4111-8111-111111111111"
ATELIER_ORGANIZATION_ID: Final = "22222222-2222-4222-8222-222222222222"
CONTEXT_HASH_FIELDS: Final = (
    "project_id",
    "revision",
    "design_hash",
    "app_version",
    "engine_version",
    "template_version",
    "rule_version",
    "material_versions",
    "joint_version",
    "machine_profile",
    "postprocessor_version",
    "generation_context_hash",
    "production_engine_context",
    "artifact_schema_version",
    "cad_status",
    "approved_assumptions",
    "warnings",
    "overrides",
    "artifacts",
)
REQUIRED_PACKAGE_PATHS: Final = frozenset(
    {
        "assembly/assembly-manual.pdf",
        "bom/bom.csv",
        "bom/bom.pdf",
        "bom/hardware-list.csv",
        "cam/operations.json",
        "cam/tool-list.csv",
        "cam/validation-backplot.svg",
        "cut-list/cut-list.csv",
        "labels/part-labels.pdf",
        "materials/material-list.csv",
        "model/design.glb",
        "model/design.step",
        "qa/measurement-protocol.pdf",
        "validation/construction-report.json",
        "validation/construction-report.pdf",
        "validation/dfm-report.json",
    }
)
MAX_ZIP_FILES: Final = 10_000
MAX_ZIP_ENTRY_BYTES: Final = 512 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES: Final = 2 * 1024 * 1024 * 1024
_MISSING: Final = object()


class AcceptanceFailure(RuntimeError):
    """A failed live acceptance invariant."""


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class HttpClient:
    def __init__(self, base_url: str, token: str | None, request_timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        require(parsed.scheme in {"http", "https"} and bool(parsed.netloc), "invalid base URL")
        self.token = token
        self.request_timeout = request_timeout
        self._opener = build_opener()
        self._no_redirect_opener = build_opener(NoRedirectHandler())

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = _MISSING,
        expected: tuple[int, ...] = (200,),
        follow_redirects: bool = True,
    ) -> HttpResult:
        target = urljoin(f"{self.base_url}/", path.lstrip("/"))
        headers = {"Accept": "application/json", "User-Agent": "custombuild-live-acceptance/1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data: bytes | None = None
        if payload is not _MISSING:
            data = canonical_json_bytes(payload)
            headers["Content-Type"] = "application/json"
        request = Request(  # noqa: S310 - constructor receives a validated HTTP(S) base URL
            target, data=data, headers=headers, method=method
        )
        opener = self._opener if follow_redirects else self._no_redirect_opener
        try:
            with opener.open(request, timeout=self.request_timeout) as response:  # noqa: S310
                result = HttpResult(
                    response.status,
                    response.geturl(),
                    {key.lower(): value for key, value in response.headers.items()},
                    response.read(),
                )
        except HTTPError as exc:
            result = HttpResult(
                exc.code,
                exc.geturl(),
                {key.lower(): value for key, value in exc.headers.items()},
                exc.read(),
            )
        except (OSError, TimeoutError, URLError) as exc:
            raise AcceptanceFailure(f"{method} {target} failed: {exc}") from exc
        if result.status not in expected:
            detail = result.body.decode("utf-8", errors="replace")[:2_000]
            raise AcceptanceFailure(
                f"{method} {target} returned {result.status}, expected {expected}: {detail}"
            )
        return result

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: object = _MISSING,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        result = self.request(method, path, payload=payload, expected=expected)
        try:
            return json.loads(result.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcceptanceFailure(f"{method} {path} returned invalid JSON") from exc


def require(condition: object, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def mapping(value: Any, label: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{label} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def sequence(value: Any, label: str) -> list[Any]:
    require(isinstance(value, list), f"{label} must be a JSON array")
    return value


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def require_sha256(value: Any, label: str) -> str:
    require(isinstance(value, str), f"{label} must be a string")
    require(re.fullmatch(r"[0-9a-f]{64}", value) is not None, f"{label} is not SHA-256")
    return value


def log(event: str, **values: object) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=False, sort_keys=True), flush=True)


def wait_for_api(client: HttpClient, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "API did not answer"
    while time.monotonic() < deadline:
        try:
            result = client.request("GET", "/health", expected=tuple(range(200, 600)))
            if result.status == 200:
                health = mapping(json.loads(result.body), "health")
                require(health.get("status") == "ok", "health endpoint did not report ok")
                log("api_ready", version=health.get("version"))
                return
            last_error = f"health returned {result.status}"
        except (AcceptanceFailure, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise AcceptanceFailure(f"API readiness timed out: {last_error}")


def wait_for_job(
    client: HttpClient,
    job_id: str,
    *,
    timeout_seconds: float,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    previous_status: object = None
    while time.monotonic() < deadline:
        job = mapping(client.json("GET", f"/v1/jobs/{job_id}"), "generation job")
        status = job.get("status")
        if status != previous_status:
            log("job_status", job_id=job_id, status=status, attempts=job.get("attempts"))
            previous_status = status
        if status == "succeeded":
            require(isinstance(job.get("result_json"), dict), "successful job has no result_json")
            require(int(job.get("attempts", 0)) >= 1, "worker never claimed the generation job")
            return job
        if status in {"failed", "cancelled"}:
            raise AcceptanceFailure(f"generation job ended as {status}: {job.get('error')}")
        require(status in {"queued", "running"}, f"unknown generation status: {status}")
        time.sleep(poll_interval)
    raise AcceptanceFailure(f"generation job {job_id} timed out after {timeout_seconds:g}s")


def _safe_zip_path(path: str) -> None:
    require(path and "\\" not in path and "\x00" not in path, f"unsafe ZIP path: {path!r}")
    require(not PurePosixPath(path).is_absolute(), f"absolute ZIP path: {path}")
    require(
        all(part not in {"", ".", ".."} for part in path.split("/")),
        f"unsafe ZIP path: {path}",
    )


def verify_package(
    bundle: bytes,
    standalone_manifest: bytes,
    *,
    project_id: str,
    revision: int,
    design_hash: str,
    required_part_id: str | None = None,
) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle), mode="r")
    except zipfile.BadZipFile as exc:
        raise AcceptanceFailure("production_bundle is not a ZIP file") from exc
    with archive:
        infos = archive.infolist()
        require(0 < len(infos) <= MAX_ZIP_FILES, "unsafe production ZIP file count")
        total_size = 0
        for info in infos:
            _safe_zip_path(info.filename)
            require(not info.is_dir(), f"ZIP contains a directory: {info.filename}")
            require(not info.flag_bits & 0x1, f"ZIP contains encryption: {info.filename}")
            require(info.file_size <= MAX_ZIP_ENTRY_BYTES, f"ZIP entry too large: {info.filename}")
            total_size += info.file_size
            require(total_size <= MAX_ZIP_UNCOMPRESSED_BYTES, "ZIP expands beyond safety limit")
            if info.file_size:
                require(info.compress_size > 0, f"invalid compression size: {info.filename}")
                require(
                    info.file_size / info.compress_size <= 1_000,
                    f"unsafe compression ratio: {info.filename}",
                )
        names = [info.filename for info in infos]
        require(len(names) == len(set(names)), "ZIP contains duplicate paths")
        try:
            zipped_manifest = archive.read("manifest.json")
        except KeyError as exc:
            raise AcceptanceFailure("ZIP has no manifest.json") from exc
        require(zipped_manifest == standalone_manifest, "ZIP and object-store manifests differ")
        try:
            manifest = mapping(json.loads(zipped_manifest), "manifest")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcceptanceFailure("manifest.json is invalid JSON") from exc
        require(
            manifest.get("schema_version") == "custombuild.production-manifest.v1",
            "unsupported manifest schema",
        )
        require(manifest.get("project_id") == project_id, "manifest project_id mismatch")
        require(manifest.get("revision") == str(revision), "manifest revision mismatch")
        require(manifest.get("design_hash") == design_hash, "manifest design_hash mismatch")
        require(manifest.get("cad_status") == "GENERATED", "authoritative CAD was not generated")

        raw_entries = sequence(manifest.get("artifacts"), "manifest artifacts")
        entries = [mapping(item, "manifest artifact") for item in raw_entries]
        paths = [str(item.get("path")) for item in entries]
        require(len(paths) == len(set(paths)), "manifest contains duplicate artifact paths")
        require(set(names) == {"manifest.json", *paths}, "ZIP content differs from manifest")
        require(set(paths) >= REQUIRED_PACKAGE_PATHS, "production package is incomplete")
        require(any(path.endswith(".dxf") for path in paths), "package contains no part DXF")
        require(any(path.endswith(".svg") and path.startswith("drawings/") for path in paths),
                "package contains no part drawing")
        require(
            any(path.startswith("nesting/") for path in paths),
            "package contains no nesting map",
        )
        require(
            any(path.startswith("cam/setups/") and path.endswith(".svg") for path in paths),
            "package contains no setup sheet",
        )
        programs = [path for path in paths if path.endswith(".validation.ngc")]
        require(programs, "package contains no validation machine program")

        for entry in entries:
            path = entry.get("path")
            require(isinstance(path, str), "manifest artifact path must be a string")
            _safe_zip_path(path)
            payload = archive.read(path)
            require(entry.get("size_bytes") == len(payload), f"size mismatch for {path}")
            require(entry.get("sha256") == sha256(payload), f"SHA-256 mismatch for {path}")

        context = {field: manifest.get(field) for field in CONTEXT_HASH_FIELDS}
        require(
            manifest.get("production_context_hash") == sha256(canonical_json_bytes(context)),
            "manifest production_context_hash mismatch",
        )
        require(archive.read("model/design.step").startswith(b"ISO-10303-21"), "invalid STEP")
        require(archive.read("model/design.glb").startswith(b"glTF"), "invalid GLB")
        operations = mapping(json.loads(archive.read("cam/operations.json")), "operations")
        require(
            operations.get("schema_version") == "custombuild.operations.v1",
            "bad operations schema",
        )
        require(operations.get("design_hash") == design_hash, "operations design_hash mismatch")
        require(operations.get("mode") == "VALIDATION", "operations are not validation-only")
        if required_part_id is not None:
            bom_rows = list(
                csv.DictReader(io.StringIO(archive.read("bom/bom.csv").decode("utf-8")))
            )
            require(
                required_part_id in {row.get("part_id") for row in bom_rows},
                "accepted reinforcement is missing from BOM",
            )
            operation_rows = sequence(operations.get("operations"), "CAM operations")
            require(
                required_part_id
                in {
                    mapping(item, "CAM operation").get("part_id")
                    for item in operation_rows
                },
                "accepted reinforcement is missing from CAM operations",
            )
            nesting_payloads = [
                archive.read(path)
                for path in paths
                if path.startswith("nesting/") and path.endswith(".svg")
            ]
            require(
                any(required_part_id.encode("utf-8") in payload for payload in nesting_payloads),
                "accepted reinforcement is missing from nesting maps",
            )
            require(
                any(path.startswith(f"parts/{required_part_id}/") for path in paths),
                "accepted reinforcement has no detail drawing or DXF",
            )
        for program in programs:
            text = archive.read(program).decode("ascii")
            require("VALIDATION DRY RUN - NOT PRODUCTION APPROVED" in text, "unsafe program marker")
            executable = re.sub(r"\([^)]*\)", "", text)
            require(re.search(r"\bM0?[34]\b", executable, re.IGNORECASE) is None,
                    f"spindle start found in {program}")
            require(re.search(r"\bZ-", executable, re.IGNORECASE) is None,
                    f"negative Z found in {program}")
    return manifest


def _validate_artifact_target(base_url: str, target: str) -> None:
    base = urlparse(base_url)
    parsed = urlparse(target)
    require(parsed.scheme in {"http", "https"} and bool(parsed.hostname), "bad artifact URL")
    require(parsed.username is None and parsed.password is None, "artifact URL contains userinfo")
    loopback = {"localhost", "127.0.0.1", "::1"}
    if base.hostname in loopback:
        require(parsed.hostname in loopback, "artifact redirect left the local Compose host")
    else:
        require(parsed.hostname == base.hostname, "artifact redirect changed host")


def download_artifact(client: HttpClient, download_path: str) -> HttpResult:
    redirect = client.request(
        "GET",
        download_path,
        expected=(307,),
        follow_redirects=False,
    )
    location = redirect.headers.get("location")
    require(location, "artifact endpoint returned no signed object-storage redirect")
    target = urljoin(redirect.url, location)
    _validate_artifact_target(client.base_url, target)
    request = Request(  # noqa: S310 - target is restricted by _validate_artifact_target
        target, headers={"User-Agent": "custombuild-live-acceptance/1"}
    )
    try:
        with build_opener().open(request, timeout=client.request_timeout) as response:  # noqa: S310
            result = HttpResult(
                response.status,
                response.geturl(),
                {key.lower(): value for key, value in response.headers.items()},
                response.read(),
            )
    except (HTTPError, OSError, TimeoutError, URLError) as exc:
        raise AcceptanceFailure(f"signed object-storage download failed: {exc}") from exc
    require(result.status == 200, f"object storage returned {result.status}")
    return result


def create_project(client: HttpClient, name: str) -> dict[str, Any]:
    return mapping(
        client.json(
            "POST",
            "/v1/projects",
            payload={
                "name": name,
                "description": "Live Compose tenant and production acceptance",
                "furniture_type": "bookcase",
            },
            expected=(201,),
        ),
        "project",
    )


def create_version(
    client: HttpClient,
    project_id: str,
    overrides: dict[str, object] | None = None,
) -> dict[str, Any]:
    spec = bookcase_spec(overrides)
    return mapping(
        client.json(
            "POST",
            f"/v1/projects/{project_id}/versions",
            payload={"spec": spec},
            expected=(201,),
        ),
        "design version",
    )


def bookcase_spec(overrides: dict[str, object] | None = None) -> dict[str, object]:
    spec: dict[str, object] = {
        "width_mm": 700,
        "height_mm": 1_000,
        "depth_mm": 320,
        "material_id": "mdf",
        "nominal_thickness_mm": 18,
        "measured_thickness_mm": 18,
        "shelf_count": 2,
        "shelf_mount": "fixed",
        "load_per_shelf_kg": 10,
        "back_panel": True,
        "plinth": True,
        "divider_count": 0,
        "edge_band_mm": 1,
        "joint_system": "dado",
        "reinforcement_mode": "manual",
        "wall_anchor_required": False,
    }
    spec.update(overrides or {})
    return spec


def run_acceptance(arguments: argparse.Namespace) -> dict[str, object]:
    anonymous = HttpClient(arguments.base_url, None, arguments.request_timeout)
    wait_for_api(anonymous, arguments.readiness_timeout)
    nordic = HttpClient(arguments.base_url, arguments.nordic_token, arguments.request_timeout)
    atelier = HttpClient(arguments.base_url, arguments.atelier_token, arguments.request_timeout)

    nordic_me = mapping(nordic.json("GET", "/v1/me"), "Nordic principal")
    atelier_me = mapping(atelier.json("GET", "/v1/me"), "Atelier principal")
    require(nordic_me.get("organization_id") == NORDIC_ORGANIZATION_ID, "wrong Nordic tenant")
    require(atelier_me.get("organization_id") == ATELIER_ORGANIZATION_ID, "wrong Atelier tenant")
    require(
        nordic_me.get("organization_id") != atelier_me.get("organization_id"),
        "tenants coincide",
    )

    baseline = mapping(
        nordic.json("POST", "/v1/designs/preview", payload=bookcase_spec()),
        "baseline preview",
    )
    require(baseline.get("status") == "PASS", "baseline bookcase is not valid")
    overloaded_spec = bookcase_spec(
        {
            "width_mm": 900,
            "shelf_count": 3,
            "load_per_shelf_kg": 31,
            "reinforcement_mode": "auto",
        }
    )
    overloaded = mapping(
        nordic.json("POST", "/v1/designs/preview", payload=overloaded_spec),
        "overloaded preview",
    )
    require(overloaded.get("status") == "BLOCK", "overloaded shelf did not block")
    evaluations = [
        mapping(item, "rule evaluation")
        for item in sequence(overloaded.get("rule_evaluations"), "rule evaluations")
    ]
    require(
        any(
            item.get("rule_id") == "CB-DEFLECTION-001" and item.get("status") == "BLOCK"
            for item in evaluations
        ),
        "shelf deflection did not identify the overload",
    )
    corrected = mapping(
        nordic.json("POST", "/v1/designs/autofix", payload=overloaded_spec),
        "automatic reinforcement",
    )
    require(corrected.get("status") == "PASS", "automatic reinforcement did not resolve block")
    corrected_spec = mapping(corrected.get("spec"), "corrected DesignSpec")
    corrected_parameters = mapping(
        corrected_spec.get("parameters"), "corrected DesignSpec parameters"
    )
    divider_count = corrected_parameters.get("vertical_divider_count")
    require(divider_count == 1, "automatic correction did not add exactly one divider")
    changes = [
        mapping(change, "automatic change")
        for diff in sequence(corrected.get("change_diff"), "automatic change diff")
        for change in sequence(mapping(diff, "automatic diff").get("changes"), "diff changes")
    ]
    require(
        any(change.get("path") == "parameters.vertical_divider_count" for change in changes),
        "automatic divider was not disclosed in the change diff",
    )
    corrected_parts = [
        mapping(item, "corrected part")
        for item in sequence(corrected.get("parts"), "corrected parts")
    ]
    corrected_dividers = [part for part in corrected_parts if part.get("kind") == "divider"]
    require(len(corrected_dividers) == 1, "corrected model has the wrong divider count")
    corrected_divider_id = str(corrected_dividers[0].get("part_id"))
    corrected_steps = [
        mapping(item, "assembly step")
        for item in sequence(corrected.get("assembly_steps"), "corrected assembly steps")
    ]
    require(
        any(corrected_divider_id in sequence(step.get("part_ids"), "assembly part IDs")
            for step in corrected_steps),
        "accepted reinforcement is missing from assembly order",
    )
    log(
        "automatic_reinforcement_verified",
        before_hash=overloaded.get("design_hash"),
        after_hash=corrected.get("design_hash"),
        divider_part_id=corrected_divider_id,
    )

    run_id = re.sub(r"[^A-Za-z0-9-]", "-", arguments.run_id)[:24]
    project_name = f"Compose acceptance {run_id}"
    nordic_project = create_project(nordic, project_name)
    atelier_project = create_project(atelier, project_name)
    nordic_project_id = str(nordic_project["id"])
    atelier_project_id = str(atelier_project["id"])
    require(nordic_project_id != atelier_project_id, "tenant projects share an ID")

    nordic.request("GET", f"/v1/projects/{atelier_project_id}", expected=(404,))
    atelier.request("GET", f"/v1/projects/{nordic_project_id}", expected=(404,))
    nordic_projects = sequence(nordic.json("GET", "/v1/projects"), "Nordic projects")
    atelier_projects = sequence(atelier.json("GET", "/v1/projects"), "Atelier projects")
    require(atelier_project_id not in {item.get("id") for item in nordic_projects}, "project leak")
    require(nordic_project_id not in {item.get("id") for item in atelier_projects}, "project leak")

    nordic_version = create_version(
        nordic,
        nordic_project_id,
        overloaded_spec | {"divider_count": int(divider_count)},
    )
    atelier_version = create_version(atelier, atelier_project_id)
    revision = int(nordic_version["revision"])
    atelier_revision = int(atelier_version["revision"])
    require(mapping(nordic_version.get("result_json"), "Nordic result").get("status") == "PASS",
            "acceptance design is not PASS")
    persisted_parts = [
        mapping(item, "persisted part")
        for item in sequence(
            mapping(nordic_version.get("result_json"), "Nordic result").get("parts"),
            "persisted parts",
        )
    ]
    persisted_dividers = [part for part in persisted_parts if part.get("kind") == "divider"]
    require(len(persisted_dividers) == 1, "accepted divider was not persisted")
    divider_part_id = str(persisted_dividers[0].get("part_id"))
    atelier.request(
        "GET",
        f"/v1/projects/{nordic_project_id}/versions/{revision}",
        expected=(404,),
    )
    nordic.request(
        "GET",
        f"/v1/projects/{atelier_project_id}/versions/{atelier_revision}",
        expected=(404,),
    )
    log(
        "tenant_isolation_verified",
        nordic_project=nordic_project_id,
        atelier_project=atelier_project_id,
    )

    base = f"/v1/projects/{nordic_project_id}/versions/{revision}"
    validated = mapping(nordic.json("POST", f"{base}/validate"), "validated version")
    require(validated.get("status") == "design_validated", "design validation did not persist")
    design_approval = mapping(
        nordic.json(
            "POST",
            f"{base}/approve",
            payload={
                "approval_type": "design",
                "reason": "Live Compose design review completed",
                "warning_overrides": [],
            },
        ),
        "design approval",
    )
    require(design_approval.get("status") == "design_validated", "unexpected design status")

    generation_request = {
        "stock_width_mm": 2_440,
        "stock_height_mm": 1_220,
        "stock_count": 4,
        "back_stock_width_mm": 2_440,
        "back_stock_height_mm": 1_220,
        "back_stock_count": 2,
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        "postprocessor_id": "linuxcnc-validation-1.0.0",
        "include_step": True,
        "include_validation_program": True,
    }
    first_job = mapping(
        nordic.json("POST", f"{base}/generate", payload=generation_request, expected=(202,)),
        "generation job",
    )
    second_job = mapping(
        nordic.json("POST", f"{base}/generate", payload=generation_request, expected=(202,)),
        "idempotent generation job",
    )
    job_id = str(first_job["id"])
    require(second_job.get("id") == job_id, "identical generation created a duplicate job")
    require(
        second_job.get("production_context_hash") == first_job.get("production_context_hash"),
        "idempotent generation changed production context",
    )
    atelier.request("GET", f"/v1/jobs/{job_id}", expected=(404,))
    atelier.request("GET", f"/v1/jobs/{job_id}/artifacts", expected=(404,))
    completed_job = wait_for_job(
        nordic,
        job_id,
        timeout_seconds=arguments.job_timeout,
        poll_interval=arguments.poll_interval,
    )
    job_result = mapping(completed_job.get("result_json"), "job result")
    require(job_result.get("authoritative_geometry") is True, "job lacks authoritative geometry")
    require(job_result.get("dfm_status") != "BLOCK", "job DFM is blocking")
    require(job_result.get("machine_program_mode") == "VALIDATION_DRY_RUN", "unsafe CAM mode")
    require(job_result.get("production_machine_program") is False, "job claims production G-code")

    artifact_values = sequence(nordic.json("GET", f"/v1/jobs/{job_id}/artifacts"), "artifacts")
    artifacts = [mapping(item, "artifact") for item in artifact_values]
    by_kind: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        kind = artifact.get("kind")
        require(isinstance(kind, str) and kind not in by_kind, "duplicate artifact kind")
        by_kind[kind] = artifact
    require({"production_bundle", "manifest"} <= set(by_kind), "stored artifacts are missing")

    downloaded: dict[str, bytes] = {}
    for kind in ("production_bundle", "manifest"):
        artifact = by_kind[kind]
        path = artifact.get("download_path")
        require(isinstance(path, str), f"{kind} has no download path")
        response = download_artifact(nordic, path)
        require(len(response.body) == artifact.get("size_bytes"), f"{kind} size mismatch")
        require(sha256(response.body) == artifact.get("sha256"), f"{kind} SHA mismatch")
        downloaded[kind] = response.body
    bundle_sha = require_sha256(job_result.get("bundle_sha256"), "job bundle_sha256")
    manifest_sha = require_sha256(job_result.get("manifest_sha256"), "job manifest_sha256")
    require(sha256(downloaded["production_bundle"]) == bundle_sha, "job bundle hash mismatch")
    require(sha256(downloaded["manifest"]) == manifest_sha, "job manifest hash mismatch")
    manifest = verify_package(
        downloaded["production_bundle"],
        downloaded["manifest"],
        project_id=nordic_project_id,
        revision=revision,
        design_hash=str(nordic_version["design_hash"]),
        required_part_id=divider_part_id,
    )
    require(job_result.get("artifact_count") == len(manifest["artifacts"]) + 1,
            "job artifact count mismatch")
    log("production_package_verified", job_id=job_id, manifest_sha256=manifest_sha)

    cam_approval = mapping(
        nordic.json(
            "POST",
            f"{base}/approve",
            payload={
                "approval_type": "cam",
                "reason": "Live Compose CAM review completed",
                "generation_job_id": job_id,
                "warning_overrides": [],
            },
        ),
        "CAM approval",
    )
    require(cam_approval.get("status") == "approved", "CAM approval did not complete approval")
    release_number = f"ACCEPT-{run_id.upper()}"[:40]
    release = mapping(
        nordic.json(
            "POST",
            f"{base}/release",
            payload={"release_number": release_number, "confirmation": "RELEASE"},
        ),
        "release",
    )
    require(release.get("status") == "released", "revision was not released")
    require(release.get("manifest_sha256") == manifest_sha, "release manifest hash mismatch")
    require(release.get("machine_use") == "validation_only", "release overstates machine safety")
    frozen = mapping(nordic.json("GET", base), "released version")
    require(frozen.get("immutable") is True and frozen.get("status") == "released",
            "released version is not immutable")
    nordic.request("POST", f"{base}/validate", expected=(409,))
    atelier.request("GET", base, expected=(404,))

    replacement = create_version(nordic, nordic_project_id, {"width_mm": 710})
    require(replacement.get("revision") == revision + 1, "design edit created no new revision")
    superseded = mapping(nordic.json("GET", base), "superseded version")
    require(
        superseded.get("status") == "superseded" and superseded.get("immutable") is True,
        "new design did not supersede the released revision",
    )
    nordic.request("GET", f"/v1/jobs/{job_id}/artifacts", expected=(409,))
    stale_download = by_kind["production_bundle"].get("download_path")
    require(isinstance(stale_download, str), "bundle has no stale download probe")
    nordic.request("GET", stale_download, expected=(409,), follow_redirects=False)
    log("release_verified", release_id=release.get("release_id"), release_number=release_number)
    return {
        "project_id": nordic_project_id,
        "revision": revision,
        "job_id": job_id,
        "release_id": release.get("release_id"),
        "manifest_sha256": manifest_sha,
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("CUSTOMBUILD_ACCEPTANCE_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--nordic-token", default=NORDIC_TOKEN)
    parser.add_argument("--atelier-token", default=ATELIER_TOKEN)
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:12])
    parser.add_argument("--readiness-timeout", type=float, default=180)
    parser.add_argument("--job-timeout", type=float, default=900)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--request-timeout", type=float, default=30)
    arguments = parser.parse_args()
    for name in ("readiness_timeout", "job_timeout", "poll_interval", "request_timeout"):
        require(getattr(arguments, name) > 0, f"--{name.replace('_', '-')} must be positive")
    return arguments


def main() -> int:
    try:
        result = run_acceptance(parse_arguments())
    except AcceptanceFailure as exc:
        print(json.dumps({"event": "acceptance_failed", "error": str(exc)}), file=sys.stderr)
        return 1
    log("acceptance_succeeded", **result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
