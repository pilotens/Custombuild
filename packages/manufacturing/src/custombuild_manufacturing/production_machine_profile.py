"""Strict server-owned configuration boundary for executable CAM candidates.

The module contains no built-in production profile.  A deployment must load an
exact, protected server-side document and pass its bytes through this boundary;
request-body profile data must never be treated as server ownership.

The accepted JSON representation is canonical UTF-8 JSON with three top-level
members: ``schema_version``, ``payload`` and ``payload_sha256``.  The digest is
SHA-256 over the canonical JSON bytes of ``payload`` only.  Every object is
closed to unknown fields and duplicate JSON keys are rejected.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Never, cast

from .model import OperationKind, Point2D, Rect, Side, canonical_json_bytes, sha256_hex

if TYPE_CHECKING:
    from custombuild_cam import ProductionExecutionContext
    from custombuild_postprocessors import LinuxCNCProductionMachineProfile

PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION = "custombuild.production-machine-profile.v1"
SERVER_OWNED_PRODUCTION_PROFILE = "SERVER_OWNED_PRODUCTION"
WORKSHOP_ACCEPTED_STATUS = "WORKSHOP_ACCEPTED"
TEST_ONLY_PROFILE = "TEST_ONLY"
TEST_ONLY_STATUS = "TEST_ONLY"
MAX_PRODUCTION_MACHINE_PROFILE_BYTES = 1_000_000

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_PRODUCTION_PLACEHOLDER_WORDS = frozenset(
    {
        "DEMO",
        "EXAMPLE",
        "EXTERNAL",
        "FAKE",
        "GENERIC",
        "MOCK",
        "PLACEHOLDER",
        "REFERENCE",
        "REQUIRED",
        "SCREENING",
        "TBD",
        "TEST",
        "TODO",
        "UNKNOWN",
        "UNRESOLVED",
        "UNVERIFIED",
        "VALIDATION",
    }
)
_SUPPORTED_CONTROLLER_ID = "linuxcnc"
_SUPPORTED_ENTRY_STRATEGY = "PLUNGE"
_SOURCE_PROVENANCE_TEXT_FIELDS = frozenset(
    {
        "source_machine_profile_id",
        "source_machine_profile_version",
        "source_material_id",
        "source_material_version",
        "source_tool_id",
        "source_tool_version",
    }
)
_SUPPORTED_OPERATION_KINDS = frozenset(
    {
        OperationKind.DRILL,
        OperationKind.COUNTERSINK,
        OperationKind.POCKET,
        OperationKind.GROOVE,
        OperationKind.CONTOUR,
    }
)


class ProductionMachineProfileError(ValueError):
    """A production machine profile is malformed, untrusted or incomplete."""


@dataclass(frozen=True, slots=True)
class WorkshopAcceptanceEvidence:
    """Identity of the externally retained workshop-acceptance evidence."""

    evidence_id: str
    evidence_version: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _require_string(self.evidence_id, "acceptance.evidence_id")
        _require_string(self.evidence_version, "acceptance.evidence_version")
        _require_sha256(self.evidence_sha256, "acceptance.evidence_sha256")


@dataclass(frozen=True, slots=True)
class LoadedProductionMachineProfile:
    """Immutable receipt retaining the verified source-payload identity."""

    schema_version: str
    profile_class: str
    acceptance_status: str
    acceptance_evidence: WorkshopAcceptanceEvidence
    payload_sha256: str
    document_sha256: str
    canonical_payload_json: bytes
    canonical_document_json: bytes
    execution_context: ProductionExecutionContext
    postprocessor_profile: LinuxCNCProductionMachineProfile


def load_production_machine_profile(
    source: bytes | str | Mapping[str, Any],
    *,
    allow_test_only: bool = False,
) -> LoadedProductionMachineProfile:
    """Verify and adapt one closed profile document.

    ``allow_test_only`` exists solely for explicitly labelled test harnesses.
    Production callers must leave it false.  The returned execution context is
    composed exclusively from immutable CAM dataclasses.
    """

    document = _parse_document(source)
    _require_exact_keys(
        document,
        frozenset({"schema_version", "payload", "payload_sha256"}),
        "profile document",
    )
    schema_version = _require_string(document["schema_version"], "schema_version")
    if schema_version != PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION:
        raise ProductionMachineProfileError("unsupported production machine profile schema")

    payload = _require_object(document["payload"], "payload")
    declared_payload_sha256 = _require_sha256(document["payload_sha256"], "payload_sha256")
    actual_payload_sha256 = sha256_hex(canonical_json_bytes(payload))
    if declared_payload_sha256 != actual_payload_sha256:
        raise ProductionMachineProfileError("production profile payload_sha256 mismatch")

    _require_exact_keys(
        payload,
        frozenset(
            {
                "profile_class",
                "acceptance",
                "machine",
                "postprocessor_profile",
                "setups",
                "tools",
                "recipes",
            }
        ),
        "profile payload",
    )
    profile_class = _require_string(payload["profile_class"], "profile_class")
    acceptance_status, acceptance_evidence = _parse_acceptance(payload["acceptance"])
    if profile_class == TEST_ONLY_PROFILE:
        if acceptance_status != TEST_ONLY_STATUS:
            raise ProductionMachineProfileError(
                "TEST_ONLY profile must have TEST_ONLY acceptance status"
            )
        if not allow_test_only:
            raise ProductionMachineProfileError(
                "TEST_ONLY production profile requires explicit test-harness opt-in"
            )
    elif profile_class == SERVER_OWNED_PRODUCTION_PROFILE:
        if acceptance_status != WORKSHOP_ACCEPTED_STATUS:
            raise ProductionMachineProfileError(
                "server-owned production profile is not workshop accepted"
            )
        _reject_production_placeholders(payload)
    else:
        raise ProductionMachineProfileError("unsupported production profile class")

    try:
        context, postprocessor_profile = _build_execution_context(payload)
    except ProductionMachineProfileError:
        raise
    except ValueError as exc:
        raise ProductionMachineProfileError(
            f"production profile execution contract is invalid: {exc}"
        ) from exc
    # Byte/string inputs were proven byte-for-byte canonical by _parse_document,
    # so this is also the exact protected-file digest used by the deployment pin.
    canonical_payload = canonical_json_bytes(payload)
    canonical_document = canonical_json_bytes(document)
    return LoadedProductionMachineProfile(
        schema_version=schema_version,
        profile_class=profile_class,
        acceptance_status=acceptance_status,
        acceptance_evidence=acceptance_evidence,
        payload_sha256=declared_payload_sha256,
        document_sha256=sha256_hex(canonical_document),
        canonical_payload_json=canonical_payload,
        canonical_document_json=canonical_document,
        execution_context=context,
        postprocessor_profile=postprocessor_profile,
    )


def load_production_execution_context(
    source: bytes | str | Mapping[str, Any],
    *,
    allow_test_only: bool = False,
) -> ProductionExecutionContext:
    """Return only the immutable CAM context after full profile verification."""

    return load_production_machine_profile(
        source,
        allow_test_only=allow_test_only,
    ).execution_context


def production_machine_profile_job_binding(
    loaded: LoadedProductionMachineProfile,
) -> dict[str, Any]:
    """Return the small canonical server-owned identity copied onto a CAM job."""

    return {
        "acceptance": {
            "evidence_id": loaded.acceptance_evidence.evidence_id,
            "evidence_sha256": loaded.acceptance_evidence.evidence_sha256,
            "evidence_version": loaded.acceptance_evidence.evidence_version,
            "status": loaded.acceptance_status,
        },
        "execution_context_sha256": loaded.execution_context.fingerprint,
        "document_sha256": loaded.document_sha256,
        "payload_sha256": loaded.payload_sha256,
        "postprocessor_profile": {
            "config_sha256": loaded.postprocessor_profile.config_sha256,
            "profile_id": loaded.postprocessor_profile.profile_id,
            "version": loaded.postprocessor_profile.version,
        },
        "profile_class": loaded.profile_class,
        "schema_version": loaded.schema_version,
    }


def production_machine_profile_job_binding_json(
    loaded: LoadedProductionMachineProfile,
) -> bytes:
    """Serialize the server-owned job identity as canonical JSON bytes."""

    return canonical_json_bytes(production_machine_profile_job_binding(loaded))


def _build_execution_context(
    payload: Mapping[str, Any],
) -> tuple[ProductionExecutionContext, LinuxCNCProductionMachineProfile]:
    # Runtime import prevents the lightweight manufacturing models imported by
    # custombuild_cam from creating a package-initialisation cycle.
    from custombuild_cam import ProductionExecutionContext

    machine = _parse_machine(payload["machine"])
    postprocessor_profile = _parse_postprocessor_profile(payload["postprocessor_profile"])
    setups = tuple(
        _parse_setup(value, index=index)
        for index, value in enumerate(_require_array(payload["setups"], "setups"))
    )
    tools = tuple(
        _parse_tool(value, index=index)
        for index, value in enumerate(_require_array(payload["tools"], "tools"))
    )
    recipes = tuple(
        _parse_recipe(value, index=index)
        for index, value in enumerate(_require_array(payload["recipes"], "recipes"))
    )

    _require_canonical_order(
        "setups",
        tuple(setup.setup_id for setup in setups),
    )
    _require_canonical_order(
        "tools",
        tuple((tool.source_tool_id, tool.tool_id) for tool in tools),
    )
    recipe_order = tuple(
        (
            recipe.material_id,
            recipe.material_version,
            recipe.tool_id,
            recipe.tool_version,
            recipe.operation_kind.value,
        )
        for recipe in recipes
    )
    _require_canonical_order("recipes", recipe_order)

    controller_numbers = tuple(tool.controller_tool_number for tool in tools)
    if len(controller_numbers) != len(set(controller_numbers)):
        raise ProductionMachineProfileError("duplicate controller tool number")
    length_offsets = tuple(tool.length_offset_number for tool in tools)
    if len(length_offsets) != len(set(length_offsets)):
        raise ProductionMachineProfileError("duplicate tool length-offset number")
    recipe_ids = tuple(recipe.recipe_id for recipe in recipes)
    if len(recipe_ids) != len(set(recipe_ids)):
        raise ProductionMachineProfileError("duplicate cutting recipe identity")

    setup_materials = {(setup.material_id, setup.material_version) for setup in setups}
    recipe_materials = {(recipe.material_id, recipe.material_version) for recipe in recipes}
    if setup_materials != recipe_materials:
        raise ProductionMachineProfileError(
            "recipe material bindings must exactly cover setup materials"
        )
    tool_keys = {(tool.tool_id, tool.tool_version) for tool in tools}
    recipe_tool_keys = {(recipe.tool_id, recipe.tool_version) for recipe in recipes}
    if tool_keys != recipe_tool_keys:
        raise ProductionMachineProfileError(
            "recipe tool bindings must exactly cover the tool catalogue"
        )
    tools_by_key = {(tool.tool_id, tool.tool_version): tool for tool in tools}
    for recipe in recipes:
        tool = tools_by_key[(recipe.tool_id, recipe.tool_version)]
        if recipe.stepdown_um > tool.cutting_length_um:
            raise ProductionMachineProfileError(
                f"recipe stepdown exceeds tool cutting length: {recipe.recipe_id}"
            )
        if tool.effective_diameter_um * recipe.stepover_ppm // 1_000_000 <= 0:
            raise ProductionMachineProfileError(
                f"recipe stepover rounds to zero: {recipe.recipe_id}"
            )
        if recipe.operation_kind in {
            OperationKind.POCKET,
            OperationKind.GROOVE,
            OperationKind.CONTOUR,
        }:
            if tool.geometry.value != "FLAT_END_MILL" or not tool.center_cutting:
                raise ProductionMachineProfileError(
                    f"area/contour recipe requires a center-cutting end mill: {recipe.recipe_id}"
                )
        elif recipe.operation_kind == OperationKind.DRILL:
            if tool.geometry.value != "DRILL":
                raise ProductionMachineProfileError(
                    f"drill recipe requires drill geometry: {recipe.recipe_id}"
                )
        elif recipe.operation_kind == OperationKind.COUNTERSINK:
            if tool.geometry.value != "COUNTERSINK":
                raise ProductionMachineProfileError(
                    f"countersink recipe requires countersink geometry: {recipe.recipe_id}"
                )
            if recipe.countersink_top_diameter_um != tool.effective_diameter_um:
                raise ProductionMachineProfileError(
                    f"countersink recipe/tool diameter mismatch: {recipe.recipe_id}"
                )
    for setup in setups:
        through_requirements = tuple(
            recipe.through_overtravel_um + recipe.process_accuracy_um
            for recipe in recipes
            if recipe.operation_kind == OperationKind.CONTOUR
            and recipe.material_id == setup.material_id
            and recipe.material_version == setup.material_version
        )
        required_allowance_um = max(through_requirements, default=0)
        if (
            setup.through_cut_allowance_um
            and required_allowance_um > setup.through_cut_allowance_um
        ):
            raise ProductionMachineProfileError(
                "setup through-cut allowance does not cover contour overtravel and process "
                f"accuracy: {setup.setup_id}"
            )

    context = ProductionExecutionContext(
        source_machine_profile_id=machine["source_machine_profile_id"],
        source_machine_profile_version=machine["source_machine_profile_version"],
        source_machine_profile_fingerprint=machine["source_machine_profile_fingerprint"],
        machine_profile_id=machine["machine_profile_id"],
        machine_profile_version=machine["machine_profile_version"],
        controller_id=machine["controller_id"],
        controller_version=machine["controller_version"],
        work_width_um=machine["work_width_um"],
        work_height_um=machine["work_height_um"],
        work_z_um=machine["work_z_um"],
        machine_x_min_um=machine["machine_x_min_um"],
        machine_x_max_um=machine["machine_x_max_um"],
        machine_y_min_um=machine["machine_y_min_um"],
        machine_y_max_um=machine["machine_y_max_um"],
        machine_z_min_um=machine["machine_z_min_um"],
        machine_z_max_um=machine["machine_z_max_um"],
        min_spindle_rpm=machine["min_spindle_rpm"],
        max_spindle_rpm=machine["max_spindle_rpm"],
        max_feed_um_min=machine["max_feed_um_min"],
        max_plunge_um_min=machine["max_plunge_um_min"],
        tool_catalog_version=machine["tool_catalog_version"],
        recipe_catalog_version=machine["recipe_catalog_version"],
        setups=setups,
        tool_bindings=tools,
        recipes=recipes,
    )
    _cross_check_postprocessor_profile(
        machine=machine,
        context=context,
        profile=postprocessor_profile,
    )
    return context, postprocessor_profile


def _parse_acceptance(value: Any) -> tuple[str, WorkshopAcceptanceEvidence]:
    acceptance = _require_object(value, "acceptance")
    _require_exact_keys(
        acceptance,
        frozenset(
            {
                "status",
                "evidence_id",
                "evidence_version",
                "evidence_sha256",
            }
        ),
        "acceptance",
    )
    status = _require_string(acceptance["status"], "acceptance.status")
    evidence = WorkshopAcceptanceEvidence(
        evidence_id=_require_string(acceptance["evidence_id"], "acceptance.evidence_id"),
        evidence_version=_require_string(
            acceptance["evidence_version"], "acceptance.evidence_version"
        ),
        evidence_sha256=_require_sha256(
            acceptance["evidence_sha256"], "acceptance.evidence_sha256"
        ),
    )
    return status, evidence


def _parse_postprocessor_profile(value: Any) -> LinuxCNCProductionMachineProfile:
    # Runtime import avoids a manufacturing -> postprocessors -> manufacturing
    # cycle while package exports are initialising.
    from custombuild_postprocessors import LinuxCNCProductionMachineProfile

    profile_value = _require_object(value, "postprocessor_profile")
    try:
        return LinuxCNCProductionMachineProfile.from_json(canonical_json_bytes(profile_value))
    except (TypeError, ValueError) as exc:
        raise ProductionMachineProfileError(f"postprocessor_profile is invalid: {exc}") from exc


def _cross_check_postprocessor_profile(
    *,
    machine: Mapping[str, Any],
    context: ProductionExecutionContext,
    profile: LinuxCNCProductionMachineProfile,
) -> None:
    expected_binding = (
        machine["postprocessor_profile_id"],
        machine["postprocessor_profile_version"],
        machine["postprocessor_profile_sha256"],
    )
    actual_binding = (profile.profile_id, profile.version, profile.config_sha256)
    if expected_binding != actual_binding:
        raise ProductionMachineProfileError(
            "postprocessor profile identity or config_sha256 binding mismatch"
        )
    if (
        profile.machine_profile_id != context.machine_profile_id
        or profile.machine_profile_version != context.machine_profile_version
    ):
        raise ProductionMachineProfileError(
            "postprocessor profile is bound to another production machine"
        )
    if (
        profile.controller_id != context.controller_id
        or profile.controller_version != context.controller_version
    ):
        raise ProductionMachineProfileError("postprocessor profile is bound to another controller")
    unsupported_wcs = sorted({setup.wcs for setup in context.setups} - set(profile.supported_wcs))
    if unsupported_wcs:
        raise ProductionMachineProfileError(
            "postprocessor profile does not support every configured setup WCS: "
            + ", ".join(unsupported_wcs)
        )
    exact_bounds = (
        ("X minimum", profile.machine_x_min_um, context.machine_x_min_um),
        ("X maximum", profile.machine_x_max_um, context.machine_x_max_um),
        ("Y minimum", profile.machine_y_min_um, context.machine_y_min_um),
        ("Y maximum", profile.machine_y_max_um, context.machine_y_max_um),
        ("Z minimum", profile.machine_z_min_um, context.machine_z_min_um),
        ("Z maximum", profile.machine_z_max_um, context.machine_z_max_um),
    )
    for bound, postprocessor_value, execution_value in exact_bounds:
        if postprocessor_value != execution_value:
            raise ProductionMachineProfileError(
                f"postprocessor machine {bound} differs from execution context"
            )
    offsets_by_wcs = {offset.wcs: offset for offset in profile.wcs_offsets}
    for setup in context.setups:
        offset = offsets_by_wcs.get(setup.wcs)
        if offset is None:
            raise ProductionMachineProfileError(
                f"postprocessor profile has no attested offset for setup WCS {setup.wcs}"
            )
        expected_origin = (
            setup.machine_wcs_origin.x_um,
            setup.machine_wcs_origin.y_um,
            setup.machine_wcs_z0_um,
            setup.machine_wcs_xy_rotation_mdeg,
        )
        actual_origin = (
            offset.machine_x0_um,
            offset.machine_y0_um,
            offset.machine_z0_um,
            offset.machine_xy_rotation_mdeg,
        )
        if actual_origin != expected_origin:
            raise ProductionMachineProfileError(
                f"postprocessor WCS offset differs from setup origin: {setup.setup_id}"
            )


def _parse_machine(value: Any) -> dict[str, Any]:
    machine = _require_object(value, "machine")
    keys = frozenset(
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
    _require_exact_keys(machine, keys, "machine")
    controller_id = _require_string(machine["controller_id"], "machine.controller_id")
    if controller_id != _SUPPORTED_CONTROLLER_ID:
        raise ProductionMachineProfileError(f"unsupported production controller: {controller_id}")
    return {
        "source_machine_profile_id": _require_string(
            machine["source_machine_profile_id"],
            "machine.source_machine_profile_id",
        ),
        "source_machine_profile_version": _require_string(
            machine["source_machine_profile_version"],
            "machine.source_machine_profile_version",
        ),
        "source_machine_profile_fingerprint": _require_sha256(
            machine["source_machine_profile_fingerprint"],
            "machine.source_machine_profile_fingerprint",
        ),
        "machine_profile_id": _require_string(
            machine["machine_profile_id"], "machine.machine_profile_id"
        ),
        "machine_profile_version": _require_string(
            machine["machine_profile_version"], "machine.machine_profile_version"
        ),
        "controller_id": controller_id,
        "controller_version": _require_string(
            machine["controller_version"], "machine.controller_version"
        ),
        "work_width_um": _require_integer(
            machine["work_width_um"], "machine.work_width_um", minimum=1
        ),
        "work_height_um": _require_integer(
            machine["work_height_um"], "machine.work_height_um", minimum=1
        ),
        "work_z_um": _require_integer(machine["work_z_um"], "machine.work_z_um", minimum=1),
        "machine_x_min_um": _require_integer(
            machine["machine_x_min_um"], "machine.machine_x_min_um"
        ),
        "machine_x_max_um": _require_integer(
            machine["machine_x_max_um"], "machine.machine_x_max_um"
        ),
        "machine_y_min_um": _require_integer(
            machine["machine_y_min_um"], "machine.machine_y_min_um"
        ),
        "machine_y_max_um": _require_integer(
            machine["machine_y_max_um"], "machine.machine_y_max_um"
        ),
        "machine_z_min_um": _require_integer(
            machine["machine_z_min_um"], "machine.machine_z_min_um"
        ),
        "machine_z_max_um": _require_integer(
            machine["machine_z_max_um"], "machine.machine_z_max_um"
        ),
        "min_spindle_rpm": _require_integer(
            machine["min_spindle_rpm"], "machine.min_spindle_rpm", minimum=1
        ),
        "max_spindle_rpm": _require_integer(
            machine["max_spindle_rpm"], "machine.max_spindle_rpm", minimum=1
        ),
        "max_feed_um_min": _require_integer(
            machine["max_feed_um_min"], "machine.max_feed_um_min", minimum=1
        ),
        "max_plunge_um_min": _require_integer(
            machine["max_plunge_um_min"],
            "machine.max_plunge_um_min",
            minimum=1,
        ),
        "tool_catalog_version": _require_string(
            machine["tool_catalog_version"], "machine.tool_catalog_version"
        ),
        "recipe_catalog_version": _require_string(
            machine["recipe_catalog_version"], "machine.recipe_catalog_version"
        ),
        "postprocessor_profile_id": _require_string(
            machine["postprocessor_profile_id"],
            "machine.postprocessor_profile_id",
        ),
        "postprocessor_profile_version": _require_string(
            machine["postprocessor_profile_version"],
            "machine.postprocessor_profile_version",
        ),
        "postprocessor_profile_sha256": _require_sha256(
            machine["postprocessor_profile_sha256"],
            "machine.postprocessor_profile_sha256",
        ),
    }


def _parse_setup(value: Any, *, index: int) -> Any:
    from custombuild_cam import BoundSetup

    label = f"setups[{index}]"
    setup = _require_object(value, label)
    _require_exact_keys(
        setup,
        frozenset(
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
        ),
        label,
    )
    origin_value = _require_object(setup["machine_wcs_origin"], f"{label}.machine_wcs_origin")
    _require_exact_keys(
        origin_value,
        frozenset({"x_um", "y_um"}),
        f"{label}.machine_wcs_origin",
    )
    machine_wcs_origin = Point2D(
        _require_integer(
            origin_value["x_um"],
            f"{label}.machine_wcs_origin.x_um",
        ),
        _require_integer(
            origin_value["y_um"],
            f"{label}.machine_wcs_origin.y_um",
        ),
    )

    fixture = _require_object(setup["fixture"], f"{label}.fixture")
    _require_exact_keys(
        fixture,
        frozenset(
            {
                "fixture_id",
                "fixture_version",
                "fixture_sha256",
                "clearance_z_um",
                "keep_out_policy",
            }
        ),
        f"{label}.fixture",
    )
    keep_out_zones = tuple(
        _parse_rect(item, label=f"{label}.keep_out_zones[{zone_index}]")
        for zone_index, item in enumerate(
            _require_array(setup["keep_out_zones"], f"{label}.keep_out_zones")
        )
    )
    zone_keys = tuple(
        (zone.y_um, zone.x_um, zone.height_um, zone.width_um) for zone in keep_out_zones
    )
    _require_canonical_order(f"{label}.keep_out_zones", zone_keys)
    if len(keep_out_zones) != len(set(keep_out_zones)):
        raise ProductionMachineProfileError(f"{label}.keep_out_zones contains duplicates")

    setup_id = _require_string(setup["setup_id"], f"{label}.setup_id")
    stock_id = _require_string(setup["stock_id"], f"{label}.stock_id")
    sheet_index = _require_integer(setup["sheet_index"], f"{label}.sheet_index", minimum=0)
    side = _parse_side(setup["side"], f"{label}.side")
    expected_setup_id = f"setup:{stock_id}:{sheet_index + 1:03d}:{side.value}"
    if setup_id != expected_setup_id:
        raise ProductionMachineProfileError(
            f"{label}.setup_id is not bound to stock, sheet index and side"
        )

    return BoundSetup(
        setup_id=setup_id,
        stock_id=stock_id,
        source_material_id=_require_string(
            setup["source_material_id"], f"{label}.source_material_id"
        ),
        source_material_version=_require_string(
            setup["source_material_version"], f"{label}.source_material_version"
        ),
        material_id=_require_string(setup["material_id"], f"{label}.material_id"),
        material_version=_require_string(setup["material_version"], f"{label}.material_version"),
        material_evidence_id=_require_string(
            setup["material_evidence_id"], f"{label}.material_evidence_id"
        ),
        material_evidence_version=_require_string(
            setup["material_evidence_version"], f"{label}.material_evidence_version"
        ),
        material_evidence_sha256=_require_sha256(
            setup["material_evidence_sha256"], f"{label}.material_evidence_sha256"
        ),
        sheet_index=sheet_index,
        side=side,
        source_setup_sha256=_require_sha256(
            setup["source_setup_sha256"], f"{label}.source_setup_sha256"
        ),
        source_to_wcs_xy_transform=_require_string(
            setup["source_to_wcs_xy_transform"],
            f"{label}.source_to_wcs_xy_transform",
        ),
        wcs=_require_string(setup["wcs"], f"{label}.wcs"),
        machine_wcs_origin=machine_wcs_origin,
        machine_wcs_z0_um=_require_integer(
            setup["machine_wcs_z0_um"], f"{label}.machine_wcs_z0_um"
        ),
        machine_wcs_xy_rotation_mdeg=_require_integer(
            setup["machine_wcs_xy_rotation_mdeg"],
            f"{label}.machine_wcs_xy_rotation_mdeg",
        ),
        stock_width_um=_require_integer(
            setup["stock_width_um"], f"{label}.stock_width_um", minimum=1
        ),
        stock_height_um=_require_integer(
            setup["stock_height_um"], f"{label}.stock_height_um", minimum=1
        ),
        stock_thickness_um=_require_integer(
            setup["stock_thickness_um"], f"{label}.stock_thickness_um", minimum=1
        ),
        safe_z_um=_require_integer(setup["safe_z_um"], f"{label}.safe_z_um", minimum=1),
        minimum_rapid_clearance_um=_require_integer(
            setup["minimum_rapid_clearance_um"],
            f"{label}.minimum_rapid_clearance_um",
            minimum=1,
        ),
        reference_surface=_require_string(setup["reference_surface"], f"{label}.reference_surface"),
        orientation=_require_string(setup["orientation"], f"{label}.orientation"),
        fixture_id=_require_string(fixture["fixture_id"], f"{label}.fixture.fixture_id"),
        fixture_version=_require_string(
            fixture["fixture_version"], f"{label}.fixture.fixture_version"
        ),
        fixture_sha256=_require_sha256(
            fixture["fixture_sha256"], f"{label}.fixture.fixture_sha256"
        ),
        fixture_clearance_z_um=_require_integer(
            fixture["clearance_z_um"],
            f"{label}.fixture.clearance_z_um",
            minimum=0,
        ),
        keep_out_policy=_require_string(
            fixture["keep_out_policy"],
            f"{label}.fixture.keep_out_policy",
        ),
        probe_method=_require_string(setup["probe_method"], f"{label}.probe_method"),
        keep_out_zones=keep_out_zones,
        raw_allowance_um=_require_integer(
            setup["raw_allowance_um"], f"{label}.raw_allowance_um", minimum=0
        ),
        spoilboard_id=_require_optional_string(setup["spoilboard_id"], f"{label}.spoilboard_id"),
        spoilboard_version=_require_optional_string(
            setup["spoilboard_version"], f"{label}.spoilboard_version"
        ),
        spoilboard_sha256=_require_optional_sha256(
            setup["spoilboard_sha256"], f"{label}.spoilboard_sha256"
        ),
        through_cut_allowance_um=_require_integer(
            setup["through_cut_allowance_um"],
            f"{label}.through_cut_allowance_um",
            minimum=0,
        ),
    )


def _parse_tool(value: Any, *, index: int) -> Any:
    from custombuild_cam import (
        MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
        ProductionToolBinding,
        ProductionToolGeometry,
    )

    label = f"tools[{index}]"
    tool = _require_object(value, label)
    _require_exact_keys(
        tool,
        frozenset(
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
        ),
        label,
    )
    geometry_value = _require_string(tool["geometry"], f"{label}.geometry")
    try:
        geometry = ProductionToolGeometry(geometry_value)
    except ValueError as exc:
        raise ProductionMachineProfileError(
            f"{label}.geometry is unsupported: {geometry_value}"
        ) from exc
    return ProductionToolBinding(
        tool_id=_require_string(tool["tool_id"], f"{label}.tool_id"),
        tool_version=_require_string(tool["tool_version"], f"{label}.tool_version"),
        source_tool_id=_require_string(tool["source_tool_id"], f"{label}.source_tool_id"),
        source_tool_version=_require_string(
            tool["source_tool_version"], f"{label}.source_tool_version"
        ),
        source_tool_sha256=_require_sha256(
            tool["source_tool_sha256"], f"{label}.source_tool_sha256"
        ),
        controller_tool_number=_require_integer(
            tool["controller_tool_number"],
            f"{label}.controller_tool_number",
            minimum=1,
            maximum=MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
        ),
        length_offset_number=_require_integer(
            tool["length_offset_number"],
            f"{label}.length_offset_number",
            minimum=1,
            maximum=MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
        ),
        expected_length_offset_x_um=_require_integer(
            tool["expected_length_offset_x_um"],
            f"{label}.expected_length_offset_x_um",
        ),
        expected_length_offset_y_um=_require_integer(
            tool["expected_length_offset_y_um"],
            f"{label}.expected_length_offset_y_um",
        ),
        expected_length_offset_z_um=_require_integer(
            tool["expected_length_offset_z_um"],
            f"{label}.expected_length_offset_z_um",
        ),
        tool_table_evidence_id=_require_string(
            tool["tool_table_evidence_id"], f"{label}.tool_table_evidence_id"
        ),
        tool_table_evidence_version=_require_string(
            tool["tool_table_evidence_version"],
            f"{label}.tool_table_evidence_version",
        ),
        tool_table_evidence_sha256=_require_sha256(
            tool["tool_table_evidence_sha256"],
            f"{label}.tool_table_evidence_sha256",
        ),
        effective_diameter_um=_require_integer(
            tool["effective_diameter_um"], f"{label}.effective_diameter_um", minimum=1
        ),
        cutting_length_um=_require_integer(
            tool["cutting_length_um"], f"{label}.cutting_length_um", minimum=1
        ),
        measured_stickout_um=_require_integer(
            tool["measured_stickout_um"],
            f"{label}.measured_stickout_um",
            minimum=1,
        ),
        assembly_collision_radius_um=_require_integer(
            tool["assembly_collision_radius_um"],
            f"{label}.assembly_collision_radius_um",
            minimum=1,
        ),
        minimum_holder_clearance_um=_require_integer(
            tool["minimum_holder_clearance_um"],
            f"{label}.minimum_holder_clearance_um",
            minimum=1,
        ),
        geometry=geometry,
        center_cutting=_require_boolean(tool["center_cutting"], f"{label}.center_cutting"),
        drill_point_length_um=_require_integer(
            tool["drill_point_length_um"],
            f"{label}.drill_point_length_um",
            minimum=0,
        ),
        spindle_direction=_require_string(tool["spindle_direction"], f"{label}.spindle_direction"),
    )


def _parse_recipe(value: Any, *, index: int) -> Any:
    from custombuild_cam import CuttingRecipe

    label = f"recipes[{index}]"
    recipe = _require_object(value, label)
    _require_exact_keys(
        recipe,
        frozenset(
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
        ),
        label,
    )
    entry_strategy = _require_string(recipe["entry_strategy"], f"{label}.entry_strategy")
    if entry_strategy != _SUPPORTED_ENTRY_STRATEGY:
        raise ProductionMachineProfileError(
            f"{label}.entry_strategy must be {_SUPPORTED_ENTRY_STRATEGY} for CAM v1"
        )
    operation_kind_value = _require_string(recipe["operation_kind"], f"{label}.operation_kind")
    try:
        operation_kind = OperationKind(operation_kind_value)
    except ValueError as exc:
        raise ProductionMachineProfileError(
            f"{label}.operation_kind is unsupported: {operation_kind_value}"
        ) from exc
    if operation_kind not in _SUPPORTED_OPERATION_KINDS:
        raise ProductionMachineProfileError(f"{label}.operation_kind is outside production CAM v1")

    return CuttingRecipe(
        recipe_id=_require_string(recipe["recipe_id"], f"{label}.recipe_id"),
        version=_require_string(recipe["version"], f"{label}.version"),
        machine_profile_id=_require_string(
            recipe["machine_profile_id"], f"{label}.machine_profile_id"
        ),
        machine_profile_version=_require_string(
            recipe["machine_profile_version"], f"{label}.machine_profile_version"
        ),
        material_id=_require_string(recipe["material_id"], f"{label}.material_id"),
        material_version=_require_string(recipe["material_version"], f"{label}.material_version"),
        tool_id=_require_string(recipe["tool_id"], f"{label}.tool_id"),
        tool_version=_require_string(recipe["tool_version"], f"{label}.tool_version"),
        operation_kind=operation_kind,
        spindle_rpm=_require_integer(recipe["spindle_rpm"], f"{label}.spindle_rpm", minimum=1),
        feed_um_min=_require_integer(recipe["feed_um_min"], f"{label}.feed_um_min", minimum=1),
        plunge_um_min=_require_integer(
            recipe["plunge_um_min"], f"{label}.plunge_um_min", minimum=1
        ),
        stepdown_um=_require_integer(recipe["stepdown_um"], f"{label}.stepdown_um", minimum=1),
        stepover_ppm=_require_integer(recipe["stepover_ppm"], f"{label}.stepover_ppm", minimum=1),
        peck_depth_um=_require_integer(
            recipe["peck_depth_um"], f"{label}.peck_depth_um", minimum=1
        ),
        approach_clearance_um=_require_integer(
            recipe["approach_clearance_um"],
            f"{label}.approach_clearance_um",
            minimum=1,
        ),
        entry_strategy=entry_strategy,
        diameter_tolerance_um=_require_integer(
            recipe["diameter_tolerance_um"],
            f"{label}.diameter_tolerance_um",
            minimum=0,
        ),
        through_overtravel_um=_require_integer(
            recipe["through_overtravel_um"],
            f"{label}.through_overtravel_um",
            minimum=0,
        ),
        tab_width_um=_require_integer(recipe["tab_width_um"], f"{label}.tab_width_um", minimum=0),
        tab_height_um=_require_integer(
            recipe["tab_height_um"], f"{label}.tab_height_um", minimum=0
        ),
        process_accuracy_um=_require_integer(
            recipe["process_accuracy_um"],
            f"{label}.process_accuracy_um",
            minimum=1,
        ),
        accepted_tolerance_um=_require_integer(
            recipe["accepted_tolerance_um"],
            f"{label}.accepted_tolerance_um",
            minimum=1,
        ),
        countersink_top_diameter_um=_require_optional_integer(
            recipe["countersink_top_diameter_um"],
            f"{label}.countersink_top_diameter_um",
            minimum=1,
        ),
        countersink_included_angle_mdeg=_require_optional_integer(
            recipe["countersink_included_angle_mdeg"],
            f"{label}.countersink_included_angle_mdeg",
            minimum=1,
        ),
    )


def _parse_rect(value: Any, *, label: str) -> Rect:
    rectangle = _require_object(value, label)
    _require_exact_keys(
        rectangle,
        frozenset({"x_um", "y_um", "width_um", "height_um"}),
        label,
    )
    return Rect(
        _require_integer(rectangle["x_um"], f"{label}.x_um", minimum=0),
        _require_integer(rectangle["y_um"], f"{label}.y_um", minimum=0),
        _require_integer(rectangle["width_um"], f"{label}.width_um", minimum=1),
        _require_integer(rectangle["height_um"], f"{label}.height_um", minimum=1),
    )


def _parse_side(value: Any, label: str) -> Side:
    side_value = _require_string(value, label)
    try:
        side = Side(side_value)
    except ValueError as exc:
        raise ProductionMachineProfileError(f"{label} is unsupported: {side_value}") from exc
    if side == Side.EDGE:
        raise ProductionMachineProfileError(f"{label} cannot be EDGE for production CAM v1")
    return side


def _parse_document(source: bytes | str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, Mapping):
        document = _require_object(source, "profile document")
        # Ensure the supplied mapping is JSON-canonicalizable before any trust
        # decision or digest comparison.
        try:
            canonical = canonical_json_bytes(document)
        except (TypeError, ValueError) as exc:
            raise ProductionMachineProfileError(
                "production profile mapping is not canonical JSON data"
            ) from exc
        if len(canonical) > MAX_PRODUCTION_MACHINE_PROFILE_BYTES:
            raise ProductionMachineProfileError(
                "production profile size is outside the accepted range"
            )
        return document

    raw = source.encode("utf-8") if isinstance(source, str) else source
    if not isinstance(raw, bytes):
        raise ProductionMachineProfileError("production profile must be bytes, text or mapping")
    if not raw or len(raw) > MAX_PRODUCTION_MACHINE_PROFILE_BYTES:
        raise ProductionMachineProfileError("production profile size is outside the accepted range")
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionMachineProfileError("production profile is not valid UTF-8 JSON") from exc
    document = _require_object(parsed, "profile document")
    try:
        canonical = canonical_json_bytes(document)
    except (TypeError, ValueError) as exc:
        raise ProductionMachineProfileError(
            "production profile is not canonical JSON data"
        ) from exc
    if raw != canonical:
        raise ProductionMachineProfileError(
            "production profile bytes must use canonical JSON encoding"
        )
    return document


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProductionMachineProfileError(
                f"production profile contains duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _reject_json_number(value: str) -> Never:
    raise ProductionMachineProfileError(
        f"production profile numbers must be exact JSON integers: {value}"
    )


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProductionMachineProfileError(f"{label} must be an object")
    if any(type(key) is not str for key in value):
        raise ProductionMachineProfileError(f"{label} keys must be strings")
    return {cast(str, key): item for key, item in value.items()}


def _require_array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProductionMachineProfileError(f"{label} must be an array")
    return value


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        raise ProductionMachineProfileError(f"{label} has {'; '.join(details)}")


def _require_string(value: Any, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductionMachineProfileError(f"{label} must be a canonical non-blank string")
    return value


def _require_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise ProductionMachineProfileError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _require_optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, label)


def _require_optional_sha256(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _require_sha256(value, label)


def _require_integer(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise ProductionMachineProfileError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ProductionMachineProfileError(
            f"{label} must be an integer greater than or equal to {minimum}"
        )
    if maximum is not None and value > maximum:
        raise ProductionMachineProfileError(
            f"{label} must be an integer less than or equal to {maximum}"
        )
    return value


def _require_optional_integer(
    value: Any,
    label: str,
    *,
    minimum: int,
) -> int | None:
    if value is None:
        return None
    return _require_integer(value, label, minimum=minimum)


def _require_boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ProductionMachineProfileError(f"{label} must be a boolean")
    return value


def _require_canonical_order(label: str, values: tuple[Any, ...]) -> None:
    if tuple(sorted(values)) != values:
        raise ProductionMachineProfileError(f"{label} must use canonical order")


def _reject_production_placeholders(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _SOURCE_PROVENANCE_TEXT_FIELDS:
                # Validation-catalog provenance may truthfully carry names such
                # as "reference".  Its required digest, rather than its label,
                # is the trust binding.  Actual production facts remain scanned.
                continue
            _reject_production_placeholders(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_production_placeholders(item, path=f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    words = frozenset(filter(None, re.split(r"[^A-Z0-9]+", value.upper())))
    placeholders = sorted(words & _PRODUCTION_PLACEHOLDER_WORDS)
    if placeholders:
        raise ProductionMachineProfileError(
            f"{path} contains forbidden production placeholder token: {placeholders[0]}"
        )
