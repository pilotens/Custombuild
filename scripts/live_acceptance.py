"""Live Compose acceptance for the complete bookcase production vertical.

This script deliberately uses only Python's standard library and public HTTP
endpoints. It must be run against the real Compose API, worker, PostgreSQL,
Redis and object storage; it never mutates the database or queue directly.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import socket
import sys
import time
import uuid
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from threading import Event, Thread
from typing import Any, Final, cast
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
    "domain_template_version",
    "template_capability_version",
    "template_capability_registry_version",
    "template_id",
    "template_capability_fingerprint",
    "template_capability",
    "rule_version",
    "material_versions",
    "joint_version",
    "machine_profile",
    "postprocessor_version",
    "generation_context_hash",
    "production_engine_context",
    "artifact_schema_version",
    "cad_status",
    "release_scope",
    "machine_use",
    "physical_cutting_authorized",
    "approved_assumptions",
    "warnings",
    "overrides",
    "external_evidence",
    "source_provenance",
    "artifacts",
)
PRODUCTION_MANIFEST_SCHEMA_VERSION: Final = "custombuild.production-manifest.v4"
DESIGN_REVIEW_PACKAGE_STATUS_SCHEMA_VERSION: Final = "custombuild.design-review-package-status.v1"
DESIGN_REVIEW_PACKAGE_STATUS_PATH: Final = "validation/design-review-package-status.json"
DFM_REPORT_PATH: Final = "validation/dfm-report.json"
DFM_REPORT_ROLE: Final = "DFM_VALIDATION_REPORT"
STOCK_SELECTION_PATH: Final = "validation/stock-selection.json"
STOCK_SELECTION_ROLE: Final = "STOCK_SELECTION_SNAPSHOT"
STOCK_SELECTION_SCHEMA_VERSION: Final = "custombuild.stock-selection.v1"
GENERATION_PLAN_PATH: Final = "validation/generation-plan.json"
GENERATION_PLAN_ROLE: Final = "GENERATION_PLAN"
GENERATION_PLAN_SCHEMA_VERSION: Final = "custombuild.generation-plan.v1"
PRODUCTION_PIPELINE_VERSION: Final = "production-pipeline-1.10.0"
NESTING_ALGORITHM_VERSION: Final = "deterministic-bottom-left-v1"
OPERATIONS_SCHEMA_VERSION: Final = "custombuild.operations.v2"
OPERATIONS_ENGINE_VERSION: Final = "semantic-operations-1.2.0"
GENERATION_PLAN_KEYS: Final = frozenset(
    {
        "schema_version",
        "pipeline_version",
        "nesting_algorithm",
        "operations_schema_version",
        "operations_engine_version",
        "machine_profile",
        "postprocessor",
        "stock_profiles_fingerprint",
        "validation_program_requested",
        "two_sided_registrations",
    }
)
DFM_ENGINE_VERSION: Final = "dfm-1.3.0"
STOCK_PROFILE_MISSING: Final = "STOCK_PROFILE_MISSING"
DFM_GRAIN_MISSING: Final = "DFM-GRAIN-001"
TWO_SIDED_REGISTRATION_MISSING: Final = "TWO_SIDED_REGISTRATION_MISSING"
DADO_RETENTION_EVIDENCE_MISSING: Final = "DADO_RETENTION_EVIDENCE_MISSING"
BACK_PANEL_RETENTION_EVIDENCE_MISSING: Final = (
    "BACK_PANEL_RETENTION_EVIDENCE_MISSING"
)
DFM_BLOCKER_CODES: Final = frozenset({STOCK_PROFILE_MISSING, DFM_GRAIN_MISSING})
BLOCKED_CAM_REQUIRED_ACTIONS: Final = {
    STOCK_PROFILE_MISSING: (
        "Select and server-bind an exact stock profile for every part material, version, "
        "thickness, blank size and quantity; do not infer sheet size, stock identity or "
        "machine capacity."
    ),
    DFM_GRAIN_MISSING: (
        "Bind an exact, structured X or Y stock-grain axis for every directional material "
        "stock profile; opaque evidence or acknowledgement cannot resolve this blocker."
    ),
    TWO_SIDED_REGISTRATION_MISSING: (
        "Bind an externally specified two-sided registration and fixture plan; "
        "do not infer WCS, pins, fixtures or registration coordinates."
    ),
    DADO_RETENTION_EVIDENCE_MISSING: (
        "Bind current certifier-signed, checksum-addressed mechanical retention evidence "
        "to every load-bearing carcass DADO application, including exact geometry, compiler, "
        "hardware quantity, material/thickness and shear/withdrawal capacity; a review "
        "acknowledgement, adhesive or geometric bearing check cannot replace that evidence."
    ),
    BACK_PANEL_RETENTION_EVIDENCE_MISSING: (
        "Use only the canonical inset back whose four boundary grooves and multi-direction "
        "closing sequence prove mechanical capture, or bind independently authenticated "
        "back-panel retention evidence when that application class is implemented."
    ),
}
GENERATED_REVIEW_REQUIRED_ACTION: Final = (
    "None for design review; physical workshop evidence remains required."
)
DESIGN_REVIEW_PACKAGE_STATUS_KEYS: Final = frozenset(
    {
        "schema_version",
        "package_status",
        "cam_status",
        "blocker_codes",
        "operations_included",
        "setup_sheets_included",
        "nesting_included",
        "validation_backplot_included",
        "validation_program_included",
        "physical_cutting_authorized",
        "required_action",
    }
)
REQUIRED_REVIEW_PACKAGE_PATHS: Final = frozenset(
    {
        "assembly/assembly-manual.pdf",
        "bom/bom.csv",
        "bom/bom.pdf",
        "bom/hardware-list.csv",
        "cut-list/cut-list.csv",
        "labels/part-labels.pdf",
        "materials/material-list.csv",
        "model/design.glb",
        "model/design.step",
        "qa/measurement-protocol.pdf",
        "validation/construction-report.json",
        "validation/construction-report.pdf",
        "validation/cad-interchange-status.json",
        "validation/dfm-report.json",
        STOCK_SELECTION_PATH,
        GENERATION_PLAN_PATH,
        DESIGN_REVIEW_PACKAGE_STATUS_PATH,
        "validation/workshop-readiness.json",
    }
)
REQUIRED_CAM_PACKAGE_PATHS: Final = frozenset(
    {
        "cam/operations.json",
        "cam/tool-list.csv",
        "cam/validation-backplot.svg",
    }
)
REQUIRED_PACKAGE_PATHS: Final = REQUIRED_REVIEW_PACKAGE_PATHS | REQUIRED_CAM_PACKAGE_PATHS
FORBIDDEN_BLOCKED_CAM_PATHS: Final = REQUIRED_CAM_PACKAGE_PATHS | frozenset(
    {
        "labels/label-index.csv",
        "materials/stock-purchase.csv",
        "quality/measurement-plan.json",
    }
)
FORBIDDEN_BLOCKED_CAM_ROLES: Final = frozenset(
    {
        "LABEL_INDEX",
        "MACHINE_NEUTRAL_OPERATIONS",
        "NESTING_MAP",
        "NON_CUTTING_VALIDATION_PROGRAM",
        "PLACEMENT_INDEX",
        "PLACEMENT_MAP",
        "QUALITY_MEASUREMENT_PLAN",
        "SETUP_SHEET",
        "STOCK_PURCHASE_SCHEDULE",
        "STOCK_LAYOUT",
        "STOCK_PROFILE",
        "TOOL_LIST",
        "VALIDATION_BACKPLOT",
    }
)
BLOCKED_CAM_ALLOWED_ARTIFACTS: Final = frozenset(
    {
        ("assembly/assembly-manual.pdf", "ASSEMBLY_REVIEW_MANUAL", "application/pdf"),
        ("assembly/assembly-readiness.json", "ASSEMBLY_READINESS", "application/json"),
        ("bom/bom.csv", "BOM", "text/csv"),
        ("bom/bom.pdf", "BOM_PDF", "application/pdf"),
        ("bom/grouped-bom.json", "GROUPED_BOM", "application/json"),
        ("bom/hardware-list.csv", "HARDWARE_LIST", "text/csv"),
        ("cut-list/cut-list.csv", "CUT_LIST", "text/csv"),
        ("design/design-spec.json", "FROZEN_DESIGN_SPEC", "application/json"),
        ("design/result-summary.json", "DESIGN_RESULT_SUMMARY", "application/json"),
        ("labels/part-labels.pdf", "PART_LABELS", "application/pdf"),
        ("materials/material-list.csv", "MATERIAL_LIST", "text/csv"),
        (
            "model/design.fcstd",
            "NON_AUTHORITATIVE_FREECAD_PROJECT",
            "application/vnd.freecad",
        ),
        ("model/design.glb", "WEB_PREVIEW_GLB", "model/gltf-binary"),
        ("model/design.step", "AUTHORITATIVE_STEP", "model/step"),
        ("qa/measurement-protocol.pdf", "QA_PROTOCOL", "application/pdf"),
        (
            "validation/cad-interchange-status.json",
            "CAD_INTERCHANGE_STATUS",
            "application/json",
        ),
        (
            "validation/construction-report.json",
            "CONSTRUCTION_VALIDATION_REPORT",
            "application/json",
        ),
        (
            "validation/construction-report.pdf",
            "CONSTRUCTION_VALIDATION_REPORT",
            "application/pdf",
        ),
        (
            DESIGN_REVIEW_PACKAGE_STATUS_PATH,
            "DESIGN_REVIEW_PACKAGE_STATUS",
            "application/json",
        ),
        ("validation/dfm-report.json", "DFM_VALIDATION_REPORT", "application/json"),
        (
            "manufacturing/manufacturing-intent.json",
            "MACHINE_NEUTRAL_MANUFACTURING_INTENT",
            "application/json",
        ),
        ("shop/supplier-handoff.json", "CNC_SHOP_HANDOFF", "application/json"),
        (STOCK_SELECTION_PATH, STOCK_SELECTION_ROLE, "application/json"),
        (GENERATION_PLAN_PATH, GENERATION_PLAN_ROLE, "application/json"),
        ("validation/source-provenance.json", "SOURCE_PROVENANCE", "application/json"),
        (
            "validation/workshop-readiness.json",
            "WORKSHOP_READINESS_REPORT",
            "application/json",
        ),
    }
)
BLOCKED_CAM_ALLOWED_EVIDENCE_KINDS: Final = frozenset(
    {
        "production_bundle",
        "manifest",
        "manufacturing_intent",
        "supplier_handoff",
        "dfm_report",
        "stock_selection",
        "generation_plan",
        "design_review_package_status",
        "design_glb",
        "workshop_readiness",
        "design_fcstd",
        "cad_interchange_status",
        "source_provenance",
        "assembly_readiness",
    }
)
DFM_REPORT_KEYS: Final = frozenset({"engine_version", "issues"})
DFM_ISSUE_KEYS: Final = frozenset(
    {
        "code",
        "severity",
        "message",
        "part_id",
        "feature_id",
        "setup_id",
        "inputs",
        "suggestion",
    }
)
DESIGN_REVIEW_PACKAGE_STATUS_ROLE: Final = "DESIGN_REVIEW_PACKAGE_STATUS"
WORKSHOP_READINESS_PATH: Final = "validation/workshop-readiness.json"
WORKSHOP_READINESS_ROLE: Final = "WORKSHOP_READINESS_REPORT"
WORKSHOP_READINESS_SCHEMA_VERSION: Final = "custombuild.workshop-readiness.v2"
WORKSHOP_READINESS_KEYS: Final = frozenset(
    {
        "schema_version",
        "release_scope",
        "machine_use",
        "edge_band_selection_required",
        "design_review_ready",
        "physical_cutting_authorized",
        "missing_evidence_count",
        "software_evidence",
        "workshop_evidence",
    }
)
READINESS_REQUIREMENT_KEYS: Final = frozenset(
    {"code", "title", "status", "evidence", "required_action"}
)
SOFTWARE_READINESS_REQUIREMENTS: Final = (
    ("AUTHORITATIVE_CAD", "Authoritative CAD geometry"),
    ("DFM_SCREEN", "Manufacturing feasibility screen"),
    ("SEMANTIC_OPERATIONS", "Semantic machining operations"),
    ("SETUP_SHEETS", "Setup sheets"),
    ("VALIDATION_BACKPLOT", "Independent review backplot"),
    ("NON_CUTTING_PROGRAM", "Non-cutting controller validation"),
)
WORKSHOP_READINESS_REQUIREMENTS: Final = (
    ("WALL_ANCHOR", "Wall substrate and anchor system"),
    ("CABINET_HARDWARE", "Base-cabinet hardware and drill pattern"),
    ("MATERIAL_GRAIN", "Structured sheet-material grain-axis binding"),
    ("MACHINE_CALIBRATION", "Calibrated physical machine"),
    ("WCS_CONVENTION", "Verified WCS and origin convention"),
    ("MEASURED_TOOLING", "Measured tool, holder and runout"),
    ("MATERIAL_BATCH", "Verified material batch"),
    ("JOINT_COUPONS", "Joint coupon and tolerance test"),
    ("MATERIAL_REMOVAL_COMPARISON", "Independent material-removal comparison"),
    ("SUPERVISED_AIR_CUT", "Supervised air cut"),
    ("REFERENCE_PART", "Measured reference part"),
    ("PROTOTYPE_BUILD", "Complete prototype furniture build"),
    ("CNC_OPERATOR_APPROVAL", "Named CNC operator approval"),
    (
        "FURNITURE_CONSTRUCTOR_APPROVAL",
        "Named furniture constructor approval",
    ),
)
EDGE_BAND_READINESS_REQUIREMENT: Final = (
    "EDGE_BAND_SYSTEM",
    "Adhesive-free mechanical edge protection and cut-size compensation",
)
MAX_ZIP_FILES: Final = 10_000
MAX_ZIP_ENTRY_BYTES: Final = 32 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES: Final = 2 * 1024 * 1024 * 1024
HTTP_READ_CHUNK_BYTES: Final = 64 * 1024
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
    def redirect_request(
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
        maximum_body_bytes: int | None = None,
        total_read_seconds: float | None = None,
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
            response = opener.open(request, timeout=self.request_timeout)  # noqa: S310
            status = response.status
            response_url = response.geturl()
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            if maximum_body_bytes is None:
                with response:
                    response_body = response.read()
            else:
                if total_read_seconds is None:
                    raise AcceptanceFailure("bounded HTTP read has no deadline")
                response_body = _read_bounded_http_body(
                    response,
                    maximum_body_bytes=maximum_body_bytes,
                    total_read_seconds=total_read_seconds,
                )
            result = HttpResult(status, response_url, response_headers, response_body)
        except HTTPError as exc:
            if maximum_body_bytes is None:
                try:
                    error_body = exc.read()
                finally:
                    exc.close()
            else:
                if total_read_seconds is None:
                    raise AcceptanceFailure("bounded HTTP read has no deadline") from exc
                error_body = _read_bounded_http_body(
                    exc,
                    maximum_body_bytes=maximum_body_bytes,
                    total_read_seconds=total_read_seconds,
                )
            result = HttpResult(
                exc.code,
                exc.geturl(),
                {key.lower(): value for key, value in exc.headers.items()},
                error_body,
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


def _read_bounded_http_body(
    response: Any,
    *,
    maximum_body_bytes: int,
    total_read_seconds: int | float,
) -> bytes:
    """Read one bounded body under a deadline that a slow drip cannot extend."""

    require(
        type(maximum_body_bytes) is int and maximum_body_bytes > 0,
        "bounded HTTP read has an invalid size",
    )
    require(
        not isinstance(total_read_seconds, bool)
        and isinstance(total_read_seconds, int | float)
        and total_read_seconds > 0,
        "bounded HTTP read has an invalid deadline",
    )
    deadline = time.monotonic() + total_read_seconds
    finished = Event()
    chunks: list[bytes] = []
    failures: list[BaseException] = []

    def read_body() -> None:
        read_bytes = 0
        try:
            while True:
                remaining = maximum_body_bytes + 1 - read_bytes
                if remaining <= 0:
                    raise AcceptanceFailure("HTTP response body exceeds its declared size")
                chunk = response.read(min(HTTP_READ_CHUNK_BYTES, remaining))
                if not isinstance(chunk, bytes):
                    raise AcceptanceFailure("HTTP response returned non-byte data")
                if not chunk:
                    break
                read_bytes += len(chunk)
                if read_bytes > maximum_body_bytes:
                    raise AcceptanceFailure("HTTP response body exceeds its declared size")
                chunks.append(chunk)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    Thread(target=read_body, name="acceptance-http-reader", daemon=True).start()
    completed = finished.wait(max(0.0, deadline - time.monotonic()))
    if not completed:
        candidate = response
        for _ in range(6):
            response_socket = getattr(candidate, "_sock", None)
            if isinstance(response_socket, socket.socket):
                with suppress(OSError):
                    response_socket.shutdown(socket.SHUT_RDWR)
                break
            nested = getattr(candidate, "fp", None)
            if nested is None:
                nested = getattr(candidate, "raw", None)
            if nested is None or nested is candidate:
                break
            candidate = nested
    with suppress(OSError, ValueError):
        response.close()
    if not completed:
        finished.wait(0.25)
        raise AcceptanceFailure("artifact download exceeded its total read deadline")
    if failures:
        raise failures[0]
    return b"".join(chunks)


def mapping(value: Any, label: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{label} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise AcceptanceFailure(f"{label} must be a JSON array")
    return value


def string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise AcceptanceFailure(f"{label} must be a string")
    return value


def integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AcceptanceFailure(f"{label} must be an integer")
    return cast(int, value)


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
    text = string(value, label)
    require(re.fullmatch(r"[0-9a-f]{64}", text) is not None, f"{label} is not SHA-256")
    return text


def verify_blocked_cam_endpoint_rejection(
    result: HttpResult,
    blocker_codes: list[Any],
    *,
    label: str,
) -> None:
    """Prove that CAM/release remains closed for the package blocker.

    DADO retention is a frozen-design invariant. Its endpoints must preserve
    the exact machine-readable blocker rather than degrade into a generic
    missing-CAM response.
    """

    require(result.status == 409, f"{label} did not fail closed")
    retention_blockers = {
        DADO_RETENTION_EVIDENCE_MISSING,
        BACK_PANEL_RETENTION_EVIDENCE_MISSING,
    }
    if len(blocker_codes) != 1 or blocker_codes[0] not in retention_blockers:
        return
    try:
        response = mapping(json.loads(result.body), f"{label} rejection")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceFailure(f"{label} rejection is not JSON") from exc
    detail = mapping(response.get("detail"), f"{label} rejection detail")
    require(
        detail.get("code") == blocker_codes[0],
        f"{label} did not preserve the joint-retention blocker",
    )


def _verify_readiness_requirements(
    value: Any,
    *,
    label: str,
    expected: tuple[tuple[str, str], ...],
    allowed_statuses: frozenset[str],
) -> list[dict[str, Any]]:
    raw_items = sequence(value, label)
    require(
        len(raw_items) == len(expected),
        f"{label} must contain every canonical requirement exactly once",
    )
    items: list[dict[str, Any]] = []
    for index, (raw_item, (expected_code, expected_title)) in enumerate(
        zip(raw_items, expected, strict=True)
    ):
        item_label = f"{label}[{index}]"
        item = mapping(raw_item, item_label)
        require(set(item) == READINESS_REQUIREMENT_KEYS, f"{item_label} keys differ")
        for field in READINESS_REQUIREMENT_KEYS:
            field_value = string(item.get(field), f"{item_label}.{field}")
            require(field_value.strip(), f"{item_label}.{field} must not be blank")
        require(
            item["code"] == expected_code and item["title"] == expected_title,
            f"{item_label} order, code or title is not canonical",
        )
        require(
            item["status"] in allowed_statuses,
            f"{item_label}.status is invalid for its evidence scope",
        )
        items.append(item)
    return items


def verify_workshop_readiness(value: Any, *, label: str) -> dict[str, Any]:
    """Verify the complete public v2 boundary without application imports."""

    payload = mapping(value, label)
    require(set(payload) == WORKSHOP_READINESS_KEYS, f"{label} keys differ")
    require(
        payload["schema_version"] == WORKSHOP_READINESS_SCHEMA_VERSION,
        f"{label} schema is not v2",
    )
    require(payload["release_scope"] == "design_review", f"{label} release scope is unsafe")
    require(payload["machine_use"] == "validation_only", f"{label} machine use is unsafe")
    edge_required = payload["edge_band_selection_required"]
    require(type(edge_required) is bool, f"{label} edge flag must be boolean")
    design_review_ready = payload["design_review_ready"]
    require(type(design_review_ready) is bool, f"{label} ready flag must be boolean")
    require(
        payload["physical_cutting_authorized"] is False,
        f"{label} authorizes physical cutting",
    )
    missing_count = integer(payload["missing_evidence_count"], f"{label} missing count")
    require(missing_count >= 0, f"{label} missing count is negative")

    software = _verify_readiness_requirements(
        payload["software_evidence"],
        label=f"{label}.software_evidence",
        expected=SOFTWARE_READINESS_REQUIREMENTS,
        allowed_statuses=frozenset({"VERIFIED", "MISSING"}),
    )
    workshop_expected = WORKSHOP_READINESS_REQUIREMENTS + (
        (EDGE_BAND_READINESS_REQUIREMENT,) if edge_required else ()
    )
    workshop = _verify_readiness_requirements(
        payload["workshop_evidence"],
        label=f"{label}.workshop_evidence",
        expected=workshop_expected,
        allowed_statuses=frozenset({"VERIFIED", "EXTERNAL_EVIDENCE_REQUIRED"}),
    )
    derived_ready = all(item["status"] == "VERIFIED" for item in software)
    require(
        design_review_ready is derived_ready,
        f"{label} ready flag differs from software evidence",
    )
    derived_missing = sum(item["status"] != "VERIFIED" for item in (*software, *workshop))
    require(
        missing_count == derived_missing,
        f"{label} missing count differs from evidence",
    )
    return payload


def verify_design_review_package_status(value: Any, *, label: str) -> dict[str, Any]:
    """Verify the versioned boundary between a review package and CAM output."""

    payload = mapping(value, label)
    require(set(payload) == DESIGN_REVIEW_PACKAGE_STATUS_KEYS, f"{label} keys differ")
    require(
        payload["schema_version"] == DESIGN_REVIEW_PACKAGE_STATUS_SCHEMA_VERSION,
        f"{label} schema is unsupported",
    )
    require(
        payload["package_status"] == "READY_FOR_DESIGN_REVIEW",
        f"{label} package is not ready for design review",
    )
    require(
        payload["physical_cutting_authorized"] is False,
        f"{label} authorizes physical cutting",
    )
    required_action = string(payload["required_action"], f"{label}.required_action")
    require(required_action.strip(), f"{label}.required_action must not be blank")
    blocker_codes = sequence(payload["blocker_codes"], f"{label}.blocker_codes")
    require(
        all(isinstance(code, str) and code for code in blocker_codes),
        f"{label}.blocker_codes are invalid",
    )
    require(
        blocker_codes == sorted(set(blocker_codes)),
        f"{label}.blocker_codes are not canonical",
    )
    flag_names = (
        "operations_included",
        "setup_sheets_included",
        "nesting_included",
        "validation_backplot_included",
        "validation_program_included",
    )
    require(
        all(type(payload[name]) is bool for name in flag_names),
        f"{label} artifact flags must be booleans",
    )
    cam_status = payload["cam_status"]
    if cam_status == "BLOCKED":
        require(
            len(blocker_codes) == 1 and blocker_codes[0] in BLOCKED_CAM_REQUIRED_ACTIONS,
            f"{label} has an unsupported CAM blocker",
        )
        require(
            all(payload[name] is False for name in flag_names),
            f"{label} blocked CAM status claims manufacturing artifacts",
        )
        expected_action = BLOCKED_CAM_REQUIRED_ACTIONS[blocker_codes[0]]
    else:
        require(cam_status == "VALIDATION_GENERATED", f"{label} CAM status is unsupported")
        require(not blocker_codes, f"{label} generated CAM status has blockers")
        require(
            all(
                payload[name] is True
                for name in (
                    "operations_included",
                    "setup_sheets_included",
                    "nesting_included",
                    "validation_backplot_included",
                )
            ),
            f"{label} generated CAM status omits required validation artifacts",
        )
        expected_action = GENERATED_REVIEW_REQUIRED_ACTION
    require(required_action == expected_action, f"{label}.required_action is not canonical")
    return payload


def verify_generation_result_safety(job_result: dict[str, Any]) -> dict[str, Any]:
    require(job_result.get("authoritative_geometry") is True, "job lacks authoritative geometry")
    require(job_result.get("production_machine_program") is False, "job claims production G-code")
    package_status = verify_design_review_package_status(
        job_result.get("design_review_package_status"),
        label="job design-review package status",
    )
    workshop_readiness = verify_workshop_readiness(
        job_result.get("workshop_readiness"),
        label="job workshop readiness",
    )
    software_status = {
        item["code"]: item["status"] for item in workshop_readiness["software_evidence"]
    }
    workshop_status = {
        item["code"]: item["status"] for item in workshop_readiness["workshop_evidence"]
    }
    dfm_blocked = package_status["blocker_codes"] in (
        [STOCK_PROFILE_MISSING],
        [DFM_GRAIN_MISSING],
    )
    if dfm_blocked:
        require(
            job_result.get("dfm_status") == "BLOCK",
            "DFM-blocked review must preserve the raw blocking DFM status",
        )
        if package_status["blocker_codes"] == [DFM_GRAIN_MISSING]:
            require(
                workshop_status.get("MATERIAL_GRAIN") == "EXTERNAL_EVIDENCE_REQUIRED",
                "grain-blocked review must keep MATERIAL_GRAIN unresolved",
            )
    else:
        require(
            job_result.get("dfm_status") in {"PASS", "WARNING"},
            "job DFM status is unsafe for the review-package blocker",
        )
    if package_status["cam_status"] == "BLOCKED":
        require(job_result.get("machine_program_mode") == "CAM_BLOCKED", "unsafe CAM mode")
        require(
            "nesting_utilization_ppm" in job_result
            and job_result["nesting_utilization_ppm"] is None,
            "blocked CAM job claims nesting utilization",
        )
        used_sheet_count = job_result.get("used_sheet_count")
        require(
            type(used_sheet_count) is int and used_sheet_count == 0,
            "blocked CAM job claims used stock sheets",
        )
        require(
            job_result.get("nesting_layouts") == [],
            "blocked CAM job claims nesting layouts",
        )
        require(
            workshop_readiness["design_review_ready"] is False,
            "blocked CAM incorrectly claims complete software readiness",
        )
        require(
            software_status
            == {
                "AUTHORITATIVE_CAD": "VERIFIED",
                "DFM_SCREEN": "MISSING" if dfm_blocked else "VERIFIED",
                "SEMANTIC_OPERATIONS": "MISSING",
                "SETUP_SHEETS": "MISSING",
                "VALIDATION_BACKPLOT": "MISSING",
                "NON_CUTTING_PROGRAM": "MISSING",
            },
            "blocked CAM readiness does not match intentionally omitted artifacts",
        )
    else:
        require(
            job_result.get("machine_program_mode") == "VALIDATION_DRY_RUN",
            "unsafe CAM mode",
        )
        require(
            workshop_readiness["design_review_ready"] is True,
            "generated CAM lacks complete software readiness",
        )
    return workshop_readiness


def verify_status_readiness_alignment(
    package_status: dict[str, Any],
    workshop_readiness: dict[str, Any],
    *,
    label: str,
) -> None:
    software = {
        item["code"]: item["status"]
        for item in sequence(
            workshop_readiness.get("software_evidence"),
            f"{label}.software_evidence",
        )
    }
    workshop = {
        item["code"]: item["status"]
        for item in sequence(
            workshop_readiness.get("workshop_evidence"),
            f"{label}.workshop_evidence",
        )
    }
    expected = {
        "AUTHORITATIVE_CAD": "VERIFIED",
        "DFM_SCREEN": (
            "MISSING"
            if package_status["blocker_codes"] in ([STOCK_PROFILE_MISSING], [DFM_GRAIN_MISSING])
            else "VERIFIED"
        ),
        "SEMANTIC_OPERATIONS": ("VERIFIED" if package_status["operations_included"] else "MISSING"),
        "SETUP_SHEETS": ("VERIFIED" if package_status["setup_sheets_included"] else "MISSING"),
        "VALIDATION_BACKPLOT": (
            "VERIFIED" if package_status["validation_backplot_included"] else "MISSING"
        ),
        "NON_CUTTING_PROGRAM": (
            "VERIFIED" if package_status["validation_program_included"] else "MISSING"
        ),
    }
    require(software == expected, f"{label} package status and readiness disagree")
    if package_status["blocker_codes"] == [DFM_GRAIN_MISSING]:
        require(
            workshop.get("MATERIAL_GRAIN") == "EXTERNAL_EVIDENCE_REQUIRED",
            f"{label} grain blocker and MATERIAL_GRAIN readiness disagree",
        )


def verify_generation_context_hash(
    completed_job: dict[str, Any],
    job_result: dict[str, Any],
) -> str:
    job_context_hash = require_sha256(
        completed_job.get("production_context_hash"),
        "completed job production_context_hash",
    )
    result_context_hash = require_sha256(
        job_result.get("generation_context_hash"),
        "job result generation_context_hash",
    )
    require(
        job_context_hash == result_context_hash,
        "job and result generation context hashes differ",
    )
    return job_context_hash


def verify_explicit_two_sided_registration(operations: dict[str, Any]) -> None:
    """Require bound stock-frame registration evidence for every two-sided sheet."""

    setups = [
        mapping(item, "operations setup")
        for item in sequence(operations.get("setups"), "operations setups")
    ]
    by_sheet: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for setup in setups:
        stock_id = string(setup.get("stock_id"), "setup stock_id")
        sheet_index = integer(setup.get("sheet_index"), "setup sheet_index")
        by_sheet.setdefault((stock_id, sheet_index), []).append(setup)
    two_sided = [
        sheet_setups
        for sheet_setups in by_sheet.values()
        if {setup.get("side") for setup in sheet_setups} >= {"A", "B"}
    ]
    require(two_sided, "full CAM acceptance has no explicitly registered two-sided sheet")
    for sheet_setups in two_sided:
        for setup in sheet_setups:
            probe_method = string(setup.get("probe_method"), "setup probe_method")
            fixture = string(setup.get("fixture"), "setup fixture")
            require(
                probe_method.startswith("DECLARED_COORDINATE_REGISTRATION;"),
                "two-sided setup has no declared coordinate registration",
            )
            method_match = re.search(r"(?:^|;)METHOD=([^;]+)(?:;|$)", probe_method)
            coordinate_match = re.search(r"(?:^|;)STOCK_XY_UM=([^;]+)(?:;|$)", probe_method)
            require(method_match is not None, "two-sided setup has no registration method ID")
            require(coordinate_match is not None, "two-sided setup has no stock-frame coordinates")
            points = coordinate_match.group(1).split("|") if coordinate_match else []
            require(
                len(points) >= 2 and len(set(points)) == len(points),
                "registration points differ",
            )
            require(
                all(re.fullmatch(r"-?[0-9]+,-?[0-9]+", point) for point in points),
                "registration coordinates are not integer stock-frame coordinates",
            )
            require(
                fixture.startswith("EXTERNAL_FIXTURE_PLAN_REQUIRED;"),
                "two-sided setup has no explicit external fixture requirement",
            )


def manifest_context(manifest: dict[str, Any]) -> dict[str, Any]:
    missing_fields = [field for field in CONTEXT_HASH_FIELDS if field not in manifest]
    require(not missing_fields, f"manifest context fields are missing: {missing_fields}")
    return {field: manifest[field] for field in CONTEXT_HASH_FIELDS}


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
    require(
        path and "\\" not in path and "\x00" not in path and ":" not in path,
        f"unsafe ZIP path: {path!r}",
    )
    require(not PurePosixPath(path).is_absolute(), f"absolute ZIP path: {path}")
    require(
        all(part not in {"", ".", ".."} for part in path.split("/")),
        f"unsafe ZIP path: {path}",
    )


def blocked_cam_artifact_violation(path: str, role: str, media_type: str) -> bool:
    """Mirror the package boundary without importing application packages."""

    if (path, role, media_type) in BLOCKED_CAM_ALLOWED_ARTIFACTS:
        return False
    dynamic = re.fullmatch(
        r"(?P<namespace>parts|drawings)/(?P<component>[^/]+)/(?P<side>A|B)"
        r"(?P<suffix>\.dxf|\.svg)",
        path,
    )
    if dynamic is None:
        return True
    component = dynamic.group("component")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", component).strip("._") or "part"
    if cleaned != component or len(cleaned) > 80:
        return True
    return (
        dynamic.group("namespace"),
        dynamic.group("suffix"),
        role,
        media_type,
    ) not in {
        ("parts", ".dxf", "PART_DXF", "image/vnd.dxf"),
        ("drawings", ".svg", "PART_DRAWING", "image/svg+xml"),
    }


def blocked_cam_evidence_kind_is_forbidden(kind: str) -> bool:
    return kind not in BLOCKED_CAM_ALLOWED_EVIDENCE_KINDS


def _strict_canonical_json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    require(not payload.startswith(b"\xef\xbb\xbf"), f"{label} has a UTF-8 BOM")

    def reject_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    try:
        decoded = payload.decode("utf-8", errors="strict")
        parsed = json.loads(decoded, parse_constant=reject_nonfinite)
        require(isinstance(parsed, dict), f"{label} is not a JSON object")
        require(canonical_json_bytes(parsed) == payload, f"{label} is not canonical JSON")
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise AcceptanceFailure(f"{label} is not canonical UTF-8 JSON") from exc
    return cast(dict[str, Any], parsed)


def verify_generation_plan(
    value: Any,
    *,
    label: str,
    generated_validation_program: bool | None,
    expected_stock_profiles_fingerprint: str,
) -> dict[str, Any]:
    payload = mapping(value, label)
    require(set(payload) == GENERATION_PLAN_KEYS, f"{label} keys differ")
    require(
        payload.get("schema_version") == GENERATION_PLAN_SCHEMA_VERSION,
        f"{label} schema differs",
    )
    require(
        payload.get("pipeline_version") == PRODUCTION_PIPELINE_VERSION,
        f"{label} pipeline version differs",
    )
    require(
        payload.get("nesting_algorithm") == NESTING_ALGORITHM_VERSION,
        f"{label} nesting algorithm differs",
    )
    require(
        payload.get("operations_schema_version") == OPERATIONS_SCHEMA_VERSION,
        f"{label} operations schema differs",
    )
    require(
        payload.get("operations_engine_version") == OPERATIONS_ENGINE_VERSION,
        f"{label} operations engine differs",
    )
    require(
        require_sha256(
            payload.get("stock_profiles_fingerprint"),
            f"{label}.stock_profiles_fingerprint",
        )
        == expected_stock_profiles_fingerprint,
        f"{label} stock profiles fingerprint differs from stock selection",
    )
    requested = payload.get("validation_program_requested")
    require(type(requested) is bool, f"{label}.validation_program_requested must be boolean")
    if generated_validation_program is not None:
        require(
            requested is generated_validation_program,
            f"{label} validation-program request differs from generated status",
        )

    machine = mapping(payload.get("machine_profile"), f"{label}.machine_profile")
    require(set(machine) == {"id", "version", "fingerprint"}, f"{label} machine keys differ")
    for key in ("id", "version", "fingerprint"):
        require(
            bool(string(machine.get(key), f"{label}.machine_profile.{key}")),
            f"{label} machine {key} is empty",
        )
    postprocessor = mapping(payload.get("postprocessor"), f"{label}.postprocessor")
    require(set(postprocessor) == {"id", "version"}, f"{label} postprocessor keys differ")
    require(
        postprocessor.get("id") == "linuxcnc-validation",
        f"{label} postprocessor id differs",
    )
    require(
        bool(string(postprocessor.get("version"), f"{label}.postprocessor.version")),
        f"{label} postprocessor version is empty",
    )

    registrations = [
        mapping(item, f"{label} registration")
        for item in sequence(
            payload.get("two_sided_registrations"),
            f"{label}.two_sided_registrations",
        )
    ]
    stock_ids: list[str] = []
    for registration in registrations:
        require(set(registration) == {"stock_id", "sheets"}, f"{label} registration keys differ")
        stock_id = string(registration.get("stock_id"), f"{label} registration stock_id")
        stock_ids.append(stock_id)
        sheets = [
            mapping(item, f"{label} registration sheet")
            for item in sequence(registration.get("sheets"), f"{label} registration sheets")
        ]
        sheet_indices: list[int] = []
        for sheet in sheets:
            require(
                set(sheet) == {"sheet_index", "method_id", "points"},
                f"{label} registration sheet keys differ",
            )
            sheet_indices.append(integer(sheet.get("sheet_index"), f"{label} sheet_index"))
            require(
                bool(string(sheet.get("method_id"), f"{label} registration method_id")),
                f"{label} registration method_id is empty",
            )
            raw_points = sequence(sheet.get("points"), f"{label} registration points")
            require(len(raw_points) >= 2, f"{label} registration has too few points")
            coordinates: list[tuple[int, int]] = []
            for point_value in raw_points:
                point = mapping(point_value, f"{label} registration point")
                require(
                    set(point) == {"x_um", "y_um"},
                    f"{label} registration point keys differ",
                )
                coordinates.append(
                    (
                        integer(point.get("x_um"), f"{label} registration point x_um"),
                        integer(point.get("y_um"), f"{label} registration point y_um"),
                    )
                )
            require(
                len(coordinates) == len(set(coordinates)),
                f"{label} registration points are not unique",
            )
        require(
            sheet_indices == sorted(set(sheet_indices)),
            f"{label} registration sheets are not uniquely sorted",
        )
    require(
        stock_ids == sorted(set(stock_ids)),
        f"{label} registrations are not uniquely sorted",
    )
    return payload


def verify_design_review_dfm_report(
    value: Any,
    *,
    package_status: dict[str, Any],
    expected_status: str,
    label: str,
) -> dict[str, Any]:
    """Verify the raw DFM report and bind it to the one supported blocker profile."""

    payload = mapping(value, label)
    require(set(payload) == DFM_REPORT_KEYS, f"{label} keys differ")
    require(payload["engine_version"] == DFM_ENGINE_VERSION, f"{label} engine is unsupported")
    raw_issues = sequence(payload["issues"], f"{label}.issues")
    issues: list[dict[str, Any]] = []
    for index, raw_issue in enumerate(raw_issues):
        issue = mapping(raw_issue, f"{label}.issues[{index}]")
        require(set(issue) == DFM_ISSUE_KEYS, f"{label}.issues[{index}] keys differ")
        require(
            isinstance(issue["code"], str) and bool(issue["code"]),
            f"{label}.issues[{index}] code is invalid",
        )
        require(
            issue["severity"] in {"PASS", "WARNING", "BLOCK"},
            f"{label}.issues[{index}] severity is invalid",
        )
        require(
            isinstance(issue["message"], str) and bool(issue["message"]),
            f"{label}.issues[{index}] message is invalid",
        )
        for field in ("part_id", "feature_id", "setup_id"):
            require(
                issue[field] is None or (isinstance(issue[field], str) and bool(issue[field])),
                f"{label}.issues[{index}].{field} is invalid",
            )
        require(isinstance(issue["inputs"], dict), f"{label}.issues[{index}].inputs is invalid")
        require(
            issue["suggestion"] is None
            or (isinstance(issue["suggestion"], str) and bool(issue["suggestion"])),
            f"{label}.issues[{index}].suggestion is invalid",
        )
        issues.append(issue)
    derived_status = (
        "BLOCK"
        if any(issue["severity"] == "BLOCK" for issue in issues)
        else "WARNING"
        if any(issue["severity"] == "WARNING" for issue in issues)
        else "PASS"
    )
    require(derived_status == expected_status, f"{label} status differs from the job result")
    blocker_codes = package_status["blocker_codes"]
    dfm_blocker = (
        blocker_codes[0]
        if len(blocker_codes) == 1 and blocker_codes[0] in DFM_BLOCKER_CODES
        else None
    )
    blocking_codes = sorted({issue["code"] for issue in issues if issue["severity"] == "BLOCK"})
    if dfm_blocker is not None:
        require(bool(issues), f"{label} omits the DFM blocker")
        require(
            blocking_codes == [dfm_blocker],
            f"{label} blocking issues do not match the canonical package blocker",
        )
        if dfm_blocker == STOCK_PROFILE_MISSING:
            require(
                all(
                    (issue["code"] == STOCK_PROFILE_MISSING and issue["severity"] == "BLOCK")
                    or (issue["code"] == DFM_GRAIN_MISSING and issue["severity"] == "WARNING")
                    for issue in issues
                ),
                f"{label} stock-precedence profile contains an unsupported issue",
            )
        else:
            require(
                all(
                    issue["code"] == DFM_GRAIN_MISSING and issue["severity"] == "BLOCK"
                    for issue in issues
                ),
                f"{label} grain profile contains an unsupported issue",
            )
    else:
        require(
            all(issue["severity"] != "BLOCK" for issue in issues),
            f"{label} contradicts the package blocker profile",
        )
    return payload


def verify_package(
    bundle: bytes,
    standalone_manifest: bytes,
    standalone_stock_selection: bytes,
    standalone_generation_plan: bytes,
    *,
    project_id: str,
    revision: int,
    design_hash: str,
    generation_context_hash: str,
    expected_workshop_readiness: dict[str, Any],
    expected_review_package_status: dict[str, Any],
    expected_dfm_status: str,
    required_part_id: str | None = None,
) -> dict[str, Any]:
    expected_package_status = verify_design_review_package_status(
        expected_review_package_status,
        label="job design-review package status",
    )
    cam_blocked = expected_package_status["cam_status"] == "BLOCKED"
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
        require(
            len(names) == len(set(names)) == len({name.casefold() for name in names}),
            "ZIP contains duplicate paths",
        )
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
            manifest.get("schema_version") == PRODUCTION_MANIFEST_SCHEMA_VERSION,
            "unsupported manifest schema",
        )
        require(manifest.get("project_id") == project_id, "manifest project_id mismatch")
        require(manifest.get("revision") == str(revision), "manifest revision mismatch")
        require(manifest.get("design_hash") == design_hash, "manifest design_hash mismatch")
        require(
            manifest.get("generation_context_hash") == generation_context_hash,
            "manifest generation context hash mismatch",
        )
        require(manifest.get("cad_status") == "GENERATED", "authoritative CAD was not generated")
        require(manifest.get("release_scope") == "design_review", "unsafe manifest release scope")
        require(manifest.get("machine_use") == "validation_only", "unsafe manifest machine use")
        require(
            manifest.get("physical_cutting_authorized") is False,
            "manifest authorizes physical cutting",
        )

        raw_entries = sequence(manifest.get("artifacts"), "manifest artifacts")
        entries = [mapping(item, "manifest artifact") for item in raw_entries]
        inventory: list[tuple[str, str, str]] = []
        for entry in entries:
            require(
                set(entry) == {"path", "media_type", "role", "size_bytes", "sha256"},
                "manifest artifact keys differ",
            )
            inventory.append(
                (
                    string(entry.get("path"), "manifest artifact path"),
                    string(entry.get("role"), "manifest artifact role"),
                    string(entry.get("media_type"), "manifest artifact media type"),
                )
            )
        paths = [path for path, _, _ in inventory]
        require(
            len(paths) == len(set(paths)) == len({path.casefold() for path in paths}),
            "manifest contains duplicate artifact paths",
        )
        require(set(names) == {"manifest.json", *paths}, "ZIP content differs from manifest")
        required_paths = REQUIRED_REVIEW_PACKAGE_PATHS if cam_blocked else REQUIRED_PACKAGE_PATHS
        require(set(paths) >= required_paths, "design-review package is incomplete")
        require(any(path.endswith(".dxf") for path in paths), "package contains no part DXF")
        require(
            any(path.endswith(".svg") and path.startswith("drawings/") for path in paths),
            "package contains no part drawing",
        )
        status_entries = [
            item
            for item in inventory
            if item[0].casefold() == DESIGN_REVIEW_PACKAGE_STATUS_PATH.casefold()
            or item[1].casefold() == DESIGN_REVIEW_PACKAGE_STATUS_ROLE.casefold()
        ]
        require(len(status_entries) == 1, "package status manifest entry is not unique")
        require(
            status_entries[0]
            == (
                DESIGN_REVIEW_PACKAGE_STATUS_PATH,
                DESIGN_REVIEW_PACKAGE_STATUS_ROLE,
                "application/json",
            ),
            "package status manifest entry is not canonical",
        )
        readiness_entries = [
            item
            for item in inventory
            if item[0].casefold() == WORKSHOP_READINESS_PATH.casefold()
            or item[1].casefold() == WORKSHOP_READINESS_ROLE.casefold()
        ]
        require(len(readiness_entries) == 1, "workshop readiness manifest entry is not unique")
        require(
            readiness_entries[0]
            == (WORKSHOP_READINESS_PATH, WORKSHOP_READINESS_ROLE, "application/json"),
            "workshop readiness manifest entry is not canonical",
        )
        dfm_entries = [
            item
            for item in inventory
            if item[0].casefold() == DFM_REPORT_PATH.casefold()
            or item[1].casefold() == DFM_REPORT_ROLE.casefold()
        ]
        require(len(dfm_entries) == 1, "DFM report manifest entry is not unique")
        require(
            dfm_entries[0] == (DFM_REPORT_PATH, DFM_REPORT_ROLE, "application/json"),
            "DFM report manifest entry is not canonical",
        )
        stock_selection_entries = [
            item
            for item in inventory
            if item[0].casefold() == STOCK_SELECTION_PATH.casefold()
            or item[1].casefold() == STOCK_SELECTION_ROLE.casefold()
        ]
        require(len(stock_selection_entries) == 1, "stock selection manifest entry is not unique")
        require(
            stock_selection_entries[0]
            == (STOCK_SELECTION_PATH, STOCK_SELECTION_ROLE, "application/json"),
            "stock selection manifest entry is not canonical",
        )
        archived_stock_selection = archive.read(STOCK_SELECTION_PATH)
        require(
            archived_stock_selection == standalone_stock_selection,
            "ZIP and object-store stock selection snapshots differ",
        )
        stock_selection = _strict_canonical_json_object(
            archived_stock_selection,
            label="stock selection snapshot",
        )
        require(
            set(stock_selection)
            == {"schema_version", "stocks", "assignments", "unmatched_part_ids"},
            "stock selection snapshot keys differ",
        )
        require(
            stock_selection.get("schema_version") == STOCK_SELECTION_SCHEMA_VERSION,
            "stock selection snapshot schema differs",
        )
        require(
            isinstance(stock_selection.get("stocks"), list),
            "stock selection snapshot stock rows are invalid",
        )
        require(
            isinstance(stock_selection.get("assignments"), list)
            and isinstance(stock_selection.get("unmatched_part_ids"), list),
            "stock selection snapshot assignment fields are invalid",
        )
        stock_rows = [
            mapping(item, "stock selection stock")
            for item in sequence(stock_selection["stocks"], "stock selection stocks")
        ]
        stock_ids = [
            string(item.get("stock_id"), "stock selection stock_id") for item in stock_rows
        ]
        require(
            stock_ids == sorted(set(stock_ids)),
            "stock selection stocks are not uniquely sorted",
        )
        stock_profiles_fingerprint = sha256(canonical_json_bytes(stock_rows))
        generation_plan_entries = [
            item
            for item in inventory
            if item[0].casefold() == GENERATION_PLAN_PATH.casefold()
            or item[1].casefold() == GENERATION_PLAN_ROLE.casefold()
        ]
        require(len(generation_plan_entries) == 1, "generation plan manifest entry is not unique")
        require(
            generation_plan_entries[0]
            == (GENERATION_PLAN_PATH, GENERATION_PLAN_ROLE, "application/json"),
            "generation plan manifest entry is not canonical",
        )
        archived_generation_plan = archive.read(GENERATION_PLAN_PATH)
        require(
            archived_generation_plan == standalone_generation_plan,
            "ZIP and object-store generation plans differ",
        )
        verify_generation_plan(
            _strict_canonical_json_object(
                archived_generation_plan,
                label="generation plan",
            ),
            label="generation plan",
            generated_validation_program=(
                None if cam_blocked else expected_package_status["validation_program_included"]
            ),
            expected_stock_profiles_fingerprint=stock_profiles_fingerprint,
        )
        programs = [path for path in paths if path.casefold().endswith(".validation.ngc")]
        if cam_blocked:
            require(
                not any(
                    blocked_cam_artifact_violation(path, role, media_type)
                    for path, role, media_type in inventory
                ),
                "blocked CAM package contains a forbidden manufacturing artifact",
            )
        else:
            require(
                any(path.startswith("nesting/") for path in paths),
                "package contains no nesting map",
            )
            require(
                any(path.startswith("cam/setups/") and path.endswith(".svg") for path in paths),
                "package contains no setup sheet",
            )
            require(programs, "package contains no validation machine program")
        require(
            expected_package_status["validation_program_included"] is bool(programs),
            "package status validation-program flag differs from the manifest inventory",
        )

        for entry in entries:
            path = string(entry.get("path"), "manifest artifact path")
            _safe_zip_path(path)
            payload = archive.read(path)
            require(entry.get("size_bytes") == len(payload), f"size mismatch for {path}")
            require(entry.get("sha256") == sha256(payload), f"SHA-256 mismatch for {path}")

        context = manifest_context(manifest)
        require(
            manifest.get("production_context_hash") == sha256(canonical_json_bytes(context)),
            "manifest production_context_hash mismatch",
        )
        require(archive.read("model/design.step").startswith(b"ISO-10303-21"), "invalid STEP")
        require(archive.read("model/design.glb").startswith(b"glTF"), "invalid GLB")
        operations: dict[str, Any] | None = None
        if not cam_blocked:
            operations = mapping(json.loads(archive.read("cam/operations.json")), "operations")
            require(
                operations.get("schema_version") == OPERATIONS_SCHEMA_VERSION,
                "bad operations schema",
            )
            require(
                operations.get("design_hash") == design_hash,
                "operations design_hash mismatch",
            )
            require(operations.get("mode") == "VALIDATION", "operations are not validation-only")
            verify_explicit_two_sided_registration(operations)
        cad_interchange = mapping(
            json.loads(archive.read("validation/cad-interchange-status.json")),
            "CAD interchange status",
        )
        require(
            cad_interchange.get("status") == "OPTIONAL_NOT_REQUESTED",
            "optional FreeCAD bridge status is ambiguous",
        )
        require(
            cad_interchange.get("runtime_probe_performed") is False,
            "review bundle falsely claims a FreeCAD runtime probe",
        )
        archive_readiness = verify_workshop_readiness(
            _strict_canonical_json_object(
                archive.read(WORKSHOP_READINESS_PATH),
                label="archive workshop readiness",
            ),
            label="archive workshop readiness",
        )
        require(
            canonical_json_bytes(archive_readiness)
            == canonical_json_bytes(expected_workshop_readiness),
            "job and archive workshop readiness differ",
        )
        archive_package_status = verify_design_review_package_status(
            _strict_canonical_json_object(
                archive.read(DESIGN_REVIEW_PACKAGE_STATUS_PATH),
                label="archive design-review package status",
            ),
            label="archive design-review package status",
        )
        require(
            canonical_json_bytes(archive_package_status)
            == canonical_json_bytes(expected_package_status),
            "job and archive design-review package status differ",
        )
        verify_status_readiness_alignment(
            archive_package_status,
            archive_readiness,
            label="archive review contract",
        )
        verify_design_review_dfm_report(
            _strict_canonical_json_object(
                archive.read(DFM_REPORT_PATH),
                label="archive DFM report",
            ),
            package_status=archive_package_status,
            expected_status=expected_dfm_status,
            label="archive DFM report",
        )
        if required_part_id is not None:
            bom_rows = list(
                csv.DictReader(io.StringIO(archive.read("bom/bom.csv").decode("utf-8")))
            )
            require(
                required_part_id in {row.get("part_id") for row in bom_rows},
                "accepted reinforcement is missing from BOM",
            )
            require(
                any(path.startswith(f"parts/{required_part_id}/") for path in paths),
                "accepted reinforcement has no detail drawing or DXF",
            )
            if operations is not None:
                operation_rows = sequence(operations.get("operations"), "CAM operations")
                require(
                    required_part_id
                    in {mapping(item, "CAM operation").get("part_id") for item in operation_rows},
                    "accepted reinforcement is missing from CAM operations",
                )
                nesting_payloads = [
                    archive.read(path)
                    for path in paths
                    if path.startswith("nesting/") and path.endswith(".svg")
                ]
                require(
                    any(
                        required_part_id.encode("utf-8") in payload for payload in nesting_payloads
                    ),
                    "accepted reinforcement is missing from nesting maps",
                )
        for program in programs:
            text = archive.read(program).decode("ascii")
            require("VALIDATION DRY RUN - NOT PRODUCTION APPROVED" in text, "unsafe program marker")
            executable = re.sub(r"\([^)]*\)", "", text)
            require(
                re.search(r"\bM0?[34]\b", executable, re.IGNORECASE) is None,
                f"spindle start found in {program}",
            )
            require(
                re.search(r"\bZ-", executable, re.IGNORECASE) is None,
                f"negative Z found in {program}",
            )
    return manifest


def download_artifact(
    client: HttpClient,
    download_path: str,
    *,
    artifact_id: str,
    artifact_kind: str,
    revision: int,
    expected_size: int,
    expected_content_type: str,
    expected_sha256: str,
) -> HttpResult:
    require(
        download_path == download_path.strip()
        and re.search(r"[\\\x00-\x20\x7f]", download_path) is None,
        "artifact endpoint returned an unsafe download path",
    )
    parsed = urlparse(download_path)
    try:
        canonical_artifact_id = str(uuid.UUID(artifact_id))
    except (AttributeError, ValueError):
        canonical_artifact_id = ""
    query_match = re.fullmatch(
        r"expires=([1-9][0-9]{0,18})&signature=([0-9a-f]{64})",
        parsed.query,
    )
    expires_at = int(query_match.group(1)) if query_match is not None else 0
    now = int(time.time())
    require(
        canonical_artifact_id == artifact_id
        and parsed.scheme == ""
        and parsed.netloc == ""
        and parsed.path == f"/v1/artifacts/{artifact_id}/download"
        and parsed.params == ""
        and parsed.fragment == ""
        and query_match is not None
        and now < expires_at <= now + 3_600,
        "artifact endpoint returned an unsafe download path",
    )
    require(
        type(expected_size) is int and 0 < expected_size <= MAX_ZIP_ENTRY_BYTES,
        "artifact declared an invalid size",
    )
    expected_digest = "sha-256=" + base64.b64encode(
        bytes.fromhex(require_sha256(expected_sha256, "artifact SHA"))
    ).decode("ascii")
    result = client.request(
        "GET",
        download_path,
        expected=(200,),
        follow_redirects=False,
        maximum_body_bytes=expected_size,
        total_read_seconds=client.request_timeout,
    )
    require("location" not in result.headers, "artifact endpoint attempted a redirect")
    require(
        result.headers.get("content-length") == str(expected_size),
        "artifact Content-Length mismatch",
    )
    require(
        result.headers.get("content-type", "").partition(";")[0].strip().lower()
        == expected_content_type.lower(),
        "artifact Content-Type mismatch",
    )
    require(result.headers.get("etag") == f'"{expected_sha256}"', "artifact ETag mismatch")
    require(result.headers.get("digest") == expected_digest, "artifact Digest mismatch")
    disposition = result.headers.get("content-disposition", "")
    expected_filename = {
        "production_bundle": f"custombuild-design-review-rev-{revision}.zip",
        "manifest": f"custombuild-design-review-rev-{revision}-manifest.json",
        "stock_selection": f"custombuild-stock-selection-rev-{revision}.json",
        "generation_plan": f"custombuild-generation-plan-rev-{revision}.json",
        "manufacturing_intent": f"custombuild-manufacturing-intent-rev-{revision}.json",
        "supplier_handoff": f"custombuild-cnc-shop-handoff-rev-{revision}.json",
    }.get(artifact_kind)
    require(
        type(revision) is int
        and revision > 0
        and expected_filename is not None
        and disposition == f'attachment; filename="{expected_filename}"',
        "artifact Content-Disposition is unsafe",
    )
    cache_directives = {
        directive.strip().lower()
        for directive in result.headers.get("cache-control", "").split(",")
        if directive.strip()
    }
    require({"private", "no-store"} <= cache_directives, "artifact may be cached")
    require(
        "no-transform" in cache_directives,
        "artifact representation may be transformed",
    )
    require(
        cache_directives == {"private", "no-store", "no-transform", "max-age=0"},
        "artifact cache policy contains unexpected directives",
    )
    require(len(result.body) == expected_size, "artifact body size mismatch")
    require(sha256(result.body) == expected_sha256, "artifact body SHA mismatch")
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
    *,
    expected_current_revision: int = 0,
) -> dict[str, Any]:
    spec = bookcase_spec(overrides)
    resolver = "autofix" if spec.get("reinforcement_mode") == "auto" else "preview"
    preview = mapping(
        client.json(
            "POST",
            f"/v1/designs/{resolver}?project_id={project_id}",
            payload=spec,
        ),
        "canonical design preview",
    )
    return mapping(
        client.json(
            "POST",
            f"/v1/projects/{project_id}/versions",
            payload={
                "template_id": (
                    "wall-library" if spec.get("furniture_type") == "wall_library" else "shelving"
                ),
                "spec": spec,
                "production_context": {
                    "stock_width_mm": 2_440,
                    "stock_height_mm": 1_220,
                    "stock_count": 4,
                    "back_stock_width_mm": 2_440,
                    "back_stock_height_mm": 1_220,
                    "back_stock_count": 2,
                    "machine_profile_id": "custombuild-router-1325-linuxcnc",
                },
                "expected_design_hash": preview["design_hash"],
                "expected_current_revision": expected_current_revision,
            },
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


def verify_dry_joining_warning(value: Any, *, label: str) -> list[dict[str, Any]]:
    """Require the one intentional DADO retention warning for this fixed fixture."""

    result = mapping(value, label)
    require(result.get("status") == "WARNING", f"{label} status is not WARNING")
    evaluations = [
        mapping(item, f"{label} rule evaluation")
        for item in sequence(result.get("rule_evaluations"), f"{label} rule evaluations")
    ]
    warnings = [item for item in evaluations if item.get("status") == "WARNING"]
    require(
        len(warnings) == 1
        and warnings[0].get("rule_id") == "CB-JOINT-001"
        and warnings[0].get("rule_version") == "1.3.0",
        f"{label} does not expose the canonical dry-joining warning",
    )
    return evaluations


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
    verify_dry_joining_warning(baseline, label="baseline preview")
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
    verify_dry_joining_warning(corrected, label="automatic reinforcement")
    corrected_spec = mapping(corrected.get("spec"), "corrected DesignSpec")
    corrected_parameters = mapping(
        corrected_spec.get("parameters"), "corrected DesignSpec parameters"
    )
    divider_count = integer(
        corrected_parameters.get("vertical_divider_count"),
        "corrected vertical divider count",
    )
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
        any(
            corrected_divider_id in sequence(step.get("part_ids"), "assembly part IDs")
            for step in corrected_steps
        ),
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
        overloaded_spec | {"divider_count": divider_count},
    )
    atelier_version = create_version(atelier, atelier_project_id)
    revision = int(nordic_version["revision"])
    atelier_revision = int(atelier_version["revision"])
    nordic_result = mapping(nordic_version.get("result_json"), "Nordic result")
    rule_evaluations = verify_dry_joining_warning(
        nordic_result,
        label="persisted Nordic result",
    )
    warning_rule_ids = sorted(
        string(item.get("rule_id"), "warning rule ID")
        for item in rule_evaluations
        if item.get("status") == "WARNING"
    )
    require(
        len(warning_rule_ids) == len(set(warning_rule_ids))
        and all(re.fullmatch(r"CB-[A-Z]+-[0-9]{3}", rule_id) for rule_id in warning_rule_ids),
        "acceptance design warnings are not canonical server rules",
    )
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
                "warning_overrides": [
                    {
                        "rule_id": rule_id,
                        "reason": "Current server warning reviewed for this exact revision",
                    }
                    for rule_id in warning_rule_ids
                ],
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
        "postprocessor_id": "linuxcnc-validation-1.1.0",
        "include_step": True,
        "include_freecad_project": False,
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
    generation_context_hash = verify_generation_context_hash(completed_job, job_result)
    require(
        generation_context_hash == first_job.get("production_context_hash"),
        "completed job generation context differs from queued job",
    )
    workshop_readiness = verify_generation_result_safety(job_result)
    review_package_status = verify_design_review_package_status(
        job_result.get("design_review_package_status"),
        label="job design-review package status",
    )
    cam_blocked = review_package_status["cam_status"] == "BLOCKED"

    artifact_values = sequence(nordic.json("GET", f"/v1/jobs/{job_id}/artifacts"), "artifacts")
    artifacts = [mapping(item, "artifact") for item in artifact_values]
    by_kind: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        kind = string(artifact.get("kind"), "artifact kind")
        require(kind not in by_kind, "duplicate artifact kind")
        by_kind[kind] = artifact
    require(
        {
            "production_bundle",
            "manifest",
            "manufacturing_intent",
            "supplier_handoff",
            "cad_interchange_status",
            "dfm_report",
            "stock_selection",
            "generation_plan",
            "design_glb",
            "workshop_readiness",
            "design_review_package_status",
        }
        <= set(by_kind),
        "stored artifacts are missing",
    )
    if cam_blocked:
        require(
            not any(blocked_cam_evidence_kind_is_forbidden(kind) for kind in by_kind),
            "blocked CAM job exposes a manufacturing evidence artifact",
        )

    downloaded: dict[str, bytes] = {}
    downloadable_content_types = {
        "production_bundle": "application/zip",
        "manifest": "application/json",
        "stock_selection": "application/json",
        "generation_plan": "application/json",
        "manufacturing_intent": "application/json",
        "supplier_handoff": "application/json",
    }
    for kind, expected_content_type in downloadable_content_types.items():
        artifact = by_kind[kind]
        require(
            artifact.get("content_type") == expected_content_type,
            f"{kind} content type mismatch",
        )
        path = string(artifact.get("download_path"), f"{kind} download path")
        response = download_artifact(
            nordic,
            path,
            artifact_id=string(artifact.get("id"), f"{kind} artifact id"),
            artifact_kind=kind,
            revision=revision,
            expected_size=integer(artifact.get("size_bytes"), f"{kind} size"),
            expected_content_type=expected_content_type,
            expected_sha256=require_sha256(artifact.get("sha256"), f"{kind} SHA"),
        )
        downloaded[kind] = response.body
    bundle_sha = require_sha256(job_result.get("bundle_sha256"), "job bundle_sha256")
    manifest_sha = require_sha256(job_result.get("manifest_sha256"), "job manifest_sha256")
    require(sha256(downloaded["production_bundle"]) == bundle_sha, "job bundle hash mismatch")
    require(sha256(downloaded["manifest"]) == manifest_sha, "job manifest hash mismatch")
    manifest = verify_package(
        downloaded["production_bundle"],
        downloaded["manifest"],
        downloaded["stock_selection"],
        downloaded["generation_plan"],
        project_id=nordic_project_id,
        revision=revision,
        design_hash=str(nordic_version["design_hash"]),
        generation_context_hash=generation_context_hash,
        expected_workshop_readiness=workshop_readiness,
        expected_review_package_status=review_package_status,
        expected_dfm_status=string(job_result.get("dfm_status"), "job DFM status"),
        required_part_id=divider_part_id,
    )
    require(
        job_result.get("artifact_count") == len(manifest["artifacts"]) + 1,
        "job artifact count mismatch",
    )
    log(
        "production_package_verified",
        job_id=job_id,
        manifest_sha256=manifest_sha,
        cam_status=review_package_status["cam_status"],
    )

    if cam_blocked:
        cam_rejection = nordic.request(
            "POST",
            f"{base}/approve",
            payload={
                "approval_type": "cam",
                "reason": "Live Compose CAM review must remain blocked",
                "generation_job_id": job_id,
                "warning_overrides": [],
            },
            expected=(409,),
        )
        release_rejection = nordic.request(
            "POST",
            f"{base}/release",
            payload={
                "release_number": f"BLOCKED-{run_id.upper()}"[:40],
                "confirmation": "RELEASE",
            },
            expected=(409,),
        )
        verify_blocked_cam_endpoint_rejection(
            cam_rejection,
            review_package_status["blocker_codes"],
            label="CAM approval",
        )
        verify_blocked_cam_endpoint_rejection(
            release_rejection,
            review_package_status["blocker_codes"],
            label="release",
        )
        log(
            "cam_blocked_review_package_verified",
            blocker_codes=review_package_status["blocker_codes"],
        )
        return {
            "project_id": nordic_project_id,
            "revision": revision,
            "job_id": job_id,
            "release_id": None,
            "manifest_sha256": manifest_sha,
            "cam_status": "BLOCKED",
        }

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
    require(
        release.get("release_kind") == "design_review",
        "review lock was incorrectly represented as a physical release",
    )
    require(release.get("manifest_sha256") == manifest_sha, "release manifest hash mismatch")
    require(release.get("machine_use") == "validation_only", "release overstates machine safety")
    frozen = mapping(nordic.json("GET", base), "released version")
    require(
        frozen.get("immutable") is True and frozen.get("status") == "released",
        "released version is not immutable",
    )
    nordic.request("POST", f"{base}/validate", expected=(409,))
    atelier.request("GET", base, expected=(404,))

    replacement = create_version(
        nordic,
        nordic_project_id,
        {"width_mm": 710},
        expected_current_revision=revision,
    )
    require(replacement.get("revision") == revision + 1, "design edit created no new revision")
    superseded = mapping(nordic.json("GET", base), "superseded version")
    require(
        superseded.get("status") == "superseded" and superseded.get("immutable") is True,
        "new design did not supersede the released revision",
    )
    nordic.request("GET", f"/v1/jobs/{job_id}/artifacts", expected=(409,))
    stale_download = string(
        by_kind["production_bundle"].get("download_path"),
        "bundle stale download probe",
    )
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
    parser.add_argument("--request-timeout", type=float, default=30.0)
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
