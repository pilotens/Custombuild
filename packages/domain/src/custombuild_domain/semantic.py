"""Semantic furniture editing boundary for drag/drop and AI-assisted intent.

The semantic layer deliberately operates above CAD and CAM.  It accepts only
furniture-domain commands (for example, "add a shelf row") and compiles them to
the existing validated :class:`BookcaseDesignSpec`.  It never accepts or emits
free-form CNC coordinates.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, ValidationError, model_validator

from .enums import BackPanelType, ReinforcementMode
from .materials import screening_birch_plywood_6, screening_mdf_6
from .models import (
    BookcaseDesignSpec,
    BookcaseParameters,
    FrozenModel,
    MaterialVersion,
    StableKey,
)
from .units import mm

SEMANTIC_DESIGN_VERSION = "semantic-design-1.2.0"
DEFAULT_PLINTH_HEIGHT_UM = mm(80)

Permille = Annotated[int, Field(strict=True, ge=0, le=1_000)]
NonNegativeUm = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]


class SemanticComponentKind(StrEnum):
    SHELF_ROW = "shelf_row"
    DIVIDER = "divider"
    BACK_PANEL = "back_panel"
    PLINTH = "plinth"
    BASE_CABINET = "base_cabinet"


class SemanticAction(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    MOVE = "move"


class SemanticIntentSource(StrEnum):
    USER_DRAG = "user_drag"
    USER_COMMAND = "user_command"
    AI_PROPOSAL = "ai_proposal"
    TEMPLATE = "template"


class SemanticSnapRelation(StrEnum):
    SHELF_IN_BAY = "shelf_in_bay"
    DIVIDER_IN_CARCASS = "divider_in_carcass"
    BACK_BEHIND_CARCASS = "back_behind_carcass"
    PLINTH_UNDER_CARCASS = "plinth_under_carcass"
    CABINET_UNDER_SHELVES = "cabinet_under_shelves"


class SemanticPlacementPolicy(StrEnum):
    """How the current production template resolves a semantic placement."""

    PARAMETRIC_EQUAL_DISTRIBUTION = "parametric_equal_distribution_v1"
    CONSTRAINED_CUSTOM_LAYOUT = "constrained_custom_layout_v1"


class SemanticCompilationError(ValueError):
    """Raised when a semantic command cannot compile to a valid production spec."""


class SemanticIntentApprovalRequired(SemanticCompilationError):
    """Raised when an AI-originated proposal has not been confirmed by a user."""


class UnsupportedSemanticOperation(SemanticCompilationError):
    """Raised when the current template cannot represent the requested operation."""


def _screened_back_material_for(spec: BookcaseDesignSpec) -> MaterialVersion:
    """Choose only the bounded back-material pair supported by the current MVP."""

    if spec.material.material_id == "birch-plywood":
        return screening_birch_plywood_6()
    if spec.material.material_id == "mdf":
        return screening_mdf_6()
    raise SemanticCompilationError(
        "adding a back panel requires an explicit supported back material"
    )


class SemanticBounds(FrozenModel):
    min_x_um: NonNegativeUm
    max_x_um: NonNegativeUm
    min_y_um: NonNegativeUm
    max_y_um: NonNegativeUm
    min_z_um: NonNegativeUm
    max_z_um: NonNegativeUm

    @model_validator(mode="after")
    def bounds_are_ordered(self) -> SemanticBounds:
        if (
            self.min_x_um > self.max_x_um
            or self.min_y_um > self.max_y_um
            or self.min_z_um > self.max_z_um
        ):
            raise ValueError("semantic bounds are inverted")
        return self


class SemanticSnapTarget(FrozenModel):
    target_id: StableKey
    label: str = Field(min_length=1, max_length=160)
    relation: SemanticSnapRelation
    accepted_components: tuple[SemanticComponentKind, ...] = Field(min_length=1)
    bounds: SemanticBounds
    bay_index: PositiveInt | None = None


class SemanticComponent(FrozenModel):
    component_id: StableKey
    kind: SemanticComponentKind
    label: str = Field(min_length=1, max_length=160)
    count: int = Field(strict=True, ge=0)
    editable: bool = True


class SemanticDesignDocument(FrozenModel):
    schema_version: StableKey = SEMANTIC_DESIGN_VERSION
    design_id: StableKey
    revision: PositiveInt
    template_id: StableKey
    components: tuple[SemanticComponent, ...]
    snap_targets: tuple[SemanticSnapTarget, ...]
    placement_policy: SemanticPlacementPolicy = SemanticPlacementPolicy.CONSTRAINED_CUSTOM_LAYOUT
    exact_component_move_supported: bool = False


class SemanticIntent(FrozenModel):
    intent_id: StableKey
    action: SemanticAction
    component_kind: SemanticComponentKind
    target_id: StableKey
    source: SemanticIntentSource
    pointer_x_permille: Permille = 500
    pointer_y_permille: Permille = 500
    rationale: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def ai_proposals_include_rationale(self) -> SemanticIntent:
        if self.source == SemanticIntentSource.AI_PROPOSAL and not self.rationale:
            raise ValueError("AI proposals require an attributed rationale")
        return self


class SemanticChange(FrozenModel):
    field: StableKey
    before: int | str | bool
    after: int | str | bool
    reason: str = Field(min_length=1, max_length=500)
    target_id: StableKey


class SemanticCompilationResult(FrozenModel):
    semantic_version: StableKey = SEMANTIC_DESIGN_VERSION
    spec: BookcaseDesignSpec
    intent: SemanticIntent
    target: SemanticSnapTarget
    changes: tuple[SemanticChange, ...]
    placement_policy: SemanticPlacementPolicy = SemanticPlacementPolicy.CONSTRAINED_CUSTOM_LAYOUT
    warnings: tuple[str, ...] = ()
    requires_engine_rebuild: bool = True


def _equal_ratios(count: int) -> tuple[int, ...]:
    base, remainder = divmod(1_000_000, count)
    return tuple(base + (1 if index < remainder else 0) for index in range(count))


def _bay_ratios(parameters: BookcaseParameters) -> tuple[int, ...]:
    count = parameters.vertical_divider_count + 1
    return parameters.bay_width_ratios_ppm or _equal_ratios(count)


def _shelf_ratios(parameters: BookcaseParameters) -> tuple[int, ...]:
    if parameters.shelf_height_ratios_ppm:
        return parameters.shelf_height_ratios_ppm
    return tuple(
        ((index + 1) * 1_000_000) // (parameters.shelf_count + 1)
        for index in range(parameters.shelf_count)
    )


def _boundaries(ratios: tuple[int, ...]) -> tuple[int, ...]:
    result = [0]
    for ratio in ratios:
        result.append(result[-1] + ratio)
    result[-1] = 1_000_000
    return tuple(result)


def _ratios_from_boundaries(boundaries: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(
        boundaries[index + 1] - boundaries[index]
        for index in range(len(boundaries) - 1)
    )


def _insert_constrained_position(
    existing: tuple[int, ...], requested: int, minimum_gap: int
) -> tuple[int, ...]:
    boundaries = (0, *existing, 1_000_000)
    for index in range(len(boundaries) - 1):
        left, right = boundaries[index], boundaries[index + 1]
        if requested <= right:
            if right - left < minimum_gap * 2:
                break
            value = min(right - minimum_gap, max(left + minimum_gap, requested))
            if all(abs(value - current) >= minimum_gap for current in existing):
                return tuple(sorted((*existing, value)))
            break
    candidates = [
        (right - left, (left + right) // 2)
        for left, right in zip(boundaries, boundaries[1:], strict=False)
        if right - left >= minimum_gap * 2
    ]
    if not candidates:
        raise UnsupportedSemanticOperation("no supported opening remains for this component")
    return tuple(sorted((*existing, max(candidates)[1])))


def _remove_nearest_position(existing: tuple[int, ...], requested: int) -> tuple[int, ...]:
    if not existing:
        raise SemanticCompilationError("semantic intent does not change the current design")
    nearest = min(range(len(existing)), key=lambda index: abs(existing[index] - requested))
    return tuple(value for index, value in enumerate(existing) if index != nearest)


def derive_bookcase_snap_targets(spec: BookcaseDesignSpec) -> tuple[SemanticSnapTarget, ...]:
    """Return deterministic furniture-aware snap zones for the current spec."""

    parameters = spec.parameters
    thickness = parameters.actual_thickness_um
    bay_count = parameters.vertical_divider_count + 1
    inner_width = (
        parameters.width_um
        - 2 * thickness
        - parameters.vertical_divider_count * thickness
    )
    ratios = _bay_ratios(parameters)
    bay_widths = [inner_width * ratio // 1_000_000 for ratio in ratios]
    distributed = sum(bay_widths)
    for index in range(inner_width - distributed):
        bay_widths[index % bay_count] += 1
    usable_z_min = (
        parameters.plinth_height_um + parameters.base_cabinet_height_um
        if parameters.base_cabinet_count
        else parameters.plinth_height_um + thickness
    )
    usable_z_max = parameters.height_um - thickness
    targets: list[SemanticSnapTarget] = []
    cursor_x = thickness

    for bay_offset in range(bay_count):
        bay_width = bay_widths[bay_offset]
        bay_number = bay_offset + 1
        targets.append(
            SemanticSnapTarget(
                target_id=f"bookcase:shelf:bay:{bay_number}",
                label=f"Hyllfack {bay_number}",
                relation=SemanticSnapRelation.SHELF_IN_BAY,
                accepted_components=(SemanticComponentKind.SHELF_ROW,),
                bounds=SemanticBounds(
                    min_x_um=cursor_x,
                    max_x_um=cursor_x + bay_width,
                    min_y_um=0,
                    max_y_um=parameters.depth_um,
                    min_z_um=usable_z_min,
                    max_z_um=usable_z_max,
                ),
                bay_index=bay_number,
            )
        )
        cursor_x += bay_width
        if bay_offset < parameters.vertical_divider_count:
            cursor_x += thickness

    targets.extend(
        (
            SemanticSnapTarget(
                target_id="bookcase:divider:carcass",
                label="Bokhyllans stomme",
                relation=SemanticSnapRelation.DIVIDER_IN_CARCASS,
                accepted_components=(SemanticComponentKind.DIVIDER,),
                bounds=SemanticBounds(
                    min_x_um=thickness,
                    max_x_um=parameters.width_um - thickness,
                    min_y_um=0,
                    max_y_um=parameters.depth_um,
                    min_z_um=usable_z_min,
                    max_z_um=usable_z_max,
                ),
            ),
            SemanticSnapTarget(
                target_id="bookcase:back:rear",
                label="Stommens baksida",
                relation=SemanticSnapRelation.BACK_BEHIND_CARCASS,
                accepted_components=(SemanticComponentKind.BACK_PANEL,),
                bounds=SemanticBounds(
                    min_x_um=0,
                    max_x_um=parameters.width_um,
                    min_y_um=0,
                    max_y_um=parameters.back_thickness_um,
                    min_z_um=0,
                    max_z_um=parameters.height_um,
                ),
            ),
            SemanticSnapTarget(
                target_id="bookcase:plinth:base",
                label="Stommens undersida",
                relation=SemanticSnapRelation.PLINTH_UNDER_CARCASS,
                accepted_components=(SemanticComponentKind.PLINTH,),
                bounds=SemanticBounds(
                    min_x_um=thickness,
                    max_x_um=parameters.width_um - thickness,
                    min_y_um=0,
                    max_y_um=parameters.depth_um,
                    min_z_um=0,
                    max_z_um=max(parameters.plinth_height_um, DEFAULT_PLINTH_HEIGHT_UM),
                ),
            ),
            SemanticSnapTarget(
                target_id="bookcase:cabinet:lower-zone",
                label="Nedre förvaringszon",
                relation=SemanticSnapRelation.CABINET_UNDER_SHELVES,
                accepted_components=(SemanticComponentKind.BASE_CABINET,),
                bounds=SemanticBounds(
                    min_x_um=thickness,
                    max_x_um=parameters.width_um - thickness,
                    min_y_um=0,
                    max_y_um=parameters.depth_um,
                    min_z_um=parameters.plinth_height_um,
                    max_z_um=max(parameters.base_cabinet_height_um, mm(680)),
                ),
            ),
        )
    )
    return tuple(targets)


def semantic_document_from_bookcase(spec: BookcaseDesignSpec) -> SemanticDesignDocument:
    """Project the production spec into the UI-facing semantic document."""

    parameters = spec.parameters
    components = (
        SemanticComponent(
            component_id="component:shelf-rows",
            kind=SemanticComponentKind.SHELF_ROW,
            label="Hyllrader",
            count=parameters.shelf_count,
        ),
        SemanticComponent(
            component_id="component:dividers",
            kind=SemanticComponentKind.DIVIDER,
            label="Vertikala avdelare",
            count=parameters.vertical_divider_count,
        ),
        SemanticComponent(
            component_id="component:back-panel",
            kind=SemanticComponentKind.BACK_PANEL,
            label="Bakstycke",
            count=int(parameters.back_panel != BackPanelType.NONE),
        ),
        SemanticComponent(
            component_id="component:plinth",
            kind=SemanticComponentKind.PLINTH,
            label="Sockel",
            count=int(parameters.plinth_height_um > 0),
        ),
        SemanticComponent(
            component_id="component:base-cabinets",
            kind=SemanticComponentKind.BASE_CABINET,
            label="Underskåp",
            count=parameters.base_cabinet_count,
        ),
    )
    return SemanticDesignDocument(
        design_id=spec.design_id,
        revision=spec.revision,
        template_id=spec.template_id,
        components=components,
        snap_targets=derive_bookcase_snap_targets(spec),
    )


def compile_semantic_intent(
    spec: BookcaseDesignSpec,
    intent: SemanticIntent,
    *,
    user_confirmed: bool = False,
) -> SemanticCompilationResult:
    """Compile one semantic intent into a validated ``BookcaseDesignSpec``.

    AI output is treated only as a proposal.  The caller must explicitly set
    ``user_confirmed=True`` before an AI-originated command may mutate the design.
    Exact coordinates remain the responsibility of the deterministic domain
    engine after this function returns.
    """

    if intent.source == SemanticIntentSource.AI_PROPOSAL and not user_confirmed:
        raise SemanticIntentApprovalRequired(
            "AI-originated furniture intent requires explicit user confirmation"
        )
    if intent.action == SemanticAction.MOVE:
        raise UnsupportedSemanticOperation(
            "MOVE requires a stable component-instance target; use the versioned shelf/divider "
            "position command instead of a free semantic transform"
        )

    targets = derive_bookcase_snap_targets(spec)
    target = next((item for item in targets if item.target_id == intent.target_id), None)
    if target is None:
        raise SemanticCompilationError(f"unknown semantic snap target: {intent.target_id}")
    if intent.component_kind not in target.accepted_components:
        raise SemanticCompilationError(
            f"{intent.component_kind.value} cannot snap to {target.relation.value}"
        )

    parameters = spec.parameters
    updates: dict[str, object] = {}
    changes: list[SemanticChange] = []
    warnings: list[str] = []
    adding = intent.action == SemanticAction.ADD

    if intent.component_kind == SemanticComponentKind.SHELF_ROW:
        shelf_before = parameters.shelf_count
        before_ratios = _shelf_ratios(parameters)
        requested = (1_000 - intent.pointer_y_permille) * 1_000
        try:
            after_ratios = (
                _insert_constrained_position(before_ratios, requested, 50_000)
                if adding
                else _remove_nearest_position(before_ratios, requested)
            )
        except UnsupportedSemanticOperation as exc:
            raise SemanticCompilationError(
                f"semantic intent would create an invalid bookcase: {exc}"
            ) from exc
        shelf_after = len(after_ratios)
        updates.update(
            {
                "shelf_count": shelf_after,
                "shelf_height_ratios_ppm": after_ratios,
            }
        )
        changes.append(
            SemanticChange(
                field="shelf_count",
                before=shelf_before,
                after=shelf_after,
                reason=(
                    f"Hyllrad {'lades till i' if adding else 'togs bort från'} "
                    f"{target.label.lower()}"
                ),
                target_id=target.target_id,
            )
        )
        warnings.append(
            "The pointer height was compiled to a constrained shelf ratio; the geometry engine "
            "still derives every final part and joint coordinate, so it is not a direct CNC "
            "coordinate."
        )
    elif intent.component_kind == SemanticComponentKind.DIVIDER:
        divider_before = parameters.vertical_divider_count
        before_positions = _boundaries(_bay_ratios(parameters))[1:-1]
        requested = intent.pointer_x_permille * 1_000
        after_positions = (
            _insert_constrained_position(before_positions, requested, 80_000)
            if adding
            else _remove_nearest_position(before_positions, requested)
        )
        divider_after = len(after_positions)
        after_boundaries = (0, *after_positions, 1_000_000)
        updates.update(
            {
                "vertical_divider_count": divider_after,
                "bay_width_ratios_ppm": _ratios_from_boundaries(after_boundaries),
                "reinforcement_mode": ReinforcementMode.MANUAL,
            }
        )
        changes.extend(
            (
                SemanticChange(
                    field="vertical_divider_count",
                    before=divider_before,
                    after=divider_after,
                    reason=(
                        "Vertikal avdelare lades till via semantisk snapping"
                        if adding
                        else "Vertikal avdelare togs bort via semantisk redigering"
                    ),
                    target_id=target.target_id,
                ),
                SemanticChange(
                    field="reinforcement_mode",
                    before=parameters.reinforcement_mode.value,
                    after=ReinforcementMode.MANUAL.value,
                    reason="En användarstyrd strukturändring ska granskas som manuell.",
                    target_id=target.target_id,
                ),
            )
        )
        warnings.append(
            "The pointer width was compiled to constrained bay ratios; production coordinates "
            "are regenerated by the geometry engine."
        )
    elif intent.component_kind == SemanticComponentKind.BACK_PANEL:
        back_before = parameters.back_panel
        back_after = BackPanelType.INSET_GROOVE if adding else BackPanelType.NONE
        updates["back_panel"] = back_after
        changes.append(
            SemanticChange(
                field="back_panel",
                before=back_before.value,
                after=back_after.value,
                reason="Bakstyckets semantiska närvaro ändrades.",
                target_id=target.target_id,
            )
        )
    elif intent.component_kind == SemanticComponentKind.PLINTH:
        plinth_before = parameters.plinth_height_um
        plinth_after = DEFAULT_PLINTH_HEIGHT_UM if adding else 0
        updates["plinth_height_um"] = plinth_after
        changes.append(
            SemanticChange(
                field="plinth_height_um",
                before=plinth_before,
                after=plinth_after,
                reason="Sockelns semantiska närvaro ändrades.",
                target_id=target.target_id,
            )
        )
    elif intent.component_kind == SemanticComponentKind.BASE_CABINET:
        cabinet_before = parameters.base_cabinet_count
        cabinet_after = parameters.vertical_divider_count + 1 if adding else 0
        updates.update(
            {
                "base_cabinet_count": cabinet_after,
                "base_cabinet_height_um": mm(680) if adding else 0,
                "base_cabinet_depth_um": parameters.depth_um if adding else 0,
            }
        )
        changes.append(
            SemanticChange(
                field="base_cabinet_count",
                before=cabinet_before,
                after=cabinet_after,
                reason="Underskåpsraden kopplades till den bärande fackindelningen.",
                target_id=target.target_id,
            )
        )
    else:  # pragma: no cover - exhaustive enum defence
        raise UnsupportedSemanticOperation(intent.component_kind.value)

    if not changes or all(change.before == change.after for change in changes):
        raise SemanticCompilationError("semantic intent does not change the current design")

    try:
        parameter_payload = parameters.model_dump(mode="python")
        parameter_payload.update(updates)
        updated_parameters = BookcaseParameters.model_validate(parameter_payload)
        spec_payload = spec.model_dump(mode="python")
        spec_payload["parameters"] = updated_parameters
        if intent.component_kind == SemanticComponentKind.BACK_PANEL:
            if adding:
                spec_payload["back_material"] = _screened_back_material_for(spec)
                warnings.append(
                    "A versioned 6 mm screening back material was selected deterministically; "
                    "supplier batch evidence remains required before physical production."
                )
            else:
                spec_payload["back_material"] = None
        if spec.joint_retention is not None:
            spec_payload["joint_retention"] = None
            warnings.append(
                "The geometry change invalidated the previous joint-retention binding; "
                "a new exact contract is required before CAM can be released."
            )
        updated_spec = BookcaseDesignSpec.model_validate(spec_payload)
    except ValidationError as exc:
        raise SemanticCompilationError(
            f"semantic intent would create an invalid bookcase: {exc.errors()[0]['msg']}"
        ) from exc

    if intent.source == SemanticIntentSource.AI_PROPOSAL:
        warnings.append(
            "AI supplied only the furniture intent; all dimensions and manufacturing features "
            "must be regenerated and validated deterministically."
        )

    return SemanticCompilationResult(
        spec=updated_spec,
        intent=intent,
        target=target,
        changes=tuple(changes),
        warnings=tuple(warnings),
    )
