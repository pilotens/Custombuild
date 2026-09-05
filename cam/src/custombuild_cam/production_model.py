"""Immutable contracts for deterministic, reviewable production CAM candidates.

These types deliberately stop short of authorizing a physical machine start.  A
document produced with them contains executable cutting motion, but remains an
``EXECUTABLE_CAM_CANDIDATE`` until the named workshop accepts the exact setup,
tool table, recipes and postprocessor output.

All dimensions and coordinates are integer micrometres.  Hashes are lowercase
SHA-256 hex digests over canonical JSON bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from custombuild_manufacturing.model import (
    OperationKind,
    Point2D,
    Rect,
    Side,
    canonical_data,
    canonical_json_bytes,
    sha256_hex,
)

PRODUCTION_TOOLPATH_SCHEMA_VERSION = "custombuild.toolpaths.v1"
PRODUCTION_TOOLPATH_ENGINE_VERSION = "production-toolpaths-1.1.0"
EXECUTABLE_CAM_CANDIDATE_MODE = "EXECUTABLE_CAM_CANDIDATE"
MAX_THROUGH_OVERTRAVEL_UM = 500
MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER = 2_147_483_647
IDENTITY_SOURCE_TO_WCS_XY = "IDENTITY_STOCK_XY_TO_WCS_XY"
FIXTURE_KEEPOUT_POLICY = "UNINFLATED_XY_FOOTPRINTS_MAX_Z_BOUND"
STOCK_TOP_Z0_REFERENCE = "STOCK_TOP_Z0"

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_WCS_PATTERN = re.compile(r"G5[4-9]")
_PLACEHOLDER_TOKENS = (
    "EXTERNAL",
    "PLACEHOLDER",
    "REQUIRED",
    "TBD",
    "TODO",
    "UNKNOWN",
    "UNRESOLVED",
    "UNVERIFIED",
)


class ProductionCAMError(ValueError):
    """A fail-closed production-CAM contract or planning failure."""


class ProductionToolGeometry(StrEnum):
    FLAT_END_MILL = "FLAT_END_MILL"
    DRILL = "DRILL"
    COUNTERSINK = "COUNTERSINK"


class ProductionMoveKind(StrEnum):
    RAPID = "RAPID"
    LINEAR = "LINEAR"


class ProductionMoveRole(StrEnum):
    POSITION = "POSITION"
    APPROACH = "APPROACH"
    CUT = "CUT"
    PECK_RETRACT = "PECK_RETRACT"
    TAB_RAMP = "TAB_RAMP"
    TAB_BRIDGE = "TAB_BRIDGE"
    RETRACT = "RETRACT"


def _require_identity(label: str, value: str) -> None:
    if not isinstance(value, str) or _IDENTITY_PATTERN.fullmatch(value) is None:
        raise ProductionCAMError(f"{label} must be a canonical non-blank identity")


def _require_hash(label: str, value: str) -> None:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ProductionCAMError(f"{label} must be a lowercase SHA-256 hex digest")


def _require_exact_setup_fact(label: str, value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProductionCAMError(f"{label} must be a canonical non-blank setup fact")
    upper = value.upper()
    if any(token in upper for token in _PLACEHOLDER_TOKENS):
        raise ProductionCAMError(f"{label} contains a placeholder instead of an accepted fact")


def _require_int(
    label: str,
    value: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> None:
    if type(value) is not int:
        raise ProductionCAMError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ProductionCAMError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ProductionCAMError(f"{label} must be at most {maximum}")


@dataclass(frozen=True, slots=True)
class ProductionToolBinding:
    """Exact physical tool assembly mapped to one validation-catalog tool.

    ``source_tool_*`` is provenance only.  ``tool_id``/``tool_version`` and the
    measured dimensions describe the independently accepted workshop tool that
    will actually be selected by the controller.  ``expected_length_offset_*``
    is the exact LinuxCNC tool-table state the operator must compare before
    execution; it is intentionally separate from physical stickout.  Every
    binding in one execution context must reference the same atomically
    captured tool-table evidence snapshot.
    """

    tool_id: str
    tool_version: str
    source_tool_id: str
    source_tool_version: str
    source_tool_sha256: str
    controller_tool_number: int
    length_offset_number: int
    expected_length_offset_x_um: int
    expected_length_offset_y_um: int
    expected_length_offset_z_um: int
    tool_table_evidence_id: str
    tool_table_evidence_version: str
    tool_table_evidence_sha256: str
    effective_diameter_um: int
    cutting_length_um: int
    measured_stickout_um: int
    minimum_holder_clearance_um: int
    assembly_collision_radius_um: int
    geometry: ProductionToolGeometry
    center_cutting: bool
    drill_point_length_um: int
    spindle_direction: str = "CW"

    def __post_init__(self) -> None:
        _require_identity("tool_id", self.tool_id)
        _require_identity("tool_version", self.tool_version)
        _require_identity("source_tool_id", self.source_tool_id)
        _require_identity("source_tool_version", self.source_tool_version)
        _require_hash("source_tool_sha256", self.source_tool_sha256)
        _require_int(
            "controller_tool_number",
            self.controller_tool_number,
            minimum=1,
            maximum=MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
        )
        _require_int(
            "length_offset_number",
            self.length_offset_number,
            minimum=1,
            maximum=MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
        )
        for label, offset in (
            ("expected_length_offset_x_um", self.expected_length_offset_x_um),
            ("expected_length_offset_y_um", self.expected_length_offset_y_um),
            ("expected_length_offset_z_um", self.expected_length_offset_z_um),
        ):
            _require_int(label, offset)
        if self.expected_length_offset_x_um or self.expected_length_offset_y_um:
            raise ProductionCAMError(
                "production CAM v1 requires zero expected X/Y tool-length offsets"
            )
        for label, identity in (
            ("tool_table_evidence_id", self.tool_table_evidence_id),
            ("tool_table_evidence_version", self.tool_table_evidence_version),
        ):
            _require_identity(label, identity)
            _require_exact_setup_fact(label, identity)
        _require_hash("tool_table_evidence_sha256", self.tool_table_evidence_sha256)
        _require_int("effective_diameter_um", self.effective_diameter_um, minimum=1)
        _require_int("cutting_length_um", self.cutting_length_um, minimum=1)
        _require_int("measured_stickout_um", self.measured_stickout_um, minimum=1)
        _require_int(
            "minimum_holder_clearance_um",
            self.minimum_holder_clearance_um,
            minimum=1,
        )
        _require_int(
            "assembly_collision_radius_um",
            self.assembly_collision_radius_um,
            minimum=1,
        )
        if self.cutting_length_um > self.measured_stickout_um:
            raise ProductionCAMError("tool cutting length cannot exceed measured stickout")
        if self.minimum_holder_clearance_um >= self.measured_stickout_um:
            raise ProductionCAMError("tool holder clearance must be smaller than measured stickout")
        if 2 * self.assembly_collision_radius_um < self.effective_diameter_um:
            raise ProductionCAMError(
                "tool assembly collision radius cannot be smaller than the cutter radius"
            )
        if not isinstance(self.geometry, ProductionToolGeometry):
            raise ProductionCAMError("tool geometry must be a production-tool enum value")
        if type(self.center_cutting) is not bool:
            raise ProductionCAMError("center_cutting must be a boolean")
        _require_int("drill_point_length_um", self.drill_point_length_um, minimum=0)
        if self.drill_point_length_um != 0:
            raise ProductionCAMError("production CAM v1 requires exactly zero drill point length")
        if self.geometry == ProductionToolGeometry.DRILL and not self.center_cutting:
            raise ProductionCAMError(
                "production drill must be center-cutting with a flat-bottom point contract"
            )
        if self.spindle_direction != "CW":
            raise ProductionCAMError("production CAM v1 supports clockwise spindle rotation only")


@dataclass(frozen=True, slots=True)
class CuttingRecipe:
    """Versioned, workshop-owned cutting parameters for one exact material/tool/kind.

    ``diameter_tolerance_um`` is a policy ceiling for the absolute difference
    between the operation's exact nominal diameter and the bound tool's exact
    measured effective diameter.  It is not another uncertainty term.  The
    generator separately adds ``process_accuracy_um`` to that actual diameter
    difference and requires the result to fit ``accepted_tolerance_um``.
    """

    recipe_id: str
    version: str
    machine_profile_id: str
    machine_profile_version: str
    material_id: str
    material_version: str
    tool_id: str
    tool_version: str
    operation_kind: OperationKind
    spindle_rpm: int
    feed_um_min: int
    plunge_um_min: int
    stepdown_um: int
    stepover_ppm: int
    peck_depth_um: int
    approach_clearance_um: int
    through_overtravel_um: int
    tab_width_um: int
    tab_height_um: int
    process_accuracy_um: int
    accepted_tolerance_um: int
    entry_strategy: str = "PLUNGE"
    diameter_tolerance_um: int = 0
    countersink_top_diameter_um: int | None = None
    countersink_included_angle_mdeg: int | None = None

    def __post_init__(self) -> None:
        for label, identity in (
            ("recipe_id", self.recipe_id),
            ("recipe version", self.version),
            ("machine_profile_id", self.machine_profile_id),
            ("machine_profile_version", self.machine_profile_version),
            ("material_id", self.material_id),
            ("material_version", self.material_version),
            ("tool_id", self.tool_id),
            ("tool_version", self.tool_version),
        ):
            _require_identity(label, identity)
        for label, numeric_value in (
            ("spindle_rpm", self.spindle_rpm),
            ("feed_um_min", self.feed_um_min),
            ("plunge_um_min", self.plunge_um_min),
            ("stepdown_um", self.stepdown_um),
            ("stepover_ppm", self.stepover_ppm),
            ("peck_depth_um", self.peck_depth_um),
            ("approach_clearance_um", self.approach_clearance_um),
            ("process_accuracy_um", self.process_accuracy_um),
            ("accepted_tolerance_um", self.accepted_tolerance_um),
        ):
            _require_int(label, numeric_value, minimum=1)
        if self.process_accuracy_um > self.accepted_tolerance_um:
            raise ProductionCAMError(
                "recipe process accuracy cannot exceed its accepted dimensional tolerance"
            )
        if self.stepover_ppm > 1_000_000:
            raise ProductionCAMError("stepover_ppm cannot exceed one tool diameter")
        if self.entry_strategy != "PLUNGE":
            raise ProductionCAMError("production CAM v1 supports explicit PLUNGE entry only")
        _require_int("diameter_tolerance_um", self.diameter_tolerance_um, minimum=0)
        if self.operation_kind != OperationKind.DRILL and self.diameter_tolerance_um:
            raise ProductionCAMError("diameter tolerance is only valid for drill recipes")
        _require_int("through_overtravel_um", self.through_overtravel_um, minimum=0)
        if self.through_overtravel_um > MAX_THROUGH_OVERTRAVEL_UM:
            raise ProductionCAMError(
                f"through overtravel cannot exceed {MAX_THROUGH_OVERTRAVEL_UM} um"
            )
        _require_int("tab_width_um", self.tab_width_um, minimum=0)
        _require_int("tab_height_um", self.tab_height_um, minimum=0)
        if self.operation_kind == OperationKind.CONTOUR:
            if self.through_overtravel_um == 0:
                raise ProductionCAMError("contour recipe requires positive through overtravel")
            if self.tab_width_um <= 0 or self.tab_height_um <= 0:
                raise ProductionCAMError("contour recipe requires explicit tab width and height")
            if self.tab_width_um <= 2 * self.process_accuracy_um:
                raise ProductionCAMError("contour tab width must exceed twice the process accuracy")
        elif self.tab_width_um or self.tab_height_um:
            raise ProductionCAMError("tabs are only valid for contour recipes")
        if self.operation_kind == OperationKind.COUNTERSINK:
            if (
                self.countersink_top_diameter_um is None
                or self.countersink_included_angle_mdeg is None
            ):
                raise ProductionCAMError(
                    "countersink recipe requires top diameter and included angle"
                )
            _require_int(
                "countersink_top_diameter_um",
                self.countersink_top_diameter_um,
                minimum=1,
            )
            _require_int(
                "countersink_included_angle_mdeg",
                self.countersink_included_angle_mdeg,
                minimum=60_000,
            )
            if self.countersink_included_angle_mdeg > 120_000:
                raise ProductionCAMError("countersink included angle exceeds 120 degrees")
        elif (
            self.countersink_top_diameter_um is not None
            or self.countersink_included_angle_mdeg is not None
        ):
            raise ProductionCAMError("countersink geometry is only valid for countersink recipes")


@dataclass(frozen=True, slots=True)
class BoundSetup:
    """A validation setup after a workshop has supplied exact physical facts.

    ``machine_wcs_origin`` and ``machine_wcs_z0_um`` are the controller's
    literal G5x offsets from machine origin; they are not a pre-compensated
    tool-tip origin.  ``machine_wcs_xy_rotation_mdeg`` binds the controller's
    live WCS rotation, not just its translation.  Production CAM v1 accepts
    only zero rotation so the declared identity source-to-WCS transform
    remains true.
    """

    setup_id: str
    stock_id: str
    source_material_id: str
    source_material_version: str
    material_id: str
    material_version: str
    material_evidence_id: str
    material_evidence_version: str
    material_evidence_sha256: str
    sheet_index: int
    side: Side
    source_setup_sha256: str
    source_to_wcs_xy_transform: str
    wcs: str
    machine_wcs_origin: Point2D
    machine_wcs_z0_um: int
    machine_wcs_xy_rotation_mdeg: int
    stock_width_um: int
    stock_height_um: int
    stock_thickness_um: int
    safe_z_um: int
    reference_surface: str
    orientation: str
    fixture_id: str
    fixture_version: str
    fixture_sha256: str
    fixture_clearance_z_um: int
    minimum_rapid_clearance_um: int
    keep_out_policy: str
    probe_method: str
    keep_out_zones: tuple[Rect, ...]
    spoilboard_id: str | None = None
    spoilboard_version: str | None = None
    spoilboard_sha256: str | None = None
    through_cut_allowance_um: int = 0
    raw_allowance_um: int = 0

    def __post_init__(self) -> None:
        for label, identity in (
            ("setup_id", self.setup_id),
            ("stock_id", self.stock_id),
            ("source_material_id", self.source_material_id),
            ("source_material_version", self.source_material_version),
            ("material_id", self.material_id),
            ("material_version", self.material_version),
            ("material_evidence_id", self.material_evidence_id),
            ("material_evidence_version", self.material_evidence_version),
            ("fixture_id", self.fixture_id),
            ("fixture_version", self.fixture_version),
        ):
            _require_identity(label, identity)
        for label, fact in (
            ("material_id", self.material_id),
            ("material_version", self.material_version),
            ("material_evidence_id", self.material_evidence_id),
            ("material_evidence_version", self.material_evidence_version),
        ):
            _require_exact_setup_fact(label, fact)
        _require_hash("material_evidence_sha256", self.material_evidence_sha256)
        _require_hash("fixture_sha256", self.fixture_sha256)
        _require_hash("source_setup_sha256", self.source_setup_sha256)
        _require_int("sheet_index", self.sheet_index, minimum=0)
        if not isinstance(self.side, Side) or self.side == Side.EDGE:
            raise ProductionCAMError("production CAM v1 does not support edge setups")
        if self.source_to_wcs_xy_transform != IDENTITY_SOURCE_TO_WCS_XY:
            raise ProductionCAMError(
                "production CAM v1 requires an explicit identity source-to-WCS XY transform"
            )
        if _WCS_PATTERN.fullmatch(self.wcs) is None:
            raise ProductionCAMError("setup WCS must be a canonical G54-G59 value")
        for label, coordinate in (
            ("machine_wcs_origin.x_um", self.machine_wcs_origin.x_um),
            ("machine_wcs_origin.y_um", self.machine_wcs_origin.y_um),
            ("machine_wcs_z0_um", self.machine_wcs_z0_um),
            ("machine_wcs_xy_rotation_mdeg", self.machine_wcs_xy_rotation_mdeg),
        ):
            _require_int(label, coordinate)
        if self.machine_wcs_xy_rotation_mdeg != 0:
            raise ProductionCAMError("production CAM v1 requires zero WCS XY rotation")
        for label, dimension in (
            ("stock_width_um", self.stock_width_um),
            ("stock_height_um", self.stock_height_um),
            ("stock_thickness_um", self.stock_thickness_um),
            ("safe_z_um", self.safe_z_um),
        ):
            _require_int(label, dimension, minimum=1)
        for label, fact in (
            ("orientation", self.orientation),
            ("probe_method", self.probe_method),
        ):
            _require_exact_setup_fact(label, fact)
        if self.reference_surface != STOCK_TOP_Z0_REFERENCE:
            raise ProductionCAMError("production CAM v1 requires measured stock-top Z0")
        _require_int("fixture_clearance_z_um", self.fixture_clearance_z_um, minimum=0)
        _require_int(
            "minimum_rapid_clearance_um",
            self.minimum_rapid_clearance_um,
            minimum=1,
        )
        if self.fixture_clearance_z_um + self.minimum_rapid_clearance_um > self.safe_z_um:
            raise ProductionCAMError(
                "setup safe Z lacks the accepted minimum rapid clearance above fixtures"
            )
        if self.keep_out_policy != FIXTURE_KEEPOUT_POLICY:
            raise ProductionCAMError("unsupported or ambiguous fixture keep-out policy")
        _require_int("through_cut_allowance_um", self.through_cut_allowance_um, minimum=0)
        if self.through_cut_allowance_um > MAX_THROUGH_OVERTRAVEL_UM:
            raise ProductionCAMError(
                f"setup through-cut allowance cannot exceed {MAX_THROUGH_OVERTRAVEL_UM} um"
            )
        spoilboard_identity = (
            self.spoilboard_id,
            self.spoilboard_version,
            self.spoilboard_sha256,
        )
        if self.through_cut_allowance_um:
            if any(value is None for value in spoilboard_identity):
                raise ProductionCAMError(
                    "positive through-cut allowance requires an exact spoilboard binding"
                )
            assert self.spoilboard_id is not None
            assert self.spoilboard_version is not None
            assert self.spoilboard_sha256 is not None
            _require_identity("spoilboard_id", self.spoilboard_id)
            _require_identity("spoilboard_version", self.spoilboard_version)
            _require_hash("spoilboard_sha256", self.spoilboard_sha256)
        elif any(value is not None for value in spoilboard_identity):
            raise ProductionCAMError("spoilboard binding requires a positive through-cut allowance")
        _require_int("raw_allowance_um", self.raw_allowance_um, minimum=0)
        if self.raw_allowance_um:
            raise ProductionCAMError(
                "production CAM v1 rejects raw allowance; finished origin must be explicit"
            )
        stock = Rect(0, 0, self.stock_width_um, self.stock_height_um)
        if tuple(sorted(self.keep_out_zones, key=_rect_sort_key)) != self.keep_out_zones:
            raise ProductionCAMError("setup keep-out zones must be in canonical order")
        if len(set(self.keep_out_zones)) != len(self.keep_out_zones):
            raise ProductionCAMError("setup keep-out zones must be unique")
        for zone in self.keep_out_zones:
            if min(zone.width_um, zone.height_um) <= 0 or not stock.contains(zone):
                raise ProductionCAMError("setup keep-out zones must be positive and within stock")


@dataclass(frozen=True, slots=True)
class ProductionExecutionContext:
    """Complete immutable workshop binding required to generate candidate motion."""

    source_machine_profile_id: str
    source_machine_profile_version: str
    source_machine_profile_fingerprint: str
    machine_profile_id: str
    machine_profile_version: str
    controller_id: str
    controller_version: str
    machine_x_min_um: int
    machine_x_max_um: int
    machine_y_min_um: int
    machine_y_max_um: int
    machine_z_min_um: int
    machine_z_max_um: int
    work_width_um: int
    work_height_um: int
    work_z_um: int
    min_spindle_rpm: int
    max_spindle_rpm: int
    max_feed_um_min: int
    max_plunge_um_min: int
    tool_catalog_version: str
    recipe_catalog_version: str
    setups: tuple[BoundSetup, ...]
    tool_bindings: tuple[ProductionToolBinding, ...]
    recipes: tuple[CuttingRecipe, ...]

    def __post_init__(self) -> None:
        for label, identity in (
            ("source_machine_profile_id", self.source_machine_profile_id),
            ("source_machine_profile_version", self.source_machine_profile_version),
            ("machine_profile_id", self.machine_profile_id),
            ("machine_profile_version", self.machine_profile_version),
            ("controller_id", self.controller_id),
            ("controller_version", self.controller_version),
            ("tool_catalog_version", self.tool_catalog_version),
            ("recipe_catalog_version", self.recipe_catalog_version),
        ):
            _require_identity(label, identity)
        _require_hash(
            "source_machine_profile_fingerprint",
            self.source_machine_profile_fingerprint,
        )
        for label, coordinate in (
            ("machine_x_min_um", self.machine_x_min_um),
            ("machine_x_max_um", self.machine_x_max_um),
            ("machine_y_min_um", self.machine_y_min_um),
            ("machine_y_max_um", self.machine_y_max_um),
            ("machine_z_min_um", self.machine_z_min_um),
            ("machine_z_max_um", self.machine_z_max_um),
        ):
            _require_int(label, coordinate)
        for label, limit in (
            ("work_width_um", self.work_width_um),
            ("work_height_um", self.work_height_um),
            ("work_z_um", self.work_z_um),
            ("min_spindle_rpm", self.min_spindle_rpm),
            ("max_spindle_rpm", self.max_spindle_rpm),
            ("max_feed_um_min", self.max_feed_um_min),
            ("max_plunge_um_min", self.max_plunge_um_min),
        ):
            _require_int(label, limit, minimum=1)
        if self.min_spindle_rpm > self.max_spindle_rpm:
            raise ProductionCAMError("machine minimum spindle speed exceeds its maximum")
        absolute_bounds = (
            (
                "X",
                self.machine_x_min_um,
                self.machine_x_max_um,
                self.work_width_um,
            ),
            (
                "Y",
                self.machine_y_min_um,
                self.machine_y_max_um,
                self.work_height_um,
            ),
            (
                "Z",
                self.machine_z_min_um,
                self.machine_z_max_um,
                self.work_z_um,
            ),
        )
        for axis, minimum, maximum, declared_travel in absolute_bounds:
            if minimum >= maximum:
                raise ProductionCAMError(f"machine {axis} minimum must be below its maximum")
            if maximum - minimum != declared_travel:
                raise ProductionCAMError(
                    f"machine {axis} absolute bounds differ from declared travel"
                )
        if not self.setups or not self.tool_bindings or not self.recipes:
            raise ProductionCAMError(
                "execution context requires setups, tool bindings and cutting recipes"
            )
        self._require_unique("setup", tuple(setup.setup_id for setup in self.setups))
        self._require_unique(
            "tool binding", tuple(binding.tool_id for binding in self.tool_bindings)
        )
        self._require_unique(
            "source tool binding",
            tuple(binding.source_tool_id for binding in self.tool_bindings),
        )
        self._require_unique(
            "controller tool number",
            tuple(binding.controller_tool_number for binding in self.tool_bindings),
        )
        self._require_unique(
            "tool length-offset number",
            tuple(binding.length_offset_number for binding in self.tool_bindings),
        )
        tool_table_evidence = {
            (
                binding.tool_table_evidence_id,
                binding.tool_table_evidence_version,
                binding.tool_table_evidence_sha256,
            )
            for binding in self.tool_bindings
        }
        if len(tool_table_evidence) != 1:
            raise ProductionCAMError(
                "tool bindings must share one atomic tool-table evidence snapshot"
            )
        self._require_unique("recipe identity", tuple(recipe.recipe_id for recipe in self.recipes))
        recipe_keys = tuple(
            (
                recipe.material_id,
                recipe.material_version,
                recipe.tool_id,
                recipe.tool_version,
                recipe.operation_kind,
            )
            for recipe in self.recipes
        )
        self._require_unique("recipe binding", recipe_keys)
        if tuple(sorted(self.setups, key=lambda item: item.setup_id)) != self.setups:
            raise ProductionCAMError("setups must be in canonical setup_id order")
        if tuple(sorted(self.tool_bindings, key=_tool_binding_sort_key)) != self.tool_bindings:
            raise ProductionCAMError("tool bindings must be in canonical source-tool order")
        if tuple(sorted(self.recipes, key=_recipe_sort_key)) != self.recipes:
            raise ProductionCAMError("cutting recipes must be in canonical binding order")
        for setup in self.setups:
            if (
                setup.machine_wcs_origin.x_um < self.machine_x_min_um
                or setup.machine_wcs_origin.x_um + setup.stock_width_um > self.machine_x_max_um
                or setup.machine_wcs_origin.y_um < self.machine_y_min_um
                or setup.machine_wcs_origin.y_um + setup.stock_height_um > self.machine_y_max_um
            ):
                raise ProductionCAMError(
                    f"setup WCS placement exceeds machine XY travel: {setup.setup_id}"
                )
        physical_sheet_materials: dict[
            tuple[str, int], tuple[str, str, str, str, str, str, str]
        ] = {}
        for setup in self.setups:
            physical_sheet = (setup.stock_id, setup.sheet_index)
            material_binding = (
                setup.source_material_id,
                setup.source_material_version,
                setup.material_id,
                setup.material_version,
                setup.material_evidence_id,
                setup.material_evidence_version,
                setup.material_evidence_sha256,
            )
            previous = physical_sheet_materials.setdefault(physical_sheet, material_binding)
            if previous != material_binding:
                raise ProductionCAMError(
                    "setups for one physical sheet disagree on source or actual material: "
                    f"{setup.stock_id}:{setup.sheet_index}"
                )
        bindings_by_id = {
            (binding.tool_id, binding.tool_version): binding for binding in self.tool_bindings
        }
        binding_ids = set(bindings_by_id)
        for recipe in self.recipes:
            if (
                recipe.machine_profile_id != self.machine_profile_id
                or recipe.machine_profile_version != self.machine_profile_version
            ):
                raise ProductionCAMError(f"recipe machine binding mismatch: {recipe.recipe_id}")
            if (recipe.tool_id, recipe.tool_version) not in binding_ids:
                raise ProductionCAMError(f"recipe references an unbound tool: {recipe.recipe_id}")
            if not self.min_spindle_rpm <= recipe.spindle_rpm <= self.max_spindle_rpm:
                raise ProductionCAMError(
                    f"recipe is outside machine spindle limits: {recipe.recipe_id}"
                )
            if recipe.feed_um_min > self.max_feed_um_min:
                raise ProductionCAMError(f"recipe exceeds machine feed limit: {recipe.recipe_id}")
            if recipe.plunge_um_min > self.max_plunge_um_min:
                raise ProductionCAMError(f"recipe exceeds machine plunge limit: {recipe.recipe_id}")
            if recipe.operation_kind in {OperationKind.POCKET, OperationKind.GROOVE}:
                binding = bindings_by_id[(recipe.tool_id, recipe.tool_version)]
                stepover_um = binding.effective_diameter_um * recipe.stepover_ppm // 1_000_000
                if stepover_um + 2 * recipe.process_accuracy_um > binding.effective_diameter_um:
                    raise ProductionCAMError(
                        "recipe stepover exceeds process-accuracy-adjusted cutter coverage: "
                        f"{recipe.recipe_id}"
                    )

    @staticmethod
    def _require_unique(label: str, values: tuple[object, ...]) -> None:
        if len(values) != len(set(values)):
            raise ProductionCAMError(f"duplicate {label}")

    @property
    def machine_profile_fingerprint(self) -> str:
        return sha256_hex(
            canonical_json_bytes(
                {
                    "controller_id": self.controller_id,
                    "controller_version": self.controller_version,
                    "machine_profile_id": self.machine_profile_id,
                    "machine_profile_version": self.machine_profile_version,
                    "machine_x_max_um": self.machine_x_max_um,
                    "machine_x_min_um": self.machine_x_min_um,
                    "machine_y_max_um": self.machine_y_max_um,
                    "machine_y_min_um": self.machine_y_min_um,
                    "machine_z_max_um": self.machine_z_max_um,
                    "machine_z_min_um": self.machine_z_min_um,
                    "max_spindle_rpm": self.max_spindle_rpm,
                    "min_spindle_rpm": self.min_spindle_rpm,
                    "max_feed_um_min": self.max_feed_um_min,
                    "max_plunge_um_min": self.max_plunge_um_min,
                    "work_height_um": self.work_height_um,
                    "work_width_um": self.work_width_um,
                    "work_z_um": self.work_z_um,
                }
            )
        )

    @property
    def tool_catalog_fingerprint(self) -> str:
        return sha256_hex(
            canonical_json_bytes(
                {
                    "tool_catalog_version": self.tool_catalog_version,
                    "tool_bindings": self.tool_bindings,
                }
            )
        )

    @property
    def recipe_catalog_fingerprint(self) -> str:
        return sha256_hex(
            canonical_json_bytes(
                {
                    "recipe_catalog_version": self.recipe_catalog_version,
                    "recipes": self.recipes,
                }
            )
        )

    @property
    def fingerprint(self) -> str:
        return sha256_hex(canonical_json_bytes(self))


def _recipe_sort_key(recipe: CuttingRecipe) -> tuple[str, str, str, str, str]:
    return (
        recipe.material_id,
        recipe.material_version,
        recipe.tool_id,
        recipe.tool_version,
        recipe.operation_kind.value,
    )


def _tool_binding_sort_key(binding: ProductionToolBinding) -> tuple[str, str]:
    return (binding.source_tool_id, binding.tool_id)


def _rect_sort_key(rectangle: Rect) -> tuple[int, int, int, int]:
    return (
        rectangle.y_um,
        rectangle.x_um,
        rectangle.height_um,
        rectangle.width_um,
    )


@dataclass(frozen=True, slots=True)
class ProductionMove:
    sequence: int
    operation_id: str
    pass_index: int
    kind: ProductionMoveKind
    role: ProductionMoveRole
    x_um: int
    y_um: int
    z_um: int
    feed_um_min: int | None = None

    def __post_init__(self) -> None:
        _require_int("move sequence", self.sequence, minimum=1)
        _require_identity("move operation_id", self.operation_id)
        _require_int("move pass_index", self.pass_index, minimum=1)
        for label, value in (("x_um", self.x_um), ("y_um", self.y_um), ("z_um", self.z_um)):
            _require_int(label, value)
        if self.kind == ProductionMoveKind.RAPID:
            if self.feed_um_min is not None:
                raise ProductionCAMError("rapid moves cannot carry a cutting feed")
            if self.role not in {
                ProductionMoveRole.POSITION,
                ProductionMoveRole.APPROACH,
                ProductionMoveRole.PECK_RETRACT,
                ProductionMoveRole.RETRACT,
            }:
                raise ProductionCAMError("rapid move has a cutting-only role")
        else:
            if self.feed_um_min is None:
                raise ProductionCAMError("linear moves require an explicit feed")
            _require_int("feed_um_min", self.feed_um_min, minimum=1)
            if self.role in {ProductionMoveRole.POSITION, ProductionMoveRole.RETRACT}:
                raise ProductionCAMError("linear move has a rapid-only role")


@dataclass(frozen=True, slots=True)
class ProductionProgram:
    program_id: str
    run_order: int
    setup_id: str
    tool_id: str
    tool_version: str
    recipe_ids: tuple[str, ...]
    operation_ids: tuple[str, ...]
    release_operation_ids: tuple[str, ...]
    moves: tuple[ProductionMove, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("program_id", self.program_id),
            ("setup_id", self.setup_id),
            ("tool_id", self.tool_id),
            ("tool_version", self.tool_version),
        ):
            _require_identity(label, value)
        _require_int("run_order", self.run_order, minimum=1)
        if not self.recipe_ids or len(set(self.recipe_ids)) != len(self.recipe_ids):
            raise ProductionCAMError("program recipe IDs must be non-empty and unique")
        if not self.operation_ids or len(set(self.operation_ids)) != len(self.operation_ids):
            raise ProductionCAMError("program operation IDs must be non-empty and unique")
        for recipe_id in self.recipe_ids:
            _require_identity("program recipe_id", recipe_id)
        for operation_id in self.operation_ids:
            _require_identity("program operation_id", operation_id)
        for operation_id in self.release_operation_ids:
            _require_identity("program release operation_id", operation_id)
        if not set(self.release_operation_ids) <= set(self.operation_ids):
            raise ProductionCAMError("release operation IDs must belong to the program")
        if self.release_operation_ids and self.operation_ids[
            -len(self.release_operation_ids) :
        ] != (self.release_operation_ids):
            raise ProductionCAMError("release contours must be the final program operations")
        if not self.moves:
            raise ProductionCAMError("production program cannot be empty")
        if tuple(move.sequence for move in self.moves) != tuple(range(1, len(self.moves) + 1)):
            raise ProductionCAMError("program move sequences must be dense and start at one")
        if {move.operation_id for move in self.moves} != set(self.operation_ids):
            raise ProductionCAMError("program moves must exactly cover every declared operation")


@dataclass(frozen=True, slots=True)
class ProductionToolpathDocument:
    design_hash: str
    operations_sha256: str
    execution_context: ProductionExecutionContext
    machine_profile_fingerprint: str
    tool_catalog_fingerprint: str
    recipe_catalog_fingerprint: str
    programs: tuple[ProductionProgram, ...]
    schema_version: str = PRODUCTION_TOOLPATH_SCHEMA_VERSION
    engine_version: str = PRODUCTION_TOOLPATH_ENGINE_VERSION
    mode: str = EXECUTABLE_CAM_CANDIDATE_MODE
    physical_cutting_authorized: bool = False
    workshop_acceptance_required: bool = True

    def __post_init__(self) -> None:
        for label, value in (
            ("design_hash", self.design_hash),
            ("operations_sha256", self.operations_sha256),
            ("machine_profile_fingerprint", self.machine_profile_fingerprint),
            ("tool_catalog_fingerprint", self.tool_catalog_fingerprint),
            ("recipe_catalog_fingerprint", self.recipe_catalog_fingerprint),
        ):
            _require_hash(label, value)
        if self.schema_version != PRODUCTION_TOOLPATH_SCHEMA_VERSION:
            raise ProductionCAMError("unsupported production toolpath schema")
        if self.engine_version != PRODUCTION_TOOLPATH_ENGINE_VERSION:
            raise ProductionCAMError("unsupported production toolpath engine")
        if self.mode != EXECUTABLE_CAM_CANDIDATE_MODE:
            raise ProductionCAMError("production toolpaths must remain an executable candidate")
        if self.physical_cutting_authorized is not False:
            raise ProductionCAMError("CAM generation cannot authorize physical cutting")
        if self.workshop_acceptance_required is not True:
            raise ProductionCAMError("workshop acceptance cannot be bypassed")
        if not self.programs:
            raise ProductionCAMError("production toolpath document requires programs")
        if tuple(program.run_order for program in self.programs) != tuple(
            range(1, len(self.programs) + 1)
        ):
            raise ProductionCAMError("program run order must be dense and start at one")
        if len({program.program_id for program in self.programs}) != len(self.programs):
            raise ProductionCAMError("duplicate production program ID")
        operation_ids = tuple(
            operation_id for program in self.programs for operation_id in program.operation_ids
        )
        if len(operation_ids) != len(set(operation_ids)):
            raise ProductionCAMError("an operation cannot belong to multiple programs")
        self._validate_program_bindings_and_release_order()
        if self.machine_profile_fingerprint != self.execution_context.machine_profile_fingerprint:
            raise ProductionCAMError("machine-profile fingerprint mismatch")
        if self.tool_catalog_fingerprint != self.execution_context.tool_catalog_fingerprint:
            raise ProductionCAMError("tool-catalog fingerprint mismatch")
        if self.recipe_catalog_fingerprint != self.execution_context.recipe_catalog_fingerprint:
            raise ProductionCAMError("recipe-catalog fingerprint mismatch")

    def _validate_program_bindings_and_release_order(self) -> None:
        setup_by_id = {setup.setup_id: setup for setup in self.execution_context.setups}
        tool_by_id = {binding.tool_id: binding for binding in self.execution_context.tool_bindings}
        recipe_by_id = {recipe.recipe_id: recipe for recipe in self.execution_context.recipes}
        released_sheets: set[tuple[str, int]] = set()
        for program in self.programs:
            setup = setup_by_id.get(program.setup_id)
            tool = tool_by_id.get(program.tool_id)
            if setup is None or tool is None:
                raise ProductionCAMError(
                    f"program references an unknown setup or tool: {program.program_id}"
                )
            if program.tool_version != tool.tool_version:
                raise ProductionCAMError(
                    f"program tool version differs from its binding: {program.program_id}"
                )
            recipes = tuple(recipe_by_id.get(recipe_id) for recipe_id in program.recipe_ids)
            if any(recipe is None for recipe in recipes):
                raise ProductionCAMError(
                    f"program references an unknown recipe: {program.program_id}"
                )
            if any(
                recipe is not None
                and (
                    recipe.tool_id != program.tool_id
                    or recipe.tool_version != program.tool_version
                    or recipe.material_id != setup.material_id
                    or recipe.material_version != setup.material_version
                )
                for recipe in recipes
            ):
                raise ProductionCAMError(
                    f"program recipe binding differs from setup/tool: {program.program_id}"
                )
            physical_sheet = (setup.stock_id, setup.sheet_index)
            if physical_sheet in released_sheets:
                raise ProductionCAMError(
                    "a program follows release contour on physical sheet "
                    f"{setup.stock_id}:{setup.sheet_index}"
                )
            if program.release_operation_ids:
                released_sheets.add(physical_sheet)

    @property
    def fingerprint(self) -> str:
        return sha256_hex(self.to_json())

    def as_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], canonical_data(self))

    def to_json(self) -> bytes:
        return canonical_json_bytes(self)
