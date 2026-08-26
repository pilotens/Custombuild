"""Run every screened template through the complete design-review pipeline.

This gate is deliberately not a physical-production release.  The caller must
provide a validation-only fixture containing every stock-frame registration
coordinate used by the automated run.  The gate never infers a WCS, pin,
fixture, machine calibration or workshop approval.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.design_service import canonical_preview
from app.schemas import BookcasePreviewInput
from custombuild_cad import CadQueryAdapter
from custombuild_domain import (
    BOOKCASE_JOINT_SUPPORT_VERSION,
    BookcaseDesignSpec,
    BookcaseParameters,
    TemplateCapability,
    TemplateProductionLevel,
    build_bookcase,
    mm,
    require_template_for_revision,
    screening_mdf_6,
    screening_mdf_18,
    template_capability_registry_payload,
)
from custombuild_manufacturing import (
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    ManifestContext,
    Point2D,
    ProductionBlockedError,
    StockSheet,
    TwoSidedRegistration,
    blocked_cam_artifact_violation,
    build_production_bundle,
    canonical_json_bytes,
    read_and_verify_package,
    sha256_hex,
)
from custombuild_manufacturing.pipeline import ProductionBundle
from custombuild_manufacturing.production_context import (
    generation_context_hash,
    resolve_production_components,
)
from custombuild_postprocessors import LinuxCNCValidationPostprocessor
from custombuild_rules import RULES_VERSION

try:
    from scripts.source_manifest import SourceManifestError, build_source_manifest
except ModuleNotFoundError:  # Direct ``python scripts/design_review_gate.py`` execution.
    from source_manifest import (  # type: ignore[import-not-found,no-redef]
        SourceManifestError,
        build_source_manifest,
    )

GATE_REPORT_SCHEMA_VERSION = "custombuild.design-review-gate.v2"
GATE_FIXTURE_SCHEMA_VERSION = "custombuild.design-review-gate-fixture.v1"
GATE_FIXTURE_SCOPE = "AUTOMATED_DESIGN_REVIEW_ONLY"
GATE_FIXTURE_WARNING = (
    "These stock-frame coordinates are deterministic automated test data, not a WCS, "
    "fixture, machine setup, calibration record, or workshop approval."
)
SCREENED_TEMPLATE_IDS = ("shelving",)
DEFAULT_REPOSITORY = Path(".")
SCREENED_DEFAULTS_CONTRACT_PATH = Path("packages/contracts/screened-template-defaults.v1.json")
SCREENED_DEFAULTS_SCHEMA_VERSION = "custombuild.screened-template-defaults.v1"
SCREENED_DEFAULTS_CONTRACT_VERSION = "1.1.0"
SCREENED_DEFAULTS_CONTRACT_FINGERPRINT = (
    "88eca4417ba84e500a21658f5d7ce2b2277d5feca0bccd8cb36aae4419a821b0"
)
SCREENED_DEFAULTS_CANONICALIZATION = (
    "UTF-8 JSON with recursively sorted object keys, compact separators, "
    "ensure_ascii=false, and the top-level fingerprint member omitted"
)
CANONICAL_CONSTRUCTION_STATUSES = frozenset({"PASS", "WARNING", "BLOCK"})
EFFECTIVE_DESIGN_SPEC_KEYS = frozenset(
    {
        "schema_version",
        "furniture_type",
        "width_mm",
        "height_mm",
        "depth_mm",
        "material_id",
        "material_name",
        "nominal_thickness_mm",
        "measured_thickness_mm",
        "shelf_count",
        "fixed_shelves",
        "load_per_shelf_kg",
        "back_panel",
        "plinth",
        "divider_count",
        "bay_sizing_mode",
        "target_bay_width_mm",
        "bay_width_ratios",
        "shelf_height_ratios",
        "symmetry_locked",
        "part_overrides",
        "removed_part_ids",
        "base_cabinet_height_mm",
        "base_cabinet_depth_mm",
        "base_cabinet_count",
        "reinforcement_mode",
        "joint_system",
        "edge_band_mm",
        "wall_anchor_verified",
        "stock_width_mm",
        "stock_height_mm",
        "stock_count",
        "back_stock_width_mm",
        "back_stock_height_mm",
        "back_stock_count",
        "machine_profile_id",
    }
)
REQUIRED_CORE_ARTIFACTS = frozenset(
    {
        "bom/bom.csv",
        "bom/grouped-bom.json",
        "cam/operations.json",
        "cam/validation-backplot.svg",
        "cut-list/cut-list.csv",
        "design/design-spec.json",
        "design/result-summary.json",
        "labels/label-index.csv",
        "materials/material-list.csv",
        "materials/stock-purchase.csv",
        "model/design.glb",
        "model/design.step",
        "quality/measurement-plan.json",
        "validation/cad-interchange-status.json",
        "validation/design-review-package-status.json",
        "validation/dfm-report.json",
        "validation/stock-selection.json",
        "validation/generation-plan.json",
        "validation/workshop-readiness.json",
    }
)
REQUIRED_BLOCKED_REVIEW_ARTIFACTS = frozenset(
    {
        "bom/bom.csv",
        "bom/grouped-bom.json",
        "cut-list/cut-list.csv",
        "design/design-spec.json",
        "design/result-summary.json",
        "materials/material-list.csv",
        "model/design.glb",
        "model/design.step",
        "validation/cad-interchange-status.json",
        "validation/design-review-package-status.json",
        "validation/dfm-report.json",
        "validation/stock-selection.json",
        "validation/generation-plan.json",
        "validation/workshop-readiness.json",
    }
)


class GateStatus(StrEnum):
    BLOCK = "BLOCK"
    EXTERNAL_EVIDENCE_REQUIRED = "EXTERNAL_EVIDENCE_REQUIRED"


class DesignReviewGateError(RuntimeError):
    """The gate input or a generated bundle violates the review contract."""


@dataclass(frozen=True, slots=True)
class GateFixture:
    raw: Mapping[str, Any]
    registrations: Mapping[str, Mapping[str, Mapping[int, TwoSidedRegistration]]]

    @property
    def fingerprint(self) -> str:
        return sha256_hex(canonical_json_bytes(self.raw))


@dataclass(frozen=True, slots=True)
class ScreenedTemplateDefault:
    template_id: str
    raw: Mapping[str, Any]
    effective_design_spec: Mapping[str, Any]

    @property
    def input_fingerprint(self) -> str:
        return sha256_hex(canonical_json_bytes(self.raw))


@dataclass(frozen=True, slots=True)
class ScreenedDefaultsContract:
    raw: Mapping[str, Any]
    contract_version: str
    fingerprint: str
    templates: tuple[ScreenedTemplateDefault, ...]


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DesignReviewGateError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise DesignReviewGateError(
            f"{label} keys must be exactly {sorted(expected)}; got {sorted(actual)}"
        )


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is forbidden")


def load_screened_defaults_contract(path: Path) -> ScreenedDefaultsContract:
    """Load the pinned effective-UI-default contract without accepting drift."""

    try:
        raw_value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_non_finite_json,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise DesignReviewGateError(
            f"cannot read screened-template defaults contract: {exc}"
        ) from exc
    raw = _object(raw_value, "screened defaults contract")
    _exact_keys(
        raw,
        {
            "schema_version",
            "contract_version",
            "status",
            "purpose",
            "units",
            "identity_policy",
            "selection_pipeline",
            "physical_cutting_authorized",
            "default_planning_brief",
            "templates",
            "fingerprint",
        },
        "screened defaults contract",
    )
    if raw["schema_version"] != SCREENED_DEFAULTS_SCHEMA_VERSION:
        raise DesignReviewGateError("unsupported screened-template defaults schema")
    if raw["contract_version"] != SCREENED_DEFAULTS_CONTRACT_VERSION:
        raise DesignReviewGateError("unsupported screened-template defaults contract version")
    if raw["status"] != "published":
        raise DesignReviewGateError("screened-template defaults contract is not published")
    if raw["physical_cutting_authorized"] is not False:
        raise DesignReviewGateError(
            "screened-template defaults contract must never authorize physical cutting"
        )
    identity_policy = _object(raw["identity_policy"], "screened defaults identity_policy")
    if identity_policy != {
        "design_id": "preserve_current_project",
        "revision": "preserve_current_project",
    }:
        raise DesignReviewGateError("screened-template defaults identity policy has drifted")

    raw_templates = raw["templates"]
    if not isinstance(raw_templates, list):
        raise DesignReviewGateError("screened-template defaults templates must be an array")
    parsed_templates: list[ScreenedTemplateDefault] = []
    seen_template_ids: set[str] = set()
    for index, raw_template in enumerate(raw_templates):
        template = _object(raw_template, f"screened defaults template {index}")
        _exact_keys(
            template,
            {
                "template_id",
                "production_level",
                "planning_selection",
                "effective_design_spec",
            },
            f"screened defaults template {index}",
        )
        template_id = template["template_id"]
        if not isinstance(template_id, str) or template_id not in SCREENED_TEMPLATE_IDS:
            raise DesignReviewGateError(
                f"screened defaults template {index} has an unsupported template_id"
            )
        if template_id in seen_template_ids:
            raise DesignReviewGateError(f"duplicate screened default template {template_id}")
        seen_template_ids.add(template_id)
        if template["production_level"] != TemplateProductionLevel.SCREENED.value:
            raise DesignReviewGateError(
                f"screened default template {template_id} is not marked screened"
            )

        spec = _object(
            template["effective_design_spec"],
            f"screened defaults template {template_id} effective_design_spec",
        )
        _exact_keys(
            spec,
            set(EFFECTIVE_DESIGN_SPEC_KEYS),
            f"screened defaults template {template_id} effective_design_spec",
        )
        default = ScreenedTemplateDefault(
            template_id=template_id,
            raw=template,
            effective_design_spec=spec,
        )
        try:
            BookcasePreviewInput.model_validate(_actual_default_preview_payload(default))
        except Exception as exc:
            raise DesignReviewGateError(
                f"screened default template {template_id} is not server-valid: {exc}"
            ) from exc
        parsed_templates.append(default)
    if seen_template_ids != set(SCREENED_TEMPLATE_IDS):
        raise DesignReviewGateError(
            f"screened defaults templates must be exactly {sorted(SCREENED_TEMPLATE_IDS)}"
        )

    fingerprint = _object(raw["fingerprint"], "screened defaults fingerprint")
    _exact_keys(
        fingerprint,
        {"algorithm", "canonicalization", "value"},
        "screened defaults fingerprint",
    )
    if fingerprint["algorithm"] != "sha256":
        raise DesignReviewGateError("screened defaults fingerprint algorithm must be sha256")
    if fingerprint["canonicalization"] != SCREENED_DEFAULTS_CANONICALIZATION:
        raise DesignReviewGateError("screened defaults canonicalization has drifted")
    fingerprint_value = fingerprint["value"]
    if (
        not isinstance(fingerprint_value, str)
        or len(fingerprint_value) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint_value)
    ):
        raise DesignReviewGateError("screened defaults fingerprint is not lowercase SHA-256")
    unsigned = {key: value for key, value in raw.items() if key != "fingerprint"}
    computed = sha256_hex(canonical_json_bytes(unsigned))
    if computed != fingerprint_value:
        raise DesignReviewGateError("screened defaults fingerprint does not match its payload")
    if fingerprint_value != SCREENED_DEFAULTS_CONTRACT_FINGERPRINT:
        raise DesignReviewGateError(
            "screened defaults fingerprint is not the version pinned by this gate"
        )

    return ScreenedDefaultsContract(
        raw=raw,
        contract_version=SCREENED_DEFAULTS_CONTRACT_VERSION,
        fingerprint=fingerprint_value,
        templates=tuple(sorted(parsed_templates, key=lambda item: item.template_id)),
    )


def load_gate_fixture(path: Path) -> GateFixture:
    """Load strict, caller-owned registration data for automated review only."""

    try:
        raw_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DesignReviewGateError(f"cannot read design-review fixture: {exc}") from exc
    raw = _object(raw_value, "fixture")
    _exact_keys(raw, {"schema_version", "fixture_scope", "warning", "templates"}, "fixture")
    if raw["schema_version"] != GATE_FIXTURE_SCHEMA_VERSION:
        raise DesignReviewGateError("unsupported design-review fixture schema")
    if raw["fixture_scope"] != GATE_FIXTURE_SCOPE:
        raise DesignReviewGateError("fixture_scope must explicitly be AUTOMATED_DESIGN_REVIEW_ONLY")
    if not isinstance(raw["warning"], str) or "not" not in raw["warning"].lower():
        raise DesignReviewGateError("fixture warning must state that it is not workshop evidence")

    template_values = _object(raw["templates"], "fixture.templates")
    _exact_keys(template_values, set(SCREENED_TEMPLATE_IDS), "fixture.templates")
    parsed_templates: dict[str, dict[str, dict[int, TwoSidedRegistration]]] = {}
    for template_id in SCREENED_TEMPLATE_IDS:
        template = _object(template_values[template_id], f"fixture.templates.{template_id}")
        _exact_keys(template, {"registrations_by_stock"}, f"fixture.templates.{template_id}")
        stocks = _object(
            template["registrations_by_stock"],
            f"fixture.templates.{template_id}.registrations_by_stock",
        )
        parsed_stocks: dict[str, dict[int, TwoSidedRegistration]] = {}
        for stock_id, raw_sheets in sorted(stocks.items()):
            sheets = _object(raw_sheets, f"fixture stock {stock_id}")
            parsed_sheets: dict[int, TwoSidedRegistration] = {}
            for raw_sheet_index, raw_plan in sorted(sheets.items()):
                if not raw_sheet_index.isdecimal():
                    raise DesignReviewGateError(
                        f"fixture stock {stock_id} sheet indexes must be decimal strings"
                    )
                sheet_index = int(raw_sheet_index)
                plan = _object(raw_plan, f"fixture stock {stock_id} sheet {sheet_index}")
                _exact_keys(
                    plan,
                    {"method_id", "points_um"},
                    f"fixture stock {stock_id} sheet {sheet_index}",
                )
                method_id = plan["method_id"]
                if not isinstance(method_id, str) or not method_id.startswith(
                    "automated-design-review:"
                ):
                    raise DesignReviewGateError(
                        "registration method_id must use the automated-design-review namespace"
                    )
                raw_points = plan["points_um"]
                if not isinstance(raw_points, list) or len(raw_points) < 2:
                    raise DesignReviewGateError(
                        f"fixture stock {stock_id} requires at least two stock-frame points"
                    )
                points: list[Point2D] = []
                for point_index, raw_point in enumerate(raw_points):
                    if (
                        not isinstance(raw_point, list)
                        or len(raw_point) != 2
                        or any(type(coordinate) is not int for coordinate in raw_point)
                    ):
                        raise DesignReviewGateError(
                            f"fixture stock {stock_id} point {point_index} must be two integers"
                        )
                    points.append(Point2D(raw_point[0], raw_point[1]))
                if len({(point.x_um, point.y_um) for point in points}) != len(points):
                    raise DesignReviewGateError(
                        f"fixture stock {stock_id} registration points must be unique"
                    )
                parsed_sheets[sheet_index] = TwoSidedRegistration(
                    method_id=method_id,
                    points=tuple(points),
                )
            parsed_stocks[stock_id] = parsed_sheets
        parsed_templates[template_id] = parsed_stocks
    return GateFixture(raw=raw, registrations=parsed_templates)


def _template_parameters(template_id: str) -> BookcaseParameters:
    if template_id == "shelving":
        return BookcaseParameters(
            width_um=mm(700),
            height_um=mm(1_000),
            depth_um=mm(300),
            shelf_count=2,
        )
    if template_id == "wall-library":
        return BookcaseParameters(
            width_um=mm(1_200),
            height_um=mm(1_600),
            depth_um=mm(340),
            shelf_count=2,
            vertical_divider_count=1,
            base_cabinet_height_um=mm(450),
            base_cabinet_depth_um=mm(340),
            base_cabinet_count=2,
        )
    raise DesignReviewGateError(f"no screened gate case exists for {template_id}")


def _stock_profiles(template_id: str, design: Any) -> tuple[StockSheet, ...]:
    main_quantity = 2 if template_id == "shelving" else 4
    return (
        StockSheet(
            stock_id=f"gate-{template_id}-mdf-18",
            material_id=design.spec.material.material_id,
            material_version=design.spec.material.version,
            width_um=mm(2_440),
            height_um=mm(1_220),
            thickness_um=mm(18),
            quantity=main_quantity,
            grain_direction=design.spec.material.grain_direction.value.upper(),
        ),
        StockSheet(
            stock_id=f"gate-{template_id}-mdf-6",
            material_id=design.spec.back_material.material_id,
            material_version=design.spec.back_material.version,
            width_um=mm(2_440),
            height_um=mm(1_220),
            thickness_um=mm(6),
            quantity=1,
            # This automated fixture is not supplier evidence for back-sheet grain.
            grain_direction="NONE",
        ),
    )


def deterministic_gate_fixture_payload() -> dict[str, Any]:
    """Generate the canonical validation-only registration fixture.

    The coordinates are deterministic test inputs, never inferred workshop
    evidence. Keeping the checked-in fixture equal to this payload prevents a
    concept template or stale sheet count from remaining in the golden data.
    """

    templates: dict[str, Any] = {}
    for template_id in SCREENED_TEMPLATE_IDS:
        design = build_bookcase(
            BookcaseDesignSpec(
                design_id=f"automated-design-review-{template_id}",
                template_id=template_id,
                parameters=_template_parameters(template_id),
                material=screening_mdf_18(),
                back_material=screening_mdf_6(),
            )
        )
        registrations_by_stock: dict[str, Any] = {}
        for stock in _stock_profiles(template_id, design):
            stock_suffix = stock.stock_id.removeprefix(f"gate-{template_id}-")
            registrations_by_stock[stock.stock_id] = {
                str(sheet_index): {
                    "method_id": (
                        f"automated-design-review:{template_id}:{stock_suffix}:"
                        f"sheet-{sheet_index + 1:03d}"
                    ),
                    "points_um": [
                        [50_000, 50_000],
                        [stock.width_um - 50_000, 50_000],
                    ],
                }
                for sheet_index in range(stock.quantity)
            }
        templates[template_id] = {"registrations_by_stock": registrations_by_stock}
    return {
        "schema_version": GATE_FIXTURE_SCHEMA_VERSION,
        "fixture_scope": GATE_FIXTURE_SCOPE,
        "warning": GATE_FIXTURE_WARNING,
        "templates": templates,
    }


def _actual_default_preview_payload(
    default: ScreenedTemplateDefault,
) -> dict[str, Any]:
    spec = default.effective_design_spec
    return {
        "furniture_type": spec["furniture_type"],
        "width_mm": spec["width_mm"],
        "height_mm": spec["height_mm"],
        "depth_mm": spec["depth_mm"],
        "material_id": spec["material_id"],
        "nominal_thickness_mm": spec["nominal_thickness_mm"],
        "measured_thickness_mm": spec["measured_thickness_mm"],
        "shelf_count": spec["shelf_count"],
        "shelf_mount": "fixed" if spec["fixed_shelves"] else "adjustable",
        "load_per_shelf_kg": spec["load_per_shelf_kg"],
        "back_panel": spec["back_panel"],
        "plinth": spec["plinth"],
        "divider_count": spec["divider_count"],
        "bay_width_ratios": spec["bay_width_ratios"],
        "shelf_height_ratios": spec["shelf_height_ratios"],
        "base_cabinet_height_mm": spec["base_cabinet_height_mm"],
        "base_cabinet_depth_mm": spec["base_cabinet_depth_mm"],
        "base_cabinet_count": spec["base_cabinet_count"],
        "edge_band_mm": spec["edge_band_mm"],
        "joint_system": spec["joint_system"],
        "reinforcement_mode": spec["reinforcement_mode"],
        # Browser assertions are never accepted as external approval evidence.
        "wall_anchor_required": False,
        "wall_anchor_verified": False,
    }


def _actual_default_stocks(
    default: ScreenedTemplateDefault,
    design: Any,
) -> tuple[StockSheet, ...]:
    spec = default.effective_design_spec
    carcass = StockSheet(
        stock_id=(
            f"stock-{design.spec.material.material_id}-"
            f"{spec['stock_width_mm']}x{spec['stock_height_mm']}"
        ),
        material_id=design.spec.material.material_id,
        material_version=design.spec.material.version,
        width_um=int(spec["stock_width_mm"]) * 1_000,
        height_um=int(spec["stock_height_mm"]) * 1_000,
        thickness_um=design.spec.parameters.actual_thickness_um,
        quantity=int(spec["stock_count"]),
        # UI defaults do not bind a supplier-sheet axis. Keep it explicit.
        grain_direction="UNBOUND",
    )
    stocks = [carcass]
    if design.spec.back_material is not None:
        stocks.append(
            StockSheet(
                stock_id=(
                    f"stock-{design.spec.back_material.material_id}-"
                    f"{spec['back_stock_width_mm']}x{spec['back_stock_height_mm']}"
                ),
                material_id=design.spec.back_material.material_id,
                material_version=design.spec.back_material.version,
                width_um=int(spec["back_stock_width_mm"]) * 1_000,
                height_um=int(spec["back_stock_height_mm"]) * 1_000,
                thickness_um=design.spec.parameters.back_thickness_um,
                quantity=int(spec["back_stock_count"]),
                grain_direction="UNBOUND",
            )
        )
    return tuple(stocks)


def _stock_snapshot(stock: StockSheet) -> dict[str, Any]:
    return {
        "stock_id": stock.stock_id,
        "material_id": stock.material_id,
        "material_version": stock.material_version,
        "width_um": stock.width_um,
        "height_um": stock.height_um,
        "thickness_um": stock.thickness_um,
        "quantity": stock.quantity,
        "grain_direction": stock.grain_direction,
    }


def _actual_default_cad_evidence(design: Any) -> dict[str, Any]:
    cad = CadQueryAdapter().export_design(design)
    return {
        "status": "PASS",
        "scope": "IN_MEMORY_DESIGN_REVIEW_EVIDENCE",
        "authoritative_geometry": cad.authoritative,
        "adapter_version": cad.adapter_version,
        "kernel": cad.kernel,
        "step": {
            "sha256": sha256_hex(cad.step),
            "size_bytes": len(cad.step),
        },
        "glb": {
            "sha256": sha256_hex(cad.glb),
            "size_bytes": len(cad.glb),
        },
        "production_ready": False,
        "physical_cutting_authorized": False,
    }


def _registrations_for(
    fixture: GateFixture,
    template_id: str,
    stocks: Sequence[StockSheet],
) -> dict[str, dict[int, TwoSidedRegistration]]:
    declared = fixture.registrations.get(template_id)
    if declared is None:
        raise DesignReviewGateError(f"fixture has no registration data for {template_id}")
    expected_stock_ids = {stock.stock_id for stock in stocks}
    if set(declared) != expected_stock_ids:
        raise DesignReviewGateError(
            f"fixture stocks for {template_id} must be exactly {sorted(expected_stock_ids)}"
        )

    resolved: dict[str, dict[int, TwoSidedRegistration]] = {}
    for stock in stocks:
        plans = declared[stock.stock_id]
        expected_sheets = set(range(stock.quantity))
        if set(plans) != expected_sheets:
            raise DesignReviewGateError(
                f"fixture sheets for {stock.stock_id} must be exactly {sorted(expected_sheets)}"
            )
        for sheet_index, plan in plans.items():
            if any(
                point.x_um < 0
                or point.y_um < 0
                or point.x_um > stock.width_um
                or point.y_um > stock.height_um
                for point in plan.points
            ):
                raise DesignReviewGateError(
                    f"fixture registration for {stock.stock_id} sheet {sheet_index} "
                    "is outside the stock frame"
                )
        resolved[stock.stock_id] = dict(plans)
    return resolved


def _project_version(repo: Path) -> str:
    try:
        raw = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
        value = raw["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise DesignReviewGateError(f"cannot resolve project version: {exc}") from exc
    if not isinstance(value, str) or not value:
        raise DesignReviewGateError("project.version must be a non-empty string")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DesignReviewGateError(f"cannot hash {path.name}: {exc}") from exc
    return digest.hexdigest()


def _manifest_context(
    *,
    repo: Path,
    template_id: str,
    capability: TemplateCapability,
    design: Any,
    stocks: Sequence[StockSheet],
    fixture: GateFixture,
    machine: Any,
    engine_context: Any,
) -> ManifestContext:
    request = {
        "fixture_fingerprint": fixture.fingerprint,
        "fixture_scope": GATE_FIXTURE_SCOPE,
        "include_step": True,
        "include_validation_program": True,
        "production_release": False,
        "stocks": [
            {
                "stock_id": stock.stock_id,
                "material_id": stock.material_id,
                "material_version": stock.material_version,
                "width_um": stock.width_um,
                "height_um": stock.height_um,
                "thickness_um": stock.thickness_um,
                "quantity": stock.quantity,
                "grain_direction": stock.grain_direction,
            }
            for stock in stocks
        ],
        "template_id": template_id,
    }
    context_hash = generation_context_hash(
        design_context_hash=design.design_hash,
        design_version_id=f"automated-design-review:{template_id}",
        revision=1,
        request=request,
        production_engine_context=engine_context,
    )
    return ManifestContext(
        project_id=f"automated-design-review-{template_id}",
        revision="1",
        design_hash=design.design_hash,
        app_version=_project_version(repo),
        engine_version="derived-by-pipeline",
        template_version="derived-by-pipeline",
        template_id=template_id,
        template_capability_fingerprint=capability.capability_fingerprint,
        template_capability=capability.snapshot(),
        rule_version=f"bookcase-rules@{RULES_VERSION}",
        material_versions=(),
        joint_version=BOOKCASE_JOINT_SUPPORT_VERSION,
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        postprocessor_version="derived-by-pipeline",
        cad_status="derived-by-pipeline",
        generation_context_hash=context_hash,
        production_engine_context=engine_context.as_dict(),
        warnings=(
            "Automated design-review fixture only; no physical machine or workshop evidence.",
        ),
    )


def _actual_default_manifest_context(
    *,
    repo: Path,
    default: ScreenedTemplateDefault,
    contract: ScreenedDefaultsContract,
    capability: TemplateCapability,
    design: Any,
    stocks: Sequence[StockSheet],
    machine: Any,
    engine_context: Any,
) -> ManifestContext:
    template_id = default.template_id
    request = {
        "gate_track": "ACTUAL_EFFECTIVE_UI_DEFAULT",
        "screened_defaults_contract": {
            "schema_version": SCREENED_DEFAULTS_SCHEMA_VERSION,
            "contract_version": contract.contract_version,
            "fingerprint": contract.fingerprint,
        },
        "effective_default_input_fingerprint": default.input_fingerprint,
        "include_step": True,
        "include_validation_program": True,
        "production_release": False,
        "stocks": [_stock_snapshot(stock) for stock in stocks],
        "template_id": template_id,
    }
    context_hash = generation_context_hash(
        design_context_hash=design.design_hash,
        design_version_id=f"automated-design-review:actual-default:{template_id}",
        revision=1,
        request=request,
        production_engine_context=engine_context,
    )
    return ManifestContext(
        project_id=f"automated-design-review-actual-default-{template_id}",
        revision="1",
        design_hash=design.design_hash,
        app_version=_project_version(repo),
        engine_version="derived-by-pipeline",
        template_version="derived-by-pipeline",
        template_id=template_id,
        template_capability_fingerprint=capability.capability_fingerprint,
        template_capability=capability.snapshot(),
        rule_version=f"bookcase-rules@{RULES_VERSION}",
        material_versions=(),
        joint_version=BOOKCASE_JOINT_SUPPORT_VERSION,
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        postprocessor_version="derived-by-pipeline",
        cad_status="derived-by-pipeline",
        generation_context_hash=context_hash,
        production_engine_context=engine_context.as_dict(),
        warnings=(
            "Actual effective UI default under automated design review; not a physical release.",
            "No stock-frame registration is inferred; manufacturing remains fail-closed.",
        ),
    )


def _zip_manifest_bytes(payload: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(payload), mode="r") as archive:
            return archive.read("manifest.json")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise DesignReviewGateError("generated package has no readable manifest.json") from exc


def _verified_bundle_report(
    *,
    template_id: str,
    capability: TemplateCapability,
    design: Any,
    bundle: ProductionBundle,
    context: ManifestContext,
    expected_design_hash: str,
    expected_domain_engine_version: str,
    expected_domain_template_version: str,
    expected_material_versions: Sequence[str],
    source_manifest_sha256: str,
    fixture_fingerprint: str | None,
) -> dict[str, Any]:
    verified_manifest = read_and_verify_package(bundle.zip_bytes)
    if verified_manifest != bundle.manifest:
        raise DesignReviewGateError("pipeline manifest differs from independently reparsed ZIP")
    manifest = verified_manifest
    artifact_entries = manifest.get("artifacts")
    if not isinstance(artifact_entries, list):
        raise DesignReviewGateError("manifest artifact inventory is missing")
    inventory = sorted(
        (
            {
                "media_type": entry.get("media_type"),
                "path": entry.get("path"),
                "role": entry.get("role"),
                "sha256": entry.get("sha256"),
                "size_bytes": entry.get("size_bytes"),
            }
            for entry in artifact_entries
            if isinstance(entry, Mapping)
        ),
        key=lambda entry: str(entry["path"]),
    )
    if len(inventory) != len(artifact_entries):
        raise DesignReviewGateError("manifest artifact inventory contains a non-object entry")
    paths = {str(entry["path"]) for entry in inventory}
    cam_blocked = bundle.review_status.cam_status.value == "BLOCKED"
    dfm_blocker_codes = {"STOCK_PROFILE_MISSING", "DFM-GRAIN-001"}
    admitted_blocker_codes = {
        *dfm_blocker_codes,
        DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    }
    blocker_codes = bundle.review_status.blocker_codes
    dfm_blocked = len(blocker_codes) == 1 and blocker_codes[0] in dfm_blocker_codes
    retention_blocked = blocker_codes == (DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,)
    if cam_blocked and (
        len(blocker_codes) != 1 or blocker_codes[0] not in admitted_blocker_codes
    ):
        raise DesignReviewGateError(
            "design-review gate only admits an exact stock, grain or DADO-retention blocker"
        )
    required_core = REQUIRED_BLOCKED_REVIEW_ARTIFACTS if cam_blocked else REQUIRED_CORE_ARTIFACTS
    missing_core = sorted(required_core - paths)
    if missing_core:
        raise DesignReviewGateError(f"design-review package lacks core artifacts: {missing_core}")
    if not any(path.startswith("parts/") and path.endswith(".dxf") for path in paths):
        raise DesignReviewGateError("design-review package has no part DXF")
    if cam_blocked:
        if bundle.operations is not None or bundle.layouts:
            raise DesignReviewGateError("CAM-blocked review package contains CAM or nesting state")
        if any(
            blocked_cam_artifact_violation(
                str(entry["path"]),
                str(entry["role"]),
                str(entry["media_type"]),
            )
            for entry in inventory
        ):
            raise DesignReviewGateError(
                "CAM-blocked review package contains a stock, nesting or CAM artifact"
            )
        blocking_codes = {
            issue.code for issue in bundle.dfm_report.issues if issue.severity.value == "BLOCK"
        }
        if dfm_blocked and blocking_codes != set(blocker_codes):
            raise DesignReviewGateError(
                "blocked review package does not preserve its exact raw DFM blocker"
            )
        if retention_blocked and blocking_codes:
            raise DesignReviewGateError(
                "DADO-retention review blocker must not be misrepresented as a DFM blocker"
            )
    else:
        if not any(path.startswith("cam/setups/") for path in paths):
            raise DesignReviewGateError("design-review package has no setup sheet")
        if not any(path.startswith("nesting/") for path in paths):
            raise DesignReviewGateError("design-review package has no nesting map")

    readiness = bundle.workshop_readiness.as_dict()
    embedded_readiness = next(
        artifact.data
        for artifact in bundle.artifacts
        if artifact.path == "validation/workshop-readiness.json"
    )
    if canonical_json_bytes(json.loads(embedded_readiness)) != canonical_json_bytes(readiness):
        raise DesignReviewGateError("embedded workshop readiness differs from pipeline result")
    if readiness.get("design_review_ready") is not (not cam_blocked):
        raise DesignReviewGateError("software readiness does not match the review package status")
    if readiness.get("physical_cutting_authorized") is not False:
        raise DesignReviewGateError("workshop readiness attempted to authorize physical cutting")
    if manifest.get("physical_cutting_authorized") is not False:
        raise DesignReviewGateError("manifest attempted to authorize physical cutting")
    if manifest.get("release_scope") != "design_review":
        raise DesignReviewGateError("manifest release_scope is not design_review")
    if manifest.get("machine_use") != "validation_only":
        raise DesignReviewGateError("manifest machine_use is not validation_only")
    if manifest.get("cad_status") != "GENERATED":
        raise DesignReviewGateError("authoritative CAD was not generated")
    if not cam_blocked and (bundle.operations is None or bundle.operations.mode != "VALIDATION"):
        raise DesignReviewGateError("operations document is not validation-only")

    registry = template_capability_registry_payload()
    engine_context = context.production_engine_context
    expected_provenance = {
        "design_hash": expected_design_hash,
        "domain_engine_version": expected_domain_engine_version,
        "domain_template_version": expected_domain_template_version,
        "generation_context_hash": context.generation_context_hash,
        "machine_profile_id": context.machine_profile_id,
        "machine_profile_version": context.machine_profile_version,
        "material_versions": sorted(expected_material_versions),
        "source_manifest_sha256": source_manifest_sha256,
        "template_capability_fingerprint": capability.capability_fingerprint,
        "template_capability_registry_fingerprint": registry["registry_fingerprint"],
        "template_capability_registry_version": registry["registry_version"],
        "template_capability_version": capability.template_version,
    }
    actual_provenance = {
        "design_hash": manifest.get("design_hash"),
        "domain_engine_version": manifest.get("engine_version"),
        "domain_template_version": manifest.get("domain_template_version"),
        "generation_context_hash": manifest.get("generation_context_hash"),
        "machine_profile_id": manifest.get("machine_profile", {}).get("id"),
        "machine_profile_version": manifest.get("machine_profile", {}).get("version"),
        "material_versions": list(manifest.get("material_versions", [])),
        "source_manifest_sha256": engine_context.get("source_manifest_sha256"),
        "template_capability_fingerprint": manifest.get("template_capability_fingerprint"),
        "template_capability_registry_fingerprint": engine_context.get(
            "template_capability_registry_fingerprint"
        ),
        "template_capability_registry_version": manifest.get(
            "template_capability_registry_version"
        ),
        "template_capability_version": manifest.get("template_capability_version"),
    }
    if actual_provenance != expected_provenance:
        raise DesignReviewGateError("manifest provenance does not match the frozen gate context")
    if manifest.get("template_capability") != capability.snapshot():
        raise DesignReviewGateError("manifest capability snapshot differs from server registry")
    if manifest.get("production_engine_context") != context.production_engine_context:
        raise DesignReviewGateError("manifest production engine context differs from gate context")
    if manifest.get("app_version") != context.app_version:
        raise DesignReviewGateError("manifest application version differs from gate context")

    external = sorted(
        requirement["code"]
        for requirement in readiness["workshop_evidence"]
        if requirement["status"] == GateStatus.EXTERNAL_EVIDENCE_REQUIRED
    )
    if not external:
        raise DesignReviewGateError(
            "gate unexpectedly found no external workshop evidence requirement"
        )

    manifest_bytes = _zip_manifest_bytes(bundle.zip_bytes)
    report: dict[str, Any] = {
        "template_id": template_id,
        "production_level": capability.production_level.value,
        "status": GateStatus.EXTERNAL_EVIDENCE_REQUIRED,
        "design_review_integrity_verified": True,
        "pipeline_executed": True,
        "physical_release_status": GateStatus.BLOCK,
        "physical_cutting_authorized": False,
        "package": {
            "artifact_count": len(inventory),
            "artifact_inventory": inventory,
            "manifest_sha256": sha256_hex(manifest_bytes),
            "sha256": sha256_hex(bundle.zip_bytes),
            "size_bytes": len(bundle.zip_bytes),
        },
        "provenance": actual_provenance,
        "workshop_readiness": {
            "design_review_ready": not cam_blocked,
            "external_evidence_required": external,
            "missing_evidence_count": readiness["missing_evidence_count"],
            "physical_cutting_authorized": False,
            "schema_version": readiness["schema_version"],
        },
        "design_review_package_status": bundle.review_status.as_dict(),
        "dfm_status": bundle.dfm_report.status.value,
    }
    if cam_blocked:
        report["blocking_issue_codes"] = list(blocker_codes)
    if dfm_blocked:
        raw_dfm = json.loads(canonical_json_bytes(bundle.dfm_report))
        names_by_part_id = {
            str(part.part_id): str(getattr(part.role, "value", part.role)).lower()
            for part in getattr(design, "parts", ())
        }
        blocking_issues: list[dict[str, Any]] = []
        for issue in bundle.dfm_report.blocking_issues:
            raw_issue = json.loads(canonical_json_bytes(issue))
            part_id = raw_issue.get("part_id")
            if isinstance(part_id, str) and part_id in names_by_part_id:
                raw_issue["part_name"] = names_by_part_id[part_id]
            blocking_issues.append(raw_issue)
        report["dfm_report"] = raw_dfm
        report["blocking_issues"] = blocking_issues
    if fixture_fingerprint is not None:
        report["fixture_scope"] = GATE_FIXTURE_SCOPE
        report["fixture_fingerprint"] = fixture_fingerprint
    return report


def _canonical_default_metadata(
    default: ScreenedTemplateDefault,
    design: Any,
    presented: Mapping[str, Any],
) -> dict[str, Any]:
    status = presented.get("status")
    if not isinstance(status, str) or status not in CANONICAL_CONSTRUCTION_STATUSES:
        raise DesignReviewGateError(
            "server canonical construction-rule status must be exactly one of "
            f"{sorted(CANONICAL_CONSTRUCTION_STATUSES)}"
        )
    return {
        "status": status,
        "effective_default_input_fingerprint": default.input_fingerprint,
        "canonical_spec_sha256": sha256_hex(canonical_json_bytes(design.spec)),
        "design_hash": design.design_hash,
        "engine_version": design.engine_version,
        "domain_template_version": design.template_version,
        "change_diff": list(presented.get("change_diff", [])),
        "physical_cutting_authorized": False,
    }


def _actual_default_failure(
    *,
    default: ScreenedTemplateDefault,
    capability: TemplateCapability,
    contract: ScreenedDefaultsContract,
    code: str,
    detail: str,
    stage: str,
    pipeline_executed: bool,
    canonical: Mapping[str, Any] | None = None,
    cad: Mapping[str, Any] | None = None,
    stocks: Sequence[StockSheet] = (),
    dfm_report: object | None = None,
    design: Any | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "template_id": default.template_id,
        "production_level": capability.production_level.value,
        "input_kind": "ACTUAL_EFFECTIVE_UI_DEFAULT",
        "status": GateStatus.BLOCK,
        "code": code,
        "detail": detail,
        "blocked_stage": stage,
        "design_review_integrity_verified": False,
        "pipeline_executed": pipeline_executed,
        "physical_release_status": GateStatus.BLOCK,
        "physical_cutting_authorized": False,
        "screened_defaults_contract": {
            "schema_version": SCREENED_DEFAULTS_SCHEMA_VERSION,
            "contract_version": contract.contract_version,
            "fingerprint": contract.fingerprint,
        },
        "effective_default_input_fingerprint": default.input_fingerprint,
        "stock_profiles": [_stock_snapshot(stock) for stock in stocks],
    }
    if canonical is not None:
        report["server_canonical"] = dict(canonical)
    if cad is not None:
        report["cad"] = dict(cad)
    if dfm_report is not None:
        raw_dfm = json.loads(canonical_json_bytes(dfm_report))
        blocking_issues = tuple(getattr(dfm_report, "blocking_issues", ()))
        names_by_part_id = {
            str(part.part_id): str(getattr(part.role, "value", part.role)).lower()
            for part in getattr(design, "parts", ())
        }
        augmented_issues: list[dict[str, Any]] = []
        for issue in blocking_issues:
            raw_issue = json.loads(canonical_json_bytes(issue))
            part_id = raw_issue.get("part_id")
            if isinstance(part_id, str) and part_id in names_by_part_id:
                raw_issue["part_name"] = names_by_part_id[part_id]
            augmented_issues.append(raw_issue)
        report["dfm_report"] = raw_dfm
        report["blocking_issues"] = augmented_issues
        report["blocking_issue_codes"] = sorted(
            {str(issue.get("code")) for issue in augmented_issues}
        )
    return report


def _run_actual_default(
    *,
    repo: Path,
    default: ScreenedTemplateDefault,
    contract: ScreenedDefaultsContract,
    capability: TemplateCapability,
    source_manifest_sha256: str,
    machine: Any,
    engine_context: Any,
) -> dict[str, Any]:
    pipeline_executed = False
    canonical: Mapping[str, Any] | None = None
    cad: Mapping[str, Any] | None = None
    stocks: tuple[StockSheet, ...] = ()
    design: Any | None = None
    try:
        validated = BookcasePreviewInput.model_validate(_actual_default_preview_payload(default))
        _, design, presented = canonical_preview(
            validated.model_dump(mode="json", exclude_none=True),
            design_id=f"automated-design-review-actual-default-{default.template_id}",
            revision=1,
        )
        canonical = _canonical_default_metadata(default, design, presented)
    except Exception as exc:
        return _actual_default_failure(
            default=default,
            capability=capability,
            contract=contract,
            code="ACTUAL_DEFAULT_SERVER_CANONICALIZATION_FAILED",
            detail=str(exc),
            stage="SERVER_CANONICALIZATION",
            pipeline_executed=False,
        )

    if canonical["status"] == "BLOCK":
        return _actual_default_failure(
            default=default,
            capability=capability,
            contract=contract,
            code="ACTUAL_DEFAULT_CONSTRUCTION_RULES_BLOCKED",
            detail=(
                "Server canonical construction rules returned BLOCK; authoritative CAD "
                "and manufacturing were not executed."
            ),
            stage="CONSTRUCTION_RULES",
            pipeline_executed=False,
            canonical=canonical,
        )

    try:
        cad = _actual_default_cad_evidence(design)
    except Exception as exc:
        return _actual_default_failure(
            default=default,
            capability=capability,
            contract=contract,
            code="ACTUAL_DEFAULT_CAD_EVIDENCE_FAILED",
            detail=str(exc),
            stage="AUTHORITATIVE_CAD",
            pipeline_executed=False,
            canonical=canonical,
        )

    try:
        stocks = _actual_default_stocks(default, design)
        context = _actual_default_manifest_context(
            repo=repo,
            default=default,
            contract=contract,
            capability=capability,
            design=design,
            stocks=stocks,
            machine=machine,
            engine_context=engine_context,
        )
        pipeline_executed = True
        bundle = build_production_bundle(
            design,
            stock=stocks,
            machine=machine,
            context=context,
            include_step=True,
            include_freecad_project=False,
            include_validation_program=True,
            production_release=False,
            allow_blocked_cam=True,
            # There is deliberately no inferred registration plan for this track.
            two_sided_registration_by_stock={},
        )
    except ProductionBlockedError as exc:
        raw_codes = sorted(
            {str(issue.code) for issue in getattr(exc.report, "blocking_issues", ())}
        )
        code = raw_codes[0] if len(raw_codes) == 1 else "ACTUAL_DEFAULT_MANUFACTURING_BLOCKED"
        return _actual_default_failure(
            default=default,
            capability=capability,
            contract=contract,
            code=code,
            detail=str(exc),
            stage="MANUFACTURING_DFM" if exc.report is not None else "MANUFACTURING_PIPELINE",
            pipeline_executed=pipeline_executed,
            canonical=canonical,
            cad=cad,
            stocks=stocks,
            dfm_report=exc.report,
            design=design,
        )
    except Exception as exc:
        return _actual_default_failure(
            default=default,
            capability=capability,
            contract=contract,
            code="ACTUAL_DEFAULT_MANUFACTURING_ATTEMPT_FAILED",
            detail=str(exc),
            stage="MANUFACTURING_PIPELINE",
            pipeline_executed=pipeline_executed,
            canonical=canonical,
            cad=cad,
            stocks=stocks,
        )

    verified = _verified_bundle_report(
        template_id=default.template_id,
        capability=capability,
        design=design,
        bundle=bundle,
        context=context,
        expected_design_hash=design.design_hash,
        expected_domain_engine_version=design.engine_version,
        expected_domain_template_version=design.template_version,
        expected_material_versions=tuple(
            f"{material.material_id}@{material.version}"
            for material in (design.spec.material, design.spec.back_material)
            if material is not None
        ),
        source_manifest_sha256=source_manifest_sha256,
        fixture_fingerprint=None,
    )
    verified.update(
        {
            "input_kind": "ACTUAL_EFFECTIVE_UI_DEFAULT",
            "screened_defaults_contract": {
                "schema_version": SCREENED_DEFAULTS_SCHEMA_VERSION,
                "contract_version": contract.contract_version,
                "fingerprint": contract.fingerprint,
            },
            "effective_default_input_fingerprint": default.input_fingerprint,
            "server_canonical": dict(canonical),
            "cad": dict(cad),
            "stock_profiles": [_stock_snapshot(stock) for stock in stocks],
        }
    )
    return verified


def _concept_report(capability: TemplateCapability) -> dict[str, Any]:
    return {
        "template_id": capability.template_id,
        "production_level": capability.production_level.value,
        "status": GateStatus.BLOCK,
        "code": "CONCEPT_TEMPLATE_NOT_RELEASEABLE",
        "detail": capability.limitation,
        "design_review_integrity_verified": False,
        "pipeline_executed": False,
        "physical_release_status": GateStatus.BLOCK,
        "physical_cutting_authorized": False,
    }


def run_design_review_gate(
    fixture: GateFixture, *, repo: Path = DEFAULT_REPOSITORY
) -> dict[str, Any]:
    """Run separate engine-smoke and actual-effective-default review tracks."""

    root = repo.resolve()
    contract = load_screened_defaults_contract(root / SCREENED_DEFAULTS_CONTRACT_PATH)
    try:
        source_manifest_sha256 = build_source_manifest(root)[2]
    except (OSError, SourceManifestError) as exc:
        raise DesignReviewGateError(f"cannot establish source provenance: {exc}") from exc
    dependency_lock_sha256 = _file_sha256(root / "uv.lock")
    app_version = _project_version(root)
    postprocessor = LinuxCNCValidationPostprocessor()
    resolved = resolve_production_components(
        machine_profile_id="custombuild-router-1325-linuxcnc",
        postprocessor_id=postprocessor.version,
        app_version=app_version,
        vcs_ref=f"source-manifest:{source_manifest_sha256}",
        build_date="DETERMINISTIC_DESIGN_REVIEW_GATE",
        source_url="urn:custombuild:automated-design-review",
        source_manifest_sha256=source_manifest_sha256,
        dependency_lock_sha256=dependency_lock_sha256,
        require_cad_runtime=True,
    )

    registry = template_capability_registry_payload()
    raw_capabilities = registry.get("templates")
    if not isinstance(raw_capabilities, list):
        raise DesignReviewGateError("template capability registry has no templates array")
    capabilities = sorted(
        (_object(item, "template capability") for item in raw_capabilities),
        key=lambda item: str(item["template_id"]),
    )
    capability_by_id = {str(item["template_id"]): item for item in capabilities}

    engine_smoke_reports: list[dict[str, Any]] = []
    for template_id in SCREENED_TEMPLATE_IDS:
        pipeline_executed = False
        try:
            capability = require_template_for_revision(
                template_id,
                "wall_library" if template_id == "wall-library" else "bookcase",
            )
            design = build_bookcase(
                BookcaseDesignSpec(
                    design_id=f"automated-design-review-{template_id}",
                    template_id=template_id,
                    parameters=_template_parameters(template_id),
                    material=screening_mdf_18(),
                    back_material=screening_mdf_6(),
                )
            )
            stocks = _stock_profiles(template_id, design)
            registrations = _registrations_for(fixture, template_id, stocks)
            context = _manifest_context(
                repo=root,
                template_id=template_id,
                capability=capability,
                design=design,
                stocks=stocks,
                fixture=fixture,
                machine=resolved.machine,
                engine_context=resolved.context,
            )
            pipeline_executed = True
            bundle = build_production_bundle(
                design,
                stock=stocks,
                machine=resolved.machine,
                context=context,
                include_step=True,
                include_freecad_project=False,
                include_validation_program=True,
                production_release=False,
                allow_blocked_cam=True,
                two_sided_registration_by_stock=registrations,
            )
            report = _verified_bundle_report(
                template_id=template_id,
                capability=capability,
                design=design,
                bundle=bundle,
                context=context,
                expected_design_hash=design.design_hash,
                expected_domain_engine_version=design.engine_version,
                expected_domain_template_version=design.template_version,
                expected_material_versions=tuple(
                    f"{material.material_id}@{material.version}"
                    for material in (design.spec.material, design.spec.back_material)
                    if material is not None
                ),
                source_manifest_sha256=source_manifest_sha256,
                fixture_fingerprint=fixture.fingerprint,
            )
            report["input_kind"] = "DETERMINISTIC_MDF_ENGINE_SMOKE"
            engine_smoke_reports.append(report)
        except Exception as exc:  # Convert every screened failure into a closed gate result.
            engine_smoke_reports.append(
                {
                    "template_id": template_id,
                    "production_level": capability_by_id[template_id]["production_level"],
                    "input_kind": "DETERMINISTIC_MDF_ENGINE_SMOKE",
                    "status": GateStatus.BLOCK,
                    "code": "SCREENED_TEMPLATE_ENGINE_SMOKE_FAILED",
                    "detail": str(exc),
                    "design_review_integrity_verified": False,
                    "pipeline_executed": pipeline_executed,
                    "physical_release_status": GateStatus.BLOCK,
                    "physical_cutting_authorized": False,
                }
            )

    actual_default_reports: list[dict[str, Any]] = []
    for default in contract.templates:
        try:
            furniture_type = str(default.effective_design_spec["furniture_type"])
            capability = require_template_for_revision(default.template_id, furniture_type)
            actual_default_reports.append(
                _run_actual_default(
                    repo=root,
                    default=default,
                    contract=contract,
                    capability=capability,
                    source_manifest_sha256=source_manifest_sha256,
                    machine=resolved.machine,
                    engine_context=resolved.context,
                )
            )
        except Exception as exc:
            actual_default_reports.append(
                {
                    "template_id": default.template_id,
                    "production_level": capability_by_id[default.template_id]["production_level"],
                    "input_kind": "ACTUAL_EFFECTIVE_UI_DEFAULT",
                    "status": GateStatus.BLOCK,
                    "code": "ACTUAL_DEFAULT_GATE_RUNTIME_FAILED",
                    "detail": str(exc),
                    "design_review_integrity_verified": False,
                    "pipeline_executed": False,
                    "physical_release_status": GateStatus.BLOCK,
                    "physical_cutting_authorized": False,
                    "effective_default_input_fingerprint": default.input_fingerprint,
                }
            )

    concept_reports: list[dict[str, Any]] = []
    for raw_capability in capabilities:
        template_id = str(raw_capability["template_id"])
        if template_id in SCREENED_TEMPLATE_IDS:
            continue
        capability = require_concept_capability(template_id)
        concept_reports.append(_concept_report(capability))

    engine_smoke_reports.sort(key=lambda item: str(item["template_id"]))
    actual_default_reports.sort(key=lambda item: str(item["template_id"]))
    concept_reports.sort(key=lambda item: str(item["template_id"]))
    engine_smoke_integrity_verified = len(engine_smoke_reports) == len(
        SCREENED_TEMPLATE_IDS
    ) and all(item["design_review_integrity_verified"] is True for item in engine_smoke_reports)
    actual_default_readiness_verified = len(actual_default_reports) == len(
        SCREENED_TEMPLATE_IDS
    ) and all(item["design_review_integrity_verified"] is True for item in actual_default_reports)
    gate_passed = engine_smoke_integrity_verified and actual_default_readiness_verified
    return {
        "schema_version": GATE_REPORT_SCHEMA_VERSION,
        "status": GateStatus.EXTERNAL_EVIDENCE_REQUIRED if gate_passed else GateStatus.BLOCK,
        "gate_passed": gate_passed,
        "design_review_integrity_verified": gate_passed,
        "engine_smoke_integrity_verified": engine_smoke_integrity_verified,
        "actual_default_readiness_verified": actual_default_readiness_verified,
        "external_blocker_status": GateStatus.EXTERNAL_EVIDENCE_REQUIRED,
        "physical_release_status": GateStatus.BLOCK,
        "physical_cutting_authorized": False,
        "source_manifest_sha256": source_manifest_sha256,
        "fixture": {
            "fingerprint": fixture.fingerprint,
            "scope": GATE_FIXTURE_SCOPE,
        },
        "template_capability_registry": {
            "fingerprint": registry["registry_fingerprint"],
            "version": registry["registry_version"],
        },
        "screened_template_defaults_contract": {
            "schema_version": SCREENED_DEFAULTS_SCHEMA_VERSION,
            "contract_version": contract.contract_version,
            "fingerprint": contract.fingerprint,
            "physical_cutting_authorized": False,
        },
        "engine_smoke": {
            "status": (
                GateStatus.EXTERNAL_EVIDENCE_REQUIRED
                if engine_smoke_integrity_verified
                else GateStatus.BLOCK
            ),
            "integrity_verified": engine_smoke_integrity_verified,
            "templates": engine_smoke_reports,
        },
        "actual_default_readiness": {
            "status": (
                GateStatus.EXTERNAL_EVIDENCE_REQUIRED
                if actual_default_readiness_verified
                else GateStatus.BLOCK
            ),
            "readiness_verified": actual_default_readiness_verified,
            "templates": actual_default_reports,
        },
        "concepts": concept_reports,
    }


def require_concept_capability(template_id: str) -> TemplateCapability:
    """Resolve a concept without ever passing it to the revision/build path."""

    from custombuild_domain import resolve_template_capability

    capability = resolve_template_capability(template_id)
    if capability.production_level is not TemplateProductionLevel.CONCEPT:
        raise DesignReviewGateError(f"unexpected non-concept template: {template_id}")
    return capability


def _blocked_report(detail: str) -> dict[str, Any]:
    return {
        "schema_version": GATE_REPORT_SCHEMA_VERSION,
        "status": GateStatus.BLOCK,
        "gate_passed": False,
        "code": "GATE_CONFIGURATION_OR_RUNTIME_FAILURE",
        "detail": detail,
        "design_review_integrity_verified": False,
        "engine_smoke_integrity_verified": False,
        "actual_default_readiness_verified": False,
        "external_blocker_status": GateStatus.EXTERNAL_EVIDENCE_REQUIRED,
        "physical_release_status": GateStatus.BLOCK,
        "physical_cutting_authorized": False,
        "engine_smoke": {"status": GateStatus.BLOCK, "integrity_verified": False, "templates": []},
        "actual_default_readiness": {
            "status": GateStatus.BLOCK,
            "readiness_verified": False,
            "templates": [],
        },
        "concepts": [],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)

    try:
        fixture = load_gate_fixture(arguments.fixture)
        report = run_design_review_gate(fixture, repo=arguments.repo)
    except DesignReviewGateError as exc:
        report = _blocked_report(str(exc))
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["gate_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
