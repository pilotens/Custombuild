"""Author and validate one workshop-owned production-machine profile.

The draft emitted by ``init`` is deliberately not a production profile.  It
copies only facts that can be derived from a strictly verified design-review
bundle and represents every workshop-owned value with an explicit unresolved
marker.  ``finalize`` is the only command that creates deployable canonical
profile bytes; it refuses unresolved markers, recomputes all derived bindings
and hashes, and validates the result against the exact review bundle.

Successful profile validation does not authorize a physical machine start.
"""

# Repository-path bootstrapping intentionally precedes local package imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Never, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for _source_root in (
    _REPOSITORY_ROOT,
    _REPOSITORY_ROOT / "packages/domain/src",
    _REPOSITORY_ROOT / "packages/rule-engine/src",
    _REPOSITORY_ROOT / "packages/manufacturing/src",
    _REPOSITORY_ROOT / "packages/template-sdk/src",
    _REPOSITORY_ROOT / "cad/src",
    _REPOSITORY_ROOT / "cam/src",
    _REPOSITORY_ROOT / "postprocessors/src",
):
    _source_path = str(_source_root)
    if _source_path not in sys.path:
        sys.path.insert(0, _source_path)

from custombuild_cam import generate_production_toolpaths
from custombuild_cam.production_model import (
    FIXTURE_KEEPOUT_POLICY,
    IDENTITY_SOURCE_TO_WCS_XY,
    MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
    STOCK_TOP_Z0_REFERENCE,
)
from custombuild_manufacturing import (
    MAX_ARTIFACT_BYTES,
    MAX_PRODUCTION_MACHINE_PROFILE_BYTES,
    PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
    SERVER_OWNED_PRODUCTION_PROFILE,
    OperationsDocument,
    canonical_json_bytes,
    linuxcnc_reference_router_1325,
    linuxcnc_reference_router_5125,
    sha256_hex,
)
from custombuild_manufacturing.cam_candidate_package import (
    read_operations_document_from_design_review_bundle,
)
from custombuild_manufacturing.errors import ManufacturingError
from custombuild_manufacturing.production_machine_profile import (
    LoadedProductionMachineProfile,
    ProductionMachineProfileError,
    load_production_machine_profile,
)
from custombuild_postprocessors import (
    CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY,
    EXTERNAL_AXIS_OFFSET_POLICY,
    FEED_SPINDLE_OVERRIDE_POLICY,
    G52_G92_OFFSET_RESET_POLICY,
    G53_TOOL_CHANGE_PATH_COMPLETE,
    HOMING_PREFLIGHT_POLICY,
    LINUXCNC_PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
    M6_TOOL_TABLE_POLICY,
    M6_WCS_TABLE_POLICY,
    METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
    PROGRAM_RESTART_POLICY,
    SPINDLE_AT_SPEED_POLICY,
    LinuxCNCProductionMachineProfile,
    LinuxCNCProductionPostprocessor,
)

DRAFT_SCHEMA_VERSION = "custombuild.production-machine-profile-draft.v1"
MAX_DRAFT_BYTES = 4 * MAX_PRODUCTION_MACHINE_PROFILE_BYTES
MAX_JSON_NESTING = 32
UNRESOLVED_KEY = "$unresolved"
UNRESOLVED_VALUE = "WORKSHOP_INPUT_REQUIRED"
COMPUTED_KEY = "$computed"
POSTPROCESSOR_HASH_VALUE = "POSTPROCESSOR_CONFIG_SHA256"

_DOCUMENT_FIELDS = frozenset({"schema_version", "payload", "payload_sha256"})
_DRAFT_FIELDS = frozenset(
    {"schema_version", "deployable", "design_review_sha256", "requirements", "payload"}
)
_PAYLOAD_FIELDS = frozenset(
    {
        "profile_class",
        "acceptance",
        "machine",
        "postprocessor_profile",
        "setups",
        "tools",
        "recipes",
    }
)
_ACCEPTANCE_FIELDS = frozenset({"status", "evidence_id", "evidence_version", "evidence_sha256"})
_MACHINE_FIELDS = frozenset(
    {
        "source_machine_profile_id",
        "source_machine_profile_version",
        "source_machine_profile_fingerprint",
        "machine_profile_id",
        "machine_profile_version",
        "controller_id",
        "controller_version",
        "work_width_um",
        "work_height_um",
        "work_z_um",
        "machine_x_min_um",
        "machine_x_max_um",
        "machine_y_min_um",
        "machine_y_max_um",
        "machine_z_min_um",
        "machine_z_max_um",
        "min_spindle_rpm",
        "max_spindle_rpm",
        "max_feed_um_min",
        "max_plunge_um_min",
        "tool_catalog_version",
        "recipe_catalog_version",
        "postprocessor_profile_id",
        "postprocessor_profile_version",
        "postprocessor_profile_sha256",
    }
)
_POSTPROCESSOR_FIELDS = frozenset(field.name for field in fields(LinuxCNCProductionMachineProfile))
_WCS_OFFSET_FIELDS = frozenset(
    {"wcs", "machine_x0_um", "machine_y0_um", "machine_z0_um", "machine_xy_rotation_mdeg"}
)
_SETUP_FIELDS = frozenset(
    {
        "setup_id",
        "stock_id",
        "source_material_id",
        "source_material_version",
        "material_id",
        "material_version",
        "material_evidence_id",
        "material_evidence_version",
        "material_evidence_sha256",
        "sheet_index",
        "side",
        "source_setup_sha256",
        "source_to_wcs_xy_transform",
        "wcs",
        "machine_wcs_origin",
        "machine_wcs_z0_um",
        "machine_wcs_xy_rotation_mdeg",
        "stock_width_um",
        "stock_height_um",
        "stock_thickness_um",
        "safe_z_um",
        "minimum_rapid_clearance_um",
        "reference_surface",
        "orientation",
        "fixture",
        "probe_method",
        "keep_out_zones",
        "raw_allowance_um",
        "spoilboard_id",
        "spoilboard_version",
        "spoilboard_sha256",
        "through_cut_allowance_um",
    }
)
_POINT_FIELDS = frozenset({"x_um", "y_um"})
_FIXTURE_FIELDS = frozenset(
    {"fixture_id", "fixture_version", "fixture_sha256", "clearance_z_um", "keep_out_policy"}
)
_RECT_FIELDS = frozenset({"x_um", "y_um", "width_um", "height_um"})
_TOOL_FIELDS = frozenset(
    {
        "tool_id",
        "tool_version",
        "source_tool_id",
        "source_tool_version",
        "source_tool_sha256",
        "controller_tool_number",
        "length_offset_number",
        "expected_length_offset_x_um",
        "expected_length_offset_y_um",
        "expected_length_offset_z_um",
        "tool_table_evidence_id",
        "tool_table_evidence_version",
        "tool_table_evidence_sha256",
        "effective_diameter_um",
        "cutting_length_um",
        "measured_stickout_um",
        "assembly_collision_radius_um",
        "minimum_holder_clearance_um",
        "geometry",
        "center_cutting",
        "drill_point_length_um",
        "spindle_direction",
    }
)
_RECIPE_FIELDS = frozenset(
    {
        "recipe_id",
        "version",
        "machine_profile_id",
        "machine_profile_version",
        "material_id",
        "material_version",
        "tool_id",
        "tool_version",
        "operation_kind",
        "spindle_rpm",
        "feed_um_min",
        "plunge_um_min",
        "stepdown_um",
        "stepover_ppm",
        "peck_depth_um",
        "approach_clearance_um",
        "entry_strategy",
        "diameter_tolerance_um",
        "through_overtravel_um",
        "tab_width_um",
        "tab_height_um",
        "process_accuracy_um",
        "accepted_tolerance_um",
        "countersink_top_diameter_um",
        "countersink_included_angle_mdeg",
    }
)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One stable, machine-readable authoring failure."""

    code: str
    pointer: str
    message: str
    expected: object | None = None
    actual: object | None = None

    def as_json(self) -> dict[str, object]:
        value: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "pointer": self.pointer,
        }
        if self.expected is not None:
            value["expected"] = self.expected
        if self.actual is not None:
            value["actual"] = self.actual
        return value


class ProfileAuthoringError(RuntimeError):
    """The authoring operation was blocked with actionable diagnostics."""

    def __init__(self, diagnostics: Sequence[Diagnostic]) -> None:
        super().__init__(diagnostics[0].message if diagnostics else "profile authoring failed")
        self.diagnostics = tuple(diagnostics)


def _unresolved() -> dict[str, str]:
    return {UNRESOLVED_KEY: UNRESOLVED_VALUE}


def _computed_postprocessor_hash() -> dict[str, str]:
    return {COMPUTED_KEY: POSTPROCESSOR_HASH_VALUE}


def _closed_schema(
    properties: Mapping[str, object],
    *,
    required: frozenset[str] | None = None,
) -> dict[str, object]:
    return {
        "additionalProperties": False,
        "properties": dict(properties),
        "required": sorted(required if required is not None else properties),
        "type": "object",
    }


def production_profile_json_schema() -> dict[str, object]:
    """Return the static Draft 2020-12 syntax contract for finalized profiles.

    Canonical byte encoding, content hashes and cross-object/design-review
    bindings cannot be expressed completely in JSON Schema; ``validate`` and
    ``finalize`` enforce those semantic constraints in addition to this file.
    """

    def ref(name: str) -> dict[str, str]:
        return {"$ref": f"#/$defs/{name}"}

    machine_properties: dict[str, object] = {}
    positive_machine_integers = {
        "work_width_um",
        "work_height_um",
        "work_z_um",
        "min_spindle_rpm",
        "max_spindle_rpm",
        "max_feed_um_min",
        "max_plunge_um_min",
    }
    machine_hashes = {
        "source_machine_profile_fingerprint",
        "postprocessor_profile_sha256",
    }
    machine_integers = {
        "machine_x_min_um",
        "machine_x_max_um",
        "machine_y_min_um",
        "machine_y_max_um",
        "machine_z_min_um",
        "machine_z_max_um",
    }
    for name in sorted(_MACHINE_FIELDS):
        if name == "controller_id":
            machine_properties[name] = {"const": "linuxcnc"}
        elif name in positive_machine_integers:
            machine_properties[name] = ref("positiveInteger")
        elif name in machine_integers:
            machine_properties[name] = {"type": "integer"}
        elif name in machine_hashes:
            machine_properties[name] = ref("sha256")
        else:
            machine_properties[name] = ref("canonicalId")

    policy_constants: dict[str, str] = {
        "continuous_spindle_speed_interlock_policy": (CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY),
        "external_axis_offset_policy": EXTERNAL_AXIS_OFFSET_POLICY,
        "feed_spindle_override_policy": FEED_SPINDLE_OVERRIDE_POLICY,
        "g52_g92_offset_reset_policy": G52_G92_OFFSET_RESET_POLICY,
        "g53_tool_change_path": G53_TOOL_CHANGE_PATH_COMPLETE,
        "homing_preflight_policy": HOMING_PREFLIGHT_POLICY,
        "m6_tool_table_policy": M6_TOOL_TABLE_POLICY,
        "m6_wcs_table_policy": M6_WCS_TABLE_POLICY,
        "metric_xyz_identity_kinematics_policy": METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
        "program_restart_policy": PROGRAM_RESTART_POLICY,
        "schema_version": LINUXCNC_PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
        "spindle_at_speed_policy": SPINDLE_AT_SPEED_POLICY,
    }
    postprocessor_properties: dict[str, object] = {}
    postprocessor_integers = {
        "machine_x_min_um",
        "machine_x_max_um",
        "machine_y_min_um",
        "machine_y_max_um",
        "machine_z_min_um",
        "machine_z_max_um",
        "tool_change_x_um",
        "tool_change_y_um",
        "tool_change_z_um",
    }
    special_booleans = {
        "m6_preserves_axis_position",
        "full_restart_after_abort_required",
    }
    for name in sorted(_POSTPROCESSOR_FIELDS):
        if name in policy_constants:
            postprocessor_properties[name] = {"const": policy_constants[name]}
        elif name == "controller_id":
            postprocessor_properties[name] = {"const": "linuxcnc"}
        elif name == "supported_wcs":
            postprocessor_properties[name] = ref("wcsArray")
        elif name == "wcs_offsets":
            postprocessor_properties[name] = {
                "items": ref("wcsOffset"),
                "minItems": 1,
                "type": "array",
            }
        elif name in postprocessor_integers:
            postprocessor_properties[name] = {"type": "integer"}
        elif name == "spindle_spinup_ms":
            postprocessor_properties[name] = {
                "maximum": 120_000,
                "minimum": 1,
                "type": "integer",
            }
        elif name == "spindle_at_speed_tolerance_ppm":
            postprocessor_properties[name] = {
                "maximum": 100_000,
                "minimum": 1,
                "type": "integer",
            }
        elif name.endswith("_verified") or name in special_booleans:
            postprocessor_properties[name] = {"const": True}
        elif name.endswith("_sha256"):
            postprocessor_properties[name] = ref("sha256")
        else:
            postprocessor_properties[name] = ref("canonicalId")

    setup_properties: dict[str, object] = {
        "fixture": ref("fixture"),
        "keep_out_zones": {"items": ref("rectangle"), "type": "array"},
        "machine_wcs_origin": ref("point"),
        "machine_wcs_xy_rotation_mdeg": {"const": 0},
        "machine_wcs_z0_um": {"type": "integer"},
        "material_evidence_id": ref("canonicalId"),
        "material_evidence_sha256": ref("sha256"),
        "material_evidence_version": ref("canonicalId"),
        "material_id": ref("canonicalId"),
        "material_version": ref("canonicalId"),
        "minimum_rapid_clearance_um": ref("positiveInteger"),
        "orientation": ref("nonEmptyString"),
        "probe_method": ref("nonEmptyString"),
        "raw_allowance_um": {"const": 0},
        "reference_surface": {"const": STOCK_TOP_Z0_REFERENCE},
        "safe_z_um": ref("positiveInteger"),
        "setup_id": ref("canonicalId"),
        "sheet_index": ref("nonNegativeInteger"),
        "side": {"enum": ["A", "B"]},
        "source_material_id": ref("canonicalId"),
        "source_material_version": ref("canonicalId"),
        "source_setup_sha256": ref("sha256"),
        "source_to_wcs_xy_transform": {"const": IDENTITY_SOURCE_TO_WCS_XY},
        "spoilboard_id": ref("nullableCanonicalId"),
        "spoilboard_sha256": ref("nullableSha256"),
        "spoilboard_version": ref("nullableCanonicalId"),
        "stock_height_um": ref("positiveInteger"),
        "stock_id": ref("canonicalId"),
        "stock_thickness_um": ref("positiveInteger"),
        "stock_width_um": ref("positiveInteger"),
        "through_cut_allowance_um": {
            "maximum": 500,
            "minimum": 0,
            "type": "integer",
        },
        "wcs": ref("wcs"),
    }
    tool_properties: dict[str, object] = {
        "assembly_collision_radius_um": ref("positiveInteger"),
        "center_cutting": {"type": "boolean"},
        "controller_tool_number": {
            "maximum": MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
            "minimum": 1,
            "type": "integer",
        },
        "cutting_length_um": ref("positiveInteger"),
        "drill_point_length_um": {"const": 0},
        "effective_diameter_um": ref("positiveInteger"),
        "expected_length_offset_x_um": {"const": 0},
        "expected_length_offset_y_um": {"const": 0},
        "expected_length_offset_z_um": {"type": "integer"},
        "geometry": {"enum": ["COUNTERSINK", "DRILL", "FLAT_END_MILL"]},
        "length_offset_number": {
            "maximum": MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
            "minimum": 1,
            "type": "integer",
        },
        "measured_stickout_um": ref("positiveInteger"),
        "minimum_holder_clearance_um": ref("positiveInteger"),
        "source_tool_id": ref("canonicalId"),
        "source_tool_sha256": ref("sha256"),
        "source_tool_version": ref("canonicalId"),
        "spindle_direction": {"const": "CW"},
        "tool_id": ref("canonicalId"),
        "tool_table_evidence_id": ref("canonicalId"),
        "tool_table_evidence_sha256": ref("sha256"),
        "tool_table_evidence_version": ref("canonicalId"),
        "tool_version": ref("canonicalId"),
    }
    recipe_properties: dict[str, object] = {
        "accepted_tolerance_um": ref("positiveInteger"),
        "approach_clearance_um": ref("positiveInteger"),
        "countersink_included_angle_mdeg": ref("nullablePositiveInteger"),
        "countersink_top_diameter_um": ref("nullablePositiveInteger"),
        "diameter_tolerance_um": ref("nonNegativeInteger"),
        "entry_strategy": {"const": "PLUNGE"},
        "feed_um_min": ref("positiveInteger"),
        "machine_profile_id": ref("canonicalId"),
        "machine_profile_version": ref("canonicalId"),
        "material_id": ref("canonicalId"),
        "material_version": ref("canonicalId"),
        "operation_kind": {"enum": ["CONTOUR", "COUNTERSINK", "DRILL", "GROOVE", "POCKET"]},
        "peck_depth_um": ref("positiveInteger"),
        "plunge_um_min": ref("positiveInteger"),
        "process_accuracy_um": ref("positiveInteger"),
        "recipe_id": ref("canonicalId"),
        "spindle_rpm": ref("positiveInteger"),
        "stepdown_um": ref("positiveInteger"),
        "stepover_ppm": {
            "maximum": 1_000_000,
            "minimum": 1,
            "type": "integer",
        },
        "tab_height_um": ref("nonNegativeInteger"),
        "tab_width_um": ref("nonNegativeInteger"),
        "through_overtravel_um": {
            "maximum": 500,
            "minimum": 0,
            "type": "integer",
        },
        "tool_id": ref("canonicalId"),
        "tool_version": ref("canonicalId"),
        "version": ref("canonicalId"),
    }
    payload_schema = _closed_schema(
        {
            "acceptance": _closed_schema(
                {
                    "evidence_id": ref("nonEmptyString"),
                    "evidence_sha256": ref("sha256"),
                    "evidence_version": ref("nonEmptyString"),
                    "status": {"const": "WORKSHOP_ACCEPTED"},
                },
                required=_ACCEPTANCE_FIELDS,
            ),
            "machine": _closed_schema(machine_properties, required=_MACHINE_FIELDS),
            "postprocessor_profile": _closed_schema(
                postprocessor_properties,
                required=_POSTPROCESSOR_FIELDS,
            ),
            "profile_class": {"const": SERVER_OWNED_PRODUCTION_PROFILE},
            "recipes": {
                "items": _closed_schema(recipe_properties, required=_RECIPE_FIELDS),
                "minItems": 1,
                "type": "array",
            },
            "setups": {
                "items": _closed_schema(setup_properties, required=_SETUP_FIELDS),
                "minItems": 1,
                "type": "array",
            },
            "tools": {
                "items": _closed_schema(tool_properties, required=_TOOL_FIELDS),
                "minItems": 1,
                "type": "array",
            },
        },
        required=_PAYLOAD_FIELDS,
    )
    return {
        "$defs": {
            "canonicalId": {
                "maxLength": 256,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$",
                "type": "string",
            },
            "fixture": _closed_schema(
                {
                    "clearance_z_um": ref("nonNegativeInteger"),
                    "fixture_id": ref("canonicalId"),
                    "fixture_sha256": ref("sha256"),
                    "fixture_version": ref("canonicalId"),
                    "keep_out_policy": {"const": FIXTURE_KEEPOUT_POLICY},
                },
                required=_FIXTURE_FIELDS,
            ),
            "nonEmptyString": {"minLength": 1, "type": "string"},
            "nonNegativeInteger": {"minimum": 0, "type": "integer"},
            "nullableCanonicalId": {
                "oneOf": [{"type": "null"}, ref("canonicalId")],
            },
            "nullablePositiveInteger": {
                "oneOf": [{"type": "null"}, ref("positiveInteger")],
            },
            "nullableSha256": {"oneOf": [{"type": "null"}, ref("sha256")]},
            "point": _closed_schema(
                {"x_um": {"type": "integer"}, "y_um": {"type": "integer"}},
                required=_POINT_FIELDS,
            ),
            "positiveInteger": {"minimum": 1, "type": "integer"},
            "rectangle": _closed_schema(
                {
                    "height_um": ref("positiveInteger"),
                    "width_um": ref("positiveInteger"),
                    "x_um": ref("nonNegativeInteger"),
                    "y_um": ref("nonNegativeInteger"),
                },
                required=_RECT_FIELDS,
            ),
            "sha256": {"pattern": "^[0-9a-f]{64}$", "type": "string"},
            "wcs": {"enum": ["G54", "G55", "G56", "G57", "G58", "G59"]},
            "wcsArray": {
                "items": ref("wcs"),
                "minItems": 1,
                "type": "array",
                "uniqueItems": True,
            },
            "wcsOffset": _closed_schema(
                {
                    "machine_x0_um": {"type": "integer"},
                    "machine_xy_rotation_mdeg": {"const": 0},
                    "machine_y0_um": {"type": "integer"},
                    "machine_z0_um": {"type": "integer"},
                    "wcs": ref("wcs"),
                },
                required=_WCS_OFFSET_FIELDS,
            ),
        },
        "$id": "https://custombuild.invalid/contracts/production-machine-profile.v1.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "description": (
            "Static syntax contract only. Use scripts.production_machine_profile validate "
            "for hashes, canonical bytes, source bindings and CAM semantics."
        ),
        "properties": {
            "payload": payload_schema,
            "payload_sha256": ref("sha256"),
            "schema_version": {"const": PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION},
        },
        "required": sorted(_DOCUMENT_FIELDS),
        "title": "Custombuild workshop-owned production machine profile v1",
        "type": "object",
    }


def _json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _pointer(parent: str, child: str | int) -> str:
    token = _json_pointer_token(str(child))
    return f"{parent}/{token}" if parent else f"/{token}"


def _read_bounded_regular_file(path: Path, *, label: str, limit: int) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProfileAuthoringError(
                (
                    Diagnostic(
                        "INPUT_NOT_REGULAR",
                        "",
                        f"{label} must be a regular non-symlink file",
                    ),
                )
            )
        if not 1 <= before.st_size <= limit:
            raise ProfileAuthoringError(
                (
                    Diagnostic(
                        "INPUT_SIZE_INVALID",
                        "",
                        f"{label} byte size is outside 1..{limit}",
                        expected=f"1..{limit}",
                        actual=before.st_size,
                    ),
                )
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            payload = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
    except ProfileAuthoringError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ProfileAuthoringError(
            (Diagnostic("INPUT_READ_FAILED", "", f"cannot safely read {label}: {exc}"),)
        ) from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity or len(payload) != before.st_size:
        raise ProfileAuthoringError(
            (Diagnostic("INPUT_CHANGED_DURING_READ", "", f"{label} changed while being read"),)
        )
    return payload


def _write_new_file(path: Path, payload: bytes) -> None:
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            with suppress(OSError):
                path.unlink()
        raise ProfileAuthoringError(
            (Diagnostic("OUTPUT_CREATE_FAILED", "", f"cannot exclusively create output: {exc}"),)
        ) from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileAuthoringError(
                (
                    Diagnostic(
                        "DUPLICATE_KEY",
                        "",
                        f"JSON contains duplicate key {key!r}",
                    ),
                )
            )
        result[key] = value
    return result


def _reject_non_integer_number(value: str) -> Never:
    raise ProfileAuthoringError(
        (
            Diagnostic(
                "NON_INTEGER_NUMBER",
                "",
                "profile numbers must be exact JSON integers",
                actual=value,
            ),
        )
    )


def _parse_json(payload: bytes, *, label: str) -> object:
    try:
        return cast(
            object,
            json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_float=_reject_non_integer_number,
                parse_constant=_reject_non_integer_number,
            ),
        )
    except ProfileAuthoringError:
        raise
    except (UnicodeDecodeError, RecursionError, ValueError) as exc:
        raise ProfileAuthoringError(
            (Diagnostic("INVALID_JSON", "", f"{label} is not valid UTF-8 JSON: {exc}"),)
        ) from exc


def _source_machine_fingerprint(source: OperationsDocument) -> str:
    matches = tuple(
        profile
        for profile in (
            linuxcnc_reference_router_1325(),
            linuxcnc_reference_router_5125(),
        )
        if profile.profile_id == source.machine_profile_id
        and profile.version == source.machine_profile_version
    )
    if len(matches) != 1:
        raise ProfileAuthoringError(
            (
                Diagnostic(
                    "SOURCE_MACHINE_UNSUPPORTED",
                    "/requirements/source_machine_profile_id",
                    "design review does not bind one supported validation-machine profile",
                    actual=f"{source.machine_profile_id}@{source.machine_profile_version}",
                ),
            )
        )
    return sha256_hex(canonical_json_bytes(matches[0]))


def _rect_json(value: object) -> dict[str, int]:
    rectangle = cast(Any, value)
    return {
        "height_um": int(rectangle.height_um),
        "width_um": int(rectangle.width_um),
        "x_um": int(rectangle.x_um),
        "y_um": int(rectangle.y_um),
    }


def _recipe_requirements(source: OperationsDocument) -> list[dict[str, object]]:
    setups = {setup.setup_id: setup for setup in source.setups}
    grouped: dict[tuple[str, int, str, str, str, str], list[str]] = {}
    for operation in source.operations:
        setup = setups[operation.setup_id]
        key = (
            setup.stock_id,
            setup.sheet_index,
            setup.material_id,
            setup.material_version,
            operation.tool_id,
            operation.kind.value,
        )
        grouped.setdefault(key, []).append(operation.operation_id)
    return [
        {
            "source_material_id": material_id,
            "source_material_version": material_version,
            "operation_ids": sorted(operation_ids),
            "operation_kind": operation_kind,
            "recipe_index": index,
            "sheet_index": sheet_index,
            "source_tool_id": source_tool_id,
            "stock_id": stock_id,
        }
        for index, (
            (
                (
                    stock_id,
                    sheet_index,
                    material_id,
                    material_version,
                    source_tool_id,
                    operation_kind,
                ),
                operation_ids,
            )
        ) in enumerate(sorted(grouped.items()))
    ]


def _draft_requirements(
    source: OperationsDocument,
    *,
    source_machine_fingerprint: str,
) -> dict[str, object]:
    return {
        "design_hash": source.design_hash,
        "recipe_bindings": _recipe_requirements(source),
        "source_machine_profile_fingerprint": source_machine_fingerprint,
        "source_machine_profile_id": source.machine_profile_id,
        "source_machine_profile_version": source.machine_profile_version,
    }


def _postprocessor_draft() -> dict[str, object]:
    fixed: dict[str, object] = {
        "continuous_spindle_speed_interlock_policy": (CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY),
        "controller_id": "linuxcnc",
        "external_axis_offset_policy": EXTERNAL_AXIS_OFFSET_POLICY,
        "feed_spindle_override_policy": FEED_SPINDLE_OVERRIDE_POLICY,
        "g52_g92_offset_reset_policy": G52_G92_OFFSET_RESET_POLICY,
        "g53_tool_change_path": G53_TOOL_CHANGE_PATH_COMPLETE,
        "homing_preflight_policy": HOMING_PREFLIGHT_POLICY,
        "m6_tool_table_policy": M6_TOOL_TABLE_POLICY,
        "m6_wcs_table_policy": M6_WCS_TABLE_POLICY,
        "metric_xyz_identity_kinematics_policy": METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
        "program_restart_policy": PROGRAM_RESTART_POLICY,
        "schema_version": LINUXCNC_PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
        "spindle_at_speed_policy": SPINDLE_AT_SPEED_POLICY,
    }
    return {
        name: deepcopy(fixed[name]) if name in fixed else _unresolved()
        for name in sorted(_POSTPROCESSOR_FIELDS)
    }


def _setup_draft(source: OperationsDocument) -> list[dict[str, object]]:
    through_setup_ids = {operation.setup_id for operation in source.operations if operation.through}
    output: list[dict[str, object]] = []
    for setup in sorted(source.setups, key=lambda value: value.setup_id):
        through = setup.setup_id in through_setup_ids
        output.append(
            {
                "fixture": {
                    "clearance_z_um": _unresolved(),
                    "fixture_id": _unresolved(),
                    "fixture_sha256": _unresolved(),
                    "fixture_version": _unresolved(),
                    "keep_out_policy": FIXTURE_KEEPOUT_POLICY,
                },
                "keep_out_zones": [_rect_json(zone) for zone in setup.keep_out_zones],
                "machine_wcs_origin": {"x_um": _unresolved(), "y_um": _unresolved()},
                "machine_wcs_xy_rotation_mdeg": _unresolved(),
                "machine_wcs_z0_um": _unresolved(),
                "material_evidence_id": _unresolved(),
                "material_evidence_sha256": _unresolved(),
                "material_evidence_version": _unresolved(),
                "material_id": _unresolved(),
                "material_version": _unresolved(),
                "minimum_rapid_clearance_um": _unresolved(),
                "orientation": setup.orientation,
                "probe_method": _unresolved(),
                "raw_allowance_um": 0,
                "reference_surface": STOCK_TOP_Z0_REFERENCE,
                "safe_z_um": _unresolved(),
                "setup_id": setup.setup_id,
                "sheet_index": setup.sheet_index,
                "side": setup.side.value,
                "source_material_id": setup.material_id,
                "source_material_version": setup.material_version,
                "source_setup_sha256": sha256_hex(canonical_json_bytes(setup)),
                "source_to_wcs_xy_transform": IDENTITY_SOURCE_TO_WCS_XY,
                "spoilboard_id": _unresolved() if through else None,
                "spoilboard_sha256": _unresolved() if through else None,
                "spoilboard_version": _unresolved() if through else None,
                "stock_height_um": setup.stock_height_um,
                "stock_id": setup.stock_id,
                "stock_thickness_um": setup.stock_thickness_um,
                "stock_width_um": setup.stock_width_um,
                "through_cut_allowance_um": _unresolved() if through else 0,
                "wcs": _unresolved(),
            }
        )
    return output


def _tool_draft(source: OperationsDocument) -> list[dict[str, object]]:
    return [
        {
            "assembly_collision_radius_um": _unresolved(),
            "center_cutting": _unresolved(),
            "controller_tool_number": _unresolved(),
            "cutting_length_um": _unresolved(),
            "drill_point_length_um": _unresolved(),
            "effective_diameter_um": _unresolved(),
            "expected_length_offset_x_um": _unresolved(),
            "expected_length_offset_y_um": _unresolved(),
            "expected_length_offset_z_um": _unresolved(),
            "geometry": _unresolved(),
            "length_offset_number": _unresolved(),
            "measured_stickout_um": _unresolved(),
            "minimum_holder_clearance_um": _unresolved(),
            "source_tool_id": tool.tool_id,
            "source_tool_sha256": sha256_hex(canonical_json_bytes(tool)),
            "source_tool_version": tool.version,
            "spindle_direction": "CW",
            "tool_id": _unresolved(),
            "tool_table_evidence_id": _unresolved(),
            "tool_table_evidence_sha256": _unresolved(),
            "tool_table_evidence_version": _unresolved(),
            "tool_version": _unresolved(),
        }
        for tool in sorted(source.tools, key=lambda value: value.tool_id)
    ]


def _recipe_draft(source: OperationsDocument) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for requirement in _recipe_requirements(source):
        kind = cast(str, requirement["operation_kind"])
        contour = kind == "CONTOUR"
        drill = kind == "DRILL"
        countersink = kind == "COUNTERSINK"
        output.append(
            {
                "accepted_tolerance_um": _unresolved(),
                "approach_clearance_um": _unresolved(),
                "countersink_included_angle_mdeg": _unresolved() if countersink else None,
                "countersink_top_diameter_um": _unresolved() if countersink else None,
                "diameter_tolerance_um": _unresolved() if drill else 0,
                "entry_strategy": "PLUNGE",
                "feed_um_min": _unresolved(),
                "machine_profile_id": _unresolved(),
                "machine_profile_version": _unresolved(),
                "material_id": _unresolved(),
                "material_version": _unresolved(),
                "operation_kind": kind,
                "peck_depth_um": _unresolved(),
                "plunge_um_min": _unresolved(),
                "process_accuracy_um": _unresolved(),
                "recipe_id": _unresolved(),
                "spindle_rpm": _unresolved(),
                "stepdown_um": _unresolved(),
                "stepover_ppm": _unresolved(),
                "tab_height_um": _unresolved() if contour else 0,
                "tab_width_um": _unresolved() if contour else 0,
                "through_overtravel_um": _unresolved() if contour else 0,
                "tool_id": _unresolved(),
                "tool_version": _unresolved(),
                "version": _unresolved(),
            }
        )
    return output


def build_profile_draft(
    design_review_bundle: bytes,
) -> tuple[dict[str, object], OperationsDocument]:
    """Build a deterministic, conspicuously non-deployable authoring draft."""

    try:
        source = read_operations_document_from_design_review_bundle(design_review_bundle)
    except (ManufacturingError, TypeError, ValueError) as exc:
        raise ProfileAuthoringError(
            (
                Diagnostic(
                    "DESIGN_REVIEW_INVALID",
                    "/design_review_sha256",
                    f"design-review verification failed: {exc}",
                ),
            )
        ) from exc
    source_fingerprint = _source_machine_fingerprint(source)
    machine: dict[str, object] = {name: _unresolved() for name in sorted(_MACHINE_FIELDS)}
    machine.update(
        {
            "controller_id": "linuxcnc",
            "postprocessor_profile_sha256": _computed_postprocessor_hash(),
            "source_machine_profile_fingerprint": source_fingerprint,
            "source_machine_profile_id": source.machine_profile_id,
            "source_machine_profile_version": source.machine_profile_version,
        }
    )
    draft: dict[str, object] = {
        "deployable": False,
        "design_review_sha256": sha256_hex(design_review_bundle),
        "payload": {
            "acceptance": {
                "evidence_id": _unresolved(),
                "evidence_sha256": _unresolved(),
                "evidence_version": _unresolved(),
                "status": _unresolved(),
            },
            "machine": machine,
            "postprocessor_profile": _postprocessor_draft(),
            "profile_class": SERVER_OWNED_PRODUCTION_PROFILE,
            "recipes": _recipe_draft(source),
            "setups": _setup_draft(source),
            "tools": _tool_draft(source),
        },
        "requirements": _draft_requirements(
            source,
            source_machine_fingerprint=source_fingerprint,
        ),
        "schema_version": DRAFT_SCHEMA_VERSION,
    }
    return draft, source


def _find_unresolved(value: object, *, pointer: str = "") -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    pending: list[tuple[str, object]] = [(pointer, value)]
    while pending:
        current_pointer, current = pending.pop()
        if isinstance(current, Mapping):
            mapping = cast(Mapping[object, object], current)
            if mapping == {UNRESOLVED_KEY: UNRESOLVED_VALUE}:
                diagnostics.append(
                    Diagnostic(
                        "WORKSHOP_FACT_UNRESOLVED",
                        current_pointer,
                        "workshop-owned fact is still unresolved",
                    )
                )
                continue
            children = sorted(mapping.items(), key=lambda item: str(item[0]))
            pending.extend(
                (
                    _pointer(current_pointer, str(key)),
                    item,
                )
                for key, item in reversed(children)
            )
        elif isinstance(current, list):
            pending.extend(
                (_pointer(current_pointer, index), item)
                for index, item in reversed(tuple(enumerate(current)))
            )
    return diagnostics


def _nesting_diagnostic(value: object) -> Diagnostic | None:
    pending: list[tuple[str, object, int]] = [("", value, 0)]
    while pending:
        pointer, current, depth = pending.pop()
        if depth > MAX_JSON_NESTING:
            return Diagnostic(
                "NESTING_TOO_DEEP",
                pointer,
                "JSON nesting exceeds the bounded production-profile contract",
                expected=f"at most {MAX_JSON_NESTING} object/array levels",
                actual=depth,
            )
        if isinstance(current, Mapping):
            pending.extend(
                (_pointer(pointer, str(key)), item, depth + 1) for key, item in current.items()
            )
        elif isinstance(current, list):
            pending.extend(
                (_pointer(pointer, index), item, depth + 1) for index, item in enumerate(current)
            )
    return None


def _object_at(
    value: object,
    *,
    pointer: str,
    expected: frozenset[str],
    diagnostics: list[Diagnostic],
) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        diagnostics.append(
            Diagnostic("TYPE_MISMATCH", pointer, "value must be a JSON object", expected="object")
        )
        return None
    mapping = {cast(str, key): item for key, item in value.items()}
    for missing in sorted(expected - mapping.keys()):
        diagnostics.append(
            Diagnostic(
                "MISSING_FIELD",
                _pointer(pointer, missing),
                "required field is missing",
            )
        )
    for unknown in sorted(mapping.keys() - expected):
        diagnostics.append(
            Diagnostic(
                "UNKNOWN_FIELD",
                _pointer(pointer, unknown),
                "field is not part of the closed v1 contract",
            )
        )
    return mapping


def _array_at(
    value: object,
    *,
    pointer: str,
    diagnostics: list[Diagnostic],
) -> list[object] | None:
    if not isinstance(value, list):
        diagnostics.append(
            Diagnostic("TYPE_MISMATCH", pointer, "value must be a JSON array", expected="array")
        )
        return None
    return cast(list[object], value)


def _shape_diagnostics(document: object) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    root = _object_at(document, pointer="", expected=_DOCUMENT_FIELDS, diagnostics=diagnostics)
    if root is None:
        return diagnostics
    payload = _object_at(
        root.get("payload"),
        pointer="/payload",
        expected=_PAYLOAD_FIELDS,
        diagnostics=diagnostics,
    )
    if payload is None:
        return diagnostics
    _object_at(
        payload.get("acceptance"),
        pointer="/payload/acceptance",
        expected=_ACCEPTANCE_FIELDS,
        diagnostics=diagnostics,
    )
    _object_at(
        payload.get("machine"),
        pointer="/payload/machine",
        expected=_MACHINE_FIELDS,
        diagnostics=diagnostics,
    )
    postprocessor = _object_at(
        payload.get("postprocessor_profile"),
        pointer="/payload/postprocessor_profile",
        expected=_POSTPROCESSOR_FIELDS,
        diagnostics=diagnostics,
    )
    if postprocessor is not None:
        offsets = _array_at(
            postprocessor.get("wcs_offsets"),
            pointer="/payload/postprocessor_profile/wcs_offsets",
            diagnostics=diagnostics,
        )
        if offsets is not None:
            for index, offset in enumerate(offsets):
                _object_at(
                    offset,
                    pointer=f"/payload/postprocessor_profile/wcs_offsets/{index}",
                    expected=_WCS_OFFSET_FIELDS,
                    diagnostics=diagnostics,
                )
    setups = _array_at(payload.get("setups"), pointer="/payload/setups", diagnostics=diagnostics)
    if setups is not None:
        for index, value in enumerate(setups):
            pointer = f"/payload/setups/{index}"
            setup = _object_at(
                value,
                pointer=pointer,
                expected=_SETUP_FIELDS,
                diagnostics=diagnostics,
            )
            if setup is None:
                continue
            _object_at(
                setup.get("machine_wcs_origin"),
                pointer=f"{pointer}/machine_wcs_origin",
                expected=_POINT_FIELDS,
                diagnostics=diagnostics,
            )
            _object_at(
                setup.get("fixture"),
                pointer=f"{pointer}/fixture",
                expected=_FIXTURE_FIELDS,
                diagnostics=diagnostics,
            )
            zones = _array_at(
                setup.get("keep_out_zones"),
                pointer=f"{pointer}/keep_out_zones",
                diagnostics=diagnostics,
            )
            if zones is not None:
                for zone_index, zone in enumerate(zones):
                    _object_at(
                        zone,
                        pointer=f"{pointer}/keep_out_zones/{zone_index}",
                        expected=_RECT_FIELDS,
                        diagnostics=diagnostics,
                    )
    tools = _array_at(payload.get("tools"), pointer="/payload/tools", diagnostics=diagnostics)
    if tools is not None:
        for index, tool in enumerate(tools):
            _object_at(
                tool,
                pointer=f"/payload/tools/{index}",
                expected=_TOOL_FIELDS,
                diagnostics=diagnostics,
            )
    recipes = _array_at(payload.get("recipes"), pointer="/payload/recipes", diagnostics=diagnostics)
    if recipes is not None:
        for index, recipe in enumerate(recipes):
            _object_at(
                recipe,
                pointer=f"/payload/recipes/{index}",
                expected=_RECIPE_FIELDS,
                diagnostics=diagnostics,
            )
    return diagnostics


def _binding_diagnostics(
    document: dict[str, object],
    source: OperationsDocument,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    payload = cast(dict[str, object], document["payload"])
    machine = cast(dict[str, object], payload["machine"])
    expected_machine = {
        "source_machine_profile_fingerprint": _source_machine_fingerprint(source),
        "source_machine_profile_id": source.machine_profile_id,
        "source_machine_profile_version": source.machine_profile_version,
    }
    for field, expected in expected_machine.items():
        actual = machine.get(field)
        if actual != expected:
            diagnostics.append(
                Diagnostic(
                    "SOURCE_BINDING_MISMATCH",
                    f"/payload/machine/{field}",
                    "field differs from the strictly verified design review",
                    expected=expected,
                    actual=actual,
                )
            )

    setup_values = cast(list[object], payload["setups"])
    source_setups = {setup.setup_id: setup for setup in source.setups}
    actual_setup_ids = [
        value.get("setup_id") if isinstance(value, dict) else None for value in setup_values
    ]
    actual_setup_id_set = {value for value in actual_setup_ids if isinstance(value, str)}
    if actual_setup_id_set != set(source_setups) or len(actual_setup_ids) != len(source_setups):
        diagnostics.append(
            Diagnostic(
                "SOURCE_SETUP_COVERAGE_MISMATCH",
                "/payload/setups",
                "setups must exactly cover design-review setup IDs",
                expected=sorted(source_setups),
                actual=actual_setup_ids,
            )
        )
    for index, value in enumerate(setup_values):
        if not isinstance(value, dict):
            continue
        setup_id = value.get("setup_id")
        if not isinstance(setup_id, str) or setup_id not in source_setups:
            continue
        source_setup = source_setups[setup_id]
        expected_setup_fields: dict[str, object] = {
            "orientation": source_setup.orientation,
            "setup_id": source_setup.setup_id,
            "sheet_index": source_setup.sheet_index,
            "side": source_setup.side.value,
            "source_material_id": source_setup.material_id,
            "source_material_version": source_setup.material_version,
            "source_setup_sha256": sha256_hex(canonical_json_bytes(source_setup)),
            "stock_height_um": source_setup.stock_height_um,
            "stock_id": source_setup.stock_id,
            "stock_thickness_um": source_setup.stock_thickness_um,
            "stock_width_um": source_setup.stock_width_um,
        }
        for field, expected_value in expected_setup_fields.items():
            actual = value.get(field)
            if actual != expected_value:
                diagnostics.append(
                    Diagnostic(
                        "SOURCE_BINDING_MISMATCH",
                        f"/payload/setups/{index}/{field}",
                        "field differs from the strictly verified design review",
                        expected=expected_value,
                        actual=actual,
                    )
                )

    tool_values = cast(list[object], payload["tools"])
    source_tools = {tool.tool_id: tool for tool in source.tools}
    actual_source_ids = [
        value.get("source_tool_id") if isinstance(value, dict) else None for value in tool_values
    ]
    actual_source_id_set = {value for value in actual_source_ids if isinstance(value, str)}
    if actual_source_id_set != set(source_tools) or len(actual_source_ids) != len(source_tools):
        diagnostics.append(
            Diagnostic(
                "SOURCE_TOOL_COVERAGE_MISMATCH",
                "/payload/tools",
                "tool mappings must exactly cover design-review source tools",
                expected=sorted(source_tools),
                actual=actual_source_ids,
            )
        )
    for index, value in enumerate(tool_values):
        if not isinstance(value, dict):
            continue
        source_tool_id = value.get("source_tool_id")
        if not isinstance(source_tool_id, str) or source_tool_id not in source_tools:
            continue
        source_tool = source_tools[source_tool_id]
        expected_tool_fields: dict[str, object] = {
            "source_tool_id": source_tool.tool_id,
            "source_tool_sha256": sha256_hex(canonical_json_bytes(source_tool)),
            "source_tool_version": source_tool.version,
        }
        for field, expected_value in expected_tool_fields.items():
            actual = value.get(field)
            if actual != expected_value:
                diagnostics.append(
                    Diagnostic(
                        "SOURCE_BINDING_MISMATCH",
                        f"/payload/tools/{index}/{field}",
                        "field differs from the strictly verified design review",
                        expected=expected_value,
                        actual=actual,
                    )
                )

    bound_setups = {
        value["setup_id"]: value
        for value in setup_values
        if isinstance(value, dict)
        and isinstance(value.get("setup_id"), str)
        and isinstance(value.get("material_id"), str)
        and isinstance(value.get("material_version"), str)
    }
    bound_tools = {
        value["source_tool_id"]: value
        for value in tool_values
        if isinstance(value, dict)
        and isinstance(value.get("source_tool_id"), str)
        and isinstance(value.get("tool_id"), str)
        and isinstance(value.get("tool_version"), str)
    }
    expected_recipe_keys: set[tuple[str, str, str, str, str]] = set()
    for operation in source.operations:
        setup = bound_setups.get(operation.setup_id)
        tool = bound_tools.get(operation.tool_id)
        if setup is None or tool is None:
            continue
        expected_recipe_keys.add(
            (
                cast(str, setup["material_id"]),
                cast(str, setup["material_version"]),
                cast(str, tool["tool_id"]),
                cast(str, tool["tool_version"]),
                operation.kind.value,
            )
        )
    recipe_values = cast(list[object], payload["recipes"])
    actual_recipe_keys = {
        (
            cast(str, value["material_id"]),
            cast(str, value["material_version"]),
            cast(str, value["tool_id"]),
            cast(str, value["tool_version"]),
            cast(str, value["operation_kind"]),
        )
        for value in recipe_values
        if isinstance(value, dict)
        and all(
            isinstance(value.get(field), str)
            for field in (
                "material_id",
                "material_version",
                "tool_id",
                "tool_version",
                "operation_kind",
            )
        )
    }
    if expected_recipe_keys != actual_recipe_keys:
        diagnostics.append(
            Diagnostic(
                "RECIPE_COVERAGE_MISMATCH",
                "/payload/recipes",
                "recipes must exactly cover actual setup material, mapped tool and operation kind",
                expected=sorted(expected_recipe_keys),
                actual=sorted(actual_recipe_keys),
            )
        )
    return diagnostics


def _error_pointer(message: str) -> str:
    lower_message = message.lower()
    exact_fragments = (
        ("unsupported production machine profile schema", "/schema_version"),
        ("unsupported production profile class", "/payload/profile_class"),
        ("server-owned production profile is not workshop accepted", "/payload/acceptance/status"),
        ("test_only profile must have test_only acceptance status", "/payload/acceptance/status"),
    )
    for fragment, pointer in exact_fragments:
        if fragment in lower_message:
            return pointer
    indexed = re.search(r"(setups|tools|recipes)\[(\d+)\](?:\.([A-Za-z0-9_.]+))?", message)
    if indexed:
        pointer = f"/payload/{indexed.group(1)}/{indexed.group(2)}"
        if indexed.group(3):
            pointer += "/" + indexed.group(3).replace(".", "/")
        return pointer
    for prefix in ("acceptance", "machine"):
        field_match = re.search(rf"{prefix}\.([A-Za-z0-9_]+)", message)
        if field_match:
            return f"/payload/{prefix}/{field_match.group(1)}"
    for field in sorted(_POSTPROCESSOR_FIELDS, key=len, reverse=True):
        if field in message:
            return f"/payload/postprocessor_profile/{field}"
    setup_fragments = (
        "raw allowance",
        "reference surface",
        "source-to-wcs",
        "stock-top",
        "wcs xy rotation",
    )
    tool_fragments = ("drill point length", "tool-length offsets")
    if "postprocessor" in lower_message:
        return "/payload/postprocessor_profile"
    if "setup" in lower_message or any(fragment in lower_message for fragment in setup_fragments):
        return "/payload/setups"
    if "tool" in lower_message or any(fragment in lower_message for fragment in tool_fragments):
        return "/payload/tools"
    if "recipe" in lower_message:
        return "/payload/recipes"
    return "/payload"


def validate_profile_bytes(
    design_review_bundle: bytes,
    profile_document: bytes,
) -> tuple[LoadedProductionMachineProfile | None, tuple[Diagnostic, ...]]:
    """Read-only validation against syntax, hashes and exact source bindings."""

    try:
        source = read_operations_document_from_design_review_bundle(design_review_bundle)
    except (ManufacturingError, TypeError, ValueError) as exc:
        return None, (
            Diagnostic(
                "DESIGN_REVIEW_INVALID",
                "/design_review_sha256",
                f"design-review verification failed: {exc}",
            ),
        )
    try:
        raw_document = _parse_json(profile_document, label="production profile")
    except ProfileAuthoringError as exc:
        return None, exc.diagnostics
    nesting_diagnostic = _nesting_diagnostic(raw_document)
    if nesting_diagnostic is not None:
        return None, (nesting_diagnostic,)
    diagnostics = _shape_diagnostics(raw_document)
    if diagnostics or not isinstance(raw_document, dict):
        return None, tuple(diagnostics)
    document = cast(dict[str, object], raw_document)
    try:
        canonical = canonical_json_bytes(document)
    except (TypeError, ValueError) as exc:
        return None, (
            Diagnostic("NON_CANONICAL_JSON", "", f"profile is not canonical JSON data: {exc}"),
        )
    if canonical != profile_document:
        diagnostics.append(
            Diagnostic(
                "NON_CANONICAL_JSON",
                "",
                "profile bytes are not canonical; use finalize instead of editing final JSON",
                expected="sorted compact UTF-8 JSON without a trailing newline",
            )
        )
    payload = cast(dict[str, object], document["payload"])
    declared_payload_hash = document.get("payload_sha256")
    actual_payload_hash = sha256_hex(canonical_json_bytes(payload))
    if declared_payload_hash != actual_payload_hash:
        diagnostics.append(
            Diagnostic(
                "PAYLOAD_SHA256_MISMATCH",
                "/payload_sha256",
                "payload digest does not match canonical payload bytes",
                expected=actual_payload_hash,
                actual=declared_payload_hash,
            )
        )
    machine = cast(dict[str, object], payload["machine"])
    postprocessor = cast(dict[str, object], payload["postprocessor_profile"])
    actual_post_hash = sha256_hex(canonical_json_bytes(postprocessor))
    if machine.get("postprocessor_profile_sha256") != actual_post_hash:
        diagnostics.append(
            Diagnostic(
                "POSTPROCESSOR_SHA256_MISMATCH",
                "/payload/machine/postprocessor_profile_sha256",
                "postprocessor binding does not match canonical nested profile bytes",
                expected=actual_post_hash,
                actual=machine.get("postprocessor_profile_sha256"),
            )
        )
    diagnostics.extend(_binding_diagnostics(document, source))
    if diagnostics:
        return None, tuple(diagnostics)
    try:
        loaded = load_production_machine_profile(profile_document)
        toolpaths = generate_production_toolpaths(source, loaded.execution_context)
        LinuxCNCProductionPostprocessor(loaded.postprocessor_profile).generate(toolpaths)
    except (ManufacturingError, ProductionMachineProfileError, TypeError, ValueError) as exc:
        message = str(exc)
        return None, (
            Diagnostic(
                "PROFILE_SEMANTIC_INVALID",
                _error_pointer(message),
                message,
            ),
        )
    return loaded, ()


def finalize_profile_draft(
    design_review_bundle: bytes,
    draft_document: bytes,
) -> tuple[bytes | None, tuple[Diagnostic, ...]]:
    """Resolve computed fields and return valid canonical production bytes."""

    try:
        source = read_operations_document_from_design_review_bundle(design_review_bundle)
        raw_draft = _parse_json(draft_document, label="production-profile draft")
    except ProfileAuthoringError as exc:
        return None, exc.diagnostics
    except (ManufacturingError, TypeError, ValueError) as exc:
        return None, (
            Diagnostic(
                "DESIGN_REVIEW_INVALID",
                "/design_review_sha256",
                f"design-review verification failed: {exc}",
            ),
        )
    nesting_diagnostic = _nesting_diagnostic(raw_draft)
    if nesting_diagnostic is not None:
        return None, (nesting_diagnostic,)
    diagnostics: list[Diagnostic] = []
    draft = _object_at(raw_draft, pointer="", expected=_DRAFT_FIELDS, diagnostics=diagnostics)
    if draft is None:
        return None, tuple(diagnostics)
    if draft.get("schema_version") != DRAFT_SCHEMA_VERSION:
        diagnostics.append(
            Diagnostic(
                "DRAFT_SCHEMA_UNSUPPORTED",
                "/schema_version",
                "unsupported production-profile draft schema",
                expected=DRAFT_SCHEMA_VERSION,
                actual=draft.get("schema_version"),
            )
        )
    if draft.get("deployable") is not False:
        diagnostics.append(
            Diagnostic(
                "DRAFT_MUST_BE_NON_DEPLOYABLE",
                "/deployable",
                "an authoring draft must remain explicitly non-deployable",
                expected=False,
                actual=draft.get("deployable"),
            )
        )
    design_review_sha256 = sha256_hex(design_review_bundle)
    if draft.get("design_review_sha256") != design_review_sha256:
        diagnostics.append(
            Diagnostic(
                "DESIGN_REVIEW_SHA256_MISMATCH",
                "/design_review_sha256",
                "draft is bound to another design-review ZIP",
                expected=design_review_sha256,
                actual=draft.get("design_review_sha256"),
            )
        )
    source_fingerprint = _source_machine_fingerprint(source)
    expected_requirements = _draft_requirements(
        source,
        source_machine_fingerprint=source_fingerprint,
    )
    if draft.get("requirements") != expected_requirements:
        diagnostics.append(
            Diagnostic(
                "DERIVED_REQUIREMENTS_CHANGED",
                "/requirements",
                "derived source requirements differ from the verified design review; rerun init",
                expected=expected_requirements,
                actual=draft.get("requirements"),
            )
        )
    payload_value = draft.get("payload")
    diagnostics.extend(_find_unresolved(payload_value, pointer="/payload"))
    if diagnostics or not isinstance(payload_value, dict):
        if not isinstance(payload_value, dict):
            diagnostics.append(
                Diagnostic("TYPE_MISMATCH", "/payload", "draft payload must be an object")
            )
        return None, tuple(diagnostics)
    payload = deepcopy(cast(dict[str, object], payload_value))
    machine = payload.get("machine")
    postprocessor = payload.get("postprocessor_profile")
    if not isinstance(machine, dict) or not isinstance(postprocessor, dict):
        return None, (
            Diagnostic("TYPE_MISMATCH", "/payload", "machine and postprocessor must be objects"),
        )
    marker = machine.get("postprocessor_profile_sha256")
    if marker != {COMPUTED_KEY: POSTPROCESSOR_HASH_VALUE}:
        return None, (
            Diagnostic(
                "COMPUTED_FIELD_CHANGED",
                "/payload/machine/postprocessor_profile_sha256",
                "computed postprocessor digest marker must not be edited",
                expected={COMPUTED_KEY: POSTPROCESSOR_HASH_VALUE},
                actual=marker,
            ),
        )
    machine["postprocessor_profile_sha256"] = sha256_hex(canonical_json_bytes(postprocessor))
    production_document: dict[str, object] = {
        "payload": payload,
        "payload_sha256": sha256_hex(canonical_json_bytes(payload)),
        "schema_version": PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
    }
    profile_bytes = canonical_json_bytes(production_document)
    loaded, validation_diagnostics = validate_profile_bytes(
        design_review_bundle,
        profile_bytes,
    )
    if loaded is None:
        return None, validation_diagnostics
    return profile_bytes, ()


def _success_receipt(
    loaded: LoadedProductionMachineProfile,
    *,
    design_review_bundle: bytes,
    output: Path | None = None,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "candidate_generation_ready": True,
        "design_review_sha256": sha256_hex(design_review_bundle),
        "document_sha256": loaded.document_sha256,
        "execution_context_sha256": loaded.execution_context.fingerprint,
        "payload_sha256": loaded.payload_sha256,
        "physical_cutting_authorized": False,
        "postprocessor_profile_sha256": loaded.postprocessor_profile.config_sha256,
        "status": "VALID",
        "workshop_acceptance_required": True,
    }
    if output is not None:
        receipt["output"] = str(output)
    return receipt


def _emit_json(value: object, *, stream: Any) -> None:
    stream.buffer.write(canonical_json_bytes(value) + b"\n")


def _emit_failure(diagnostics: Sequence[Diagnostic]) -> None:
    _emit_json(
        {
            "diagnostics": [diagnostic.as_json() for diagnostic in diagnostics],
            "status": "INVALID",
        },
        stream=sys.stderr,
    )


def _init_command(arguments: argparse.Namespace) -> int:
    bundle = _read_bounded_regular_file(
        arguments.design_review,
        label="design-review bundle",
        limit=MAX_ARTIFACT_BYTES,
    )
    draft, _source = build_profile_draft(bundle)
    pretty = (
        json.dumps(draft, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    _write_new_file(arguments.output, pretty)
    unresolved = _find_unresolved(draft["payload"], pointer="/payload")
    _emit_json(
        {
            "deployable": False,
            "design_review_sha256": sha256_hex(bundle),
            "output": str(arguments.output),
            "status": "DRAFT_CREATED",
            "unresolved_workshop_fact_count": len(unresolved),
        },
        stream=sys.stdout,
    )
    return 0


def _validate_command(arguments: argparse.Namespace) -> int:
    bundle = _read_bounded_regular_file(
        arguments.design_review,
        label="design-review bundle",
        limit=MAX_ARTIFACT_BYTES,
    )
    profile = _read_bounded_regular_file(
        arguments.profile,
        label="production profile",
        limit=MAX_PRODUCTION_MACHINE_PROFILE_BYTES,
    )
    loaded, diagnostics = validate_profile_bytes(bundle, profile)
    if loaded is None:
        _emit_failure(diagnostics)
        return 2
    _emit_json(_success_receipt(loaded, design_review_bundle=bundle), stream=sys.stdout)
    return 0


def _finalize_command(arguments: argparse.Namespace) -> int:
    bundle = _read_bounded_regular_file(
        arguments.design_review,
        label="design-review bundle",
        limit=MAX_ARTIFACT_BYTES,
    )
    draft = _read_bounded_regular_file(
        arguments.draft,
        label="production-profile draft",
        limit=MAX_DRAFT_BYTES,
    )
    profile, diagnostics = finalize_profile_draft(bundle, draft)
    if profile is None:
        _emit_failure(diagnostics)
        return 2
    _write_new_file(arguments.output, profile)
    loaded = load_production_machine_profile(profile)
    _emit_json(
        _success_receipt(
            loaded,
            design_review_bundle=bundle,
            output=arguments.output,
        ),
        stream=sys.stdout,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create, validate and finalize a workshop-owned LinuxCNC production-machine profile."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser(
        "init",
        help="create a non-deployable draft from one strictly verified design-review ZIP",
    )
    init.add_argument("--design-review", required=True, type=Path)
    init.add_argument("--output", required=True, type=Path)
    init.set_defaults(handler=_init_command)

    validate = subparsers.add_parser(
        "validate",
        help="read-only validate an exact finalized profile against a design-review ZIP",
    )
    validate.add_argument("--design-review", required=True, type=Path)
    validate.add_argument("--profile", required=True, type=Path)
    validate.set_defaults(handler=_validate_command)

    finalize = subparsers.add_parser(
        "finalize",
        help="validate a completed draft and exclusively write canonical production bytes",
    )
    finalize.add_argument("--design-review", required=True, type=Path)
    finalize.add_argument("--draft", required=True, type=Path)
    finalize.add_argument("--output", required=True, type=Path)
    finalize.set_defaults(handler=_finalize_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    handler = cast(Any, arguments.handler)
    try:
        return cast(int, handler(arguments))
    except ProfileAuthoringError as exc:
        _emit_failure(exc.diagnostics)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
