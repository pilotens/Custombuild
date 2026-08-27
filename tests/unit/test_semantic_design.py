from __future__ import annotations

import custombuild_domain as domain
import pytest
from pydantic import ValidationError


def _spec(parameters: domain.BookcaseParameters | None = None) -> domain.BookcaseDesignSpec:
    effective_parameters = parameters or domain.BookcaseParameters()
    return domain.BookcaseDesignSpec(
        design_id="semantic-test",
        parameters=effective_parameters,
        material=domain.screening_birch_plywood_18(),
        back_material=(
            None
            if effective_parameters.back_panel == domain.BackPanelType.NONE
            else domain.screening_birch_plywood_6()
        ),
    )


def _intent(
    component: domain.SemanticComponentKind,
    target_id: str,
    *,
    action: domain.SemanticAction = domain.SemanticAction.ADD,
    source: domain.SemanticIntentSource = domain.SemanticIntentSource.USER_DRAG,
    rationale: str | None = None,
    pointer_x_permille: int = 500,
    pointer_y_permille: int = 500,
) -> domain.SemanticIntent:
    return domain.SemanticIntent(
        intent_id=f"intent-{action.value}-{component.value}",
        action=action,
        component_kind=component,
        target_id=target_id,
        source=source,
        rationale=rationale,
        pointer_x_permille=pointer_x_permille,
        pointer_y_permille=pointer_y_permille,
    )


def test_semantic_document_exposes_furniture_aware_snap_targets() -> None:
    document = domain.semantic_document_from_bookcase(_spec())

    assert document.schema_version == "semantic-design-1.2.0"
    shelf_targets = [
        target
        for target in document.snap_targets
        if domain.SemanticComponentKind.SHELF_ROW in target.accepted_components
    ]
    assert len(shelf_targets) == 1
    assert shelf_targets[0].bay_index == 1
    assert document.exact_component_move_supported is False


def test_multiple_bays_have_ordered_non_overlapping_snap_bounds() -> None:
    spec = _spec(domain.BookcaseParameters(vertical_divider_count=2))
    targets = domain.derive_bookcase_snap_targets(spec)
    shelves = [target for target in targets if target.bay_index is not None]

    assert [target.bay_index for target in shelves] == [1, 2, 3]
    assert shelves[0].bounds.max_x_um < shelves[1].bounds.min_x_um
    assert shelves[1].bounds.max_x_um < shelves[2].bounds.min_x_um


def test_semantic_bounds_and_ai_intent_validate_their_contracts() -> None:
    with pytest.raises(ValidationError, match="semantic bounds are inverted"):
        domain.SemanticBounds(
            min_x_um=10,
            max_x_um=0,
            min_y_um=0,
            max_y_um=0,
            min_z_um=0,
            max_z_um=0,
        )
    with pytest.raises(ValidationError, match="AI proposals require"):
        _intent(
            domain.SemanticComponentKind.BACK_PANEL,
            "bookcase:back:rear",
            source=domain.SemanticIntentSource.AI_PROPOSAL,
        )


def test_user_drag_adds_a_shelf_row_without_emitting_coordinates() -> None:
    spec = _spec()
    target = next(
        target
        for target in domain.derive_bookcase_snap_targets(spec)
        if domain.SemanticComponentKind.SHELF_ROW in target.accepted_components
    )
    intent = domain.SemanticIntent(
        intent_id="intent-add-shelf",
        action=domain.SemanticAction.ADD,
        component_kind=domain.SemanticComponentKind.SHELF_ROW,
        target_id=target.target_id,
        source=domain.SemanticIntentSource.USER_DRAG,
        pointer_x_permille=250,
        pointer_y_permille=360,
    )

    result = domain.compile_semantic_intent(spec, intent)

    assert result.spec.parameters.shelf_count == spec.parameters.shelf_count + 1
    assert result.target.target_id == target.target_id
    assert result.changes[0].field == "shelf_count"
    assert any("not a direct CNC coordinate" in warning for warning in result.warnings)


def test_shelf_remove_and_no_op_remove_are_explicit() -> None:
    result = domain.compile_semantic_intent(
        _spec(),
        _intent(
            domain.SemanticComponentKind.SHELF_ROW,
            "bookcase:shelf:bay:1",
            action=domain.SemanticAction.REMOVE,
        ),
    )
    assert result.spec.parameters.shelf_count == 4
    assert "togs bort" in result.changes[0].reason

    empty = _spec(domain.BookcaseParameters(shelf_count=0))
    with pytest.raises(domain.SemanticCompilationError, match="does not change"):
        domain.compile_semantic_intent(
            empty,
            _intent(
                domain.SemanticComponentKind.SHELF_ROW,
                "bookcase:shelf:bay:1",
                action=domain.SemanticAction.REMOVE,
            ),
        )


def test_divider_add_and_remove_reset_manual_review() -> None:
    automatic = _spec(domain.BookcaseParameters(reinforcement_mode=domain.ReinforcementMode.AUTO))
    added = domain.compile_semantic_intent(
        automatic,
        _intent(domain.SemanticComponentKind.DIVIDER, "bookcase:divider:carcass"),
    )
    assert added.spec.parameters.vertical_divider_count == 1
    assert added.spec.parameters.reinforcement_mode == domain.ReinforcementMode.MANUAL
    assert {change.field for change in added.changes} == {
        "vertical_divider_count",
        "reinforcement_mode",
    }

    removed = domain.compile_semantic_intent(
        added.spec,
        _intent(
            domain.SemanticComponentKind.DIVIDER,
            "bookcase:divider:carcass",
            action=domain.SemanticAction.REMOVE,
        ),
    )
    assert removed.spec.parameters.vertical_divider_count == 0
    assert "togs bort" in removed.changes[0].reason


def test_back_and_plinth_compile_to_versioned_parameters() -> None:
    without_back = _spec(domain.BookcaseParameters(back_panel=domain.BackPanelType.NONE))
    back = domain.compile_semantic_intent(
        without_back,
        _intent(domain.SemanticComponentKind.BACK_PANEL, "bookcase:back:rear"),
    )
    assert back.spec.parameters.back_panel == domain.BackPanelType.INSET_GROOVE
    assert back.spec.back_material == domain.screening_birch_plywood_6()
    assert any("supplier batch evidence remains required" in warning for warning in back.warnings)

    removed_back = domain.compile_semantic_intent(
        back.spec,
        _intent(
            domain.SemanticComponentKind.BACK_PANEL,
            "bookcase:back:rear",
            action=domain.SemanticAction.REMOVE,
        ),
    )
    assert removed_back.spec.parameters.back_panel == domain.BackPanelType.NONE
    assert removed_back.spec.back_material is None

    without_plinth = _spec(domain.BookcaseParameters(plinth_height_um=0))
    plinth = domain.compile_semantic_intent(
        without_plinth,
        _intent(domain.SemanticComponentKind.PLINTH, "bookcase:plinth:base"),
    )
    assert plinth.spec.parameters.plinth_height_um > 0

    removed_plinth = domain.compile_semantic_intent(
        plinth.spec,
        _intent(
            domain.SemanticComponentKind.PLINTH,
            "bookcase:plinth:base",
            action=domain.SemanticAction.REMOVE,
        ),
    )
    assert removed_plinth.spec.parameters.plinth_height_um == 0


def test_ai_proposal_requires_confirmation_before_mutating_design() -> None:
    spec = _spec()
    intent = _intent(
        domain.SemanticComponentKind.BACK_PANEL,
        "bookcase:back:rear",
        action=domain.SemanticAction.REMOVE,
        source=domain.SemanticIntentSource.AI_PROPOSAL,
        rationale="The user asked for an open-backed shelf.",
    )

    with pytest.raises(domain.SemanticIntentApprovalRequired):
        domain.compile_semantic_intent(spec, intent)

    result = domain.compile_semantic_intent(spec, intent, user_confirmed=True)
    assert result.spec.parameters.back_panel == domain.BackPanelType.NONE
    assert any("AI supplied only the furniture intent" in warning for warning in result.warnings)


def test_unknown_and_incompatible_targets_fail_closed() -> None:
    spec = _spec()
    with pytest.raises(domain.SemanticCompilationError, match="unknown semantic snap target"):
        domain.compile_semantic_intent(
            spec,
            _intent(domain.SemanticComponentKind.SHELF_ROW, "bookcase:shelf:bay:99"),
        )
    with pytest.raises(domain.SemanticCompilationError, match="cannot snap"):
        domain.compile_semantic_intent(
            spec,
            _intent(domain.SemanticComponentKind.DIVIDER, "bookcase:shelf:bay:1"),
        )


def test_invalid_compiled_geometry_is_rejected() -> None:
    spec = _spec(domain.BookcaseParameters(shelf_count=31))
    with pytest.raises(domain.SemanticCompilationError, match="invalid bookcase"):
        domain.compile_semantic_intent(
            spec,
            _intent(domain.SemanticComponentKind.SHELF_ROW, "bookcase:shelf:bay:1"),
        )


def test_free_move_fails_closed_until_intent_names_a_stable_component_instance() -> None:
    spec = _spec()
    intent = _intent(
        domain.SemanticComponentKind.SHELF_ROW,
        "bookcase:shelf:bay:1",
        action=domain.SemanticAction.MOVE,
    )

    with pytest.raises(domain.UnsupportedSemanticOperation):
        domain.compile_semantic_intent(spec, intent)


def test_pointer_height_compiles_to_a_constrained_shelf_ratio() -> None:
    result = domain.compile_semantic_intent(
        _spec(),
        _intent(
            domain.SemanticComponentKind.SHELF_ROW,
            "bookcase:shelf:bay:1",
            pointer_y_permille=250,
        ),
    )

    assert result.spec.parameters.shelf_count == 6
    assert 750_000 in result.spec.parameters.shelf_height_ratios_ppm


def test_pointer_width_compiles_to_unequal_server_validated_bays() -> None:
    result = domain.compile_semantic_intent(
        _spec(),
        _intent(
            domain.SemanticComponentKind.DIVIDER,
            "bookcase:divider:carcass",
            pointer_x_permille=300,
        ),
    )

    assert result.spec.parameters.vertical_divider_count == 1
    assert result.spec.parameters.bay_width_ratios_ppm == (300_000, 700_000)


def test_lower_cabinet_semantics_follow_the_current_bays() -> None:
    spec = _spec(
        domain.BookcaseParameters(
            width_um=domain.mm(2400),
            height_um=domain.mm(2400),
            depth_um=domain.mm(350),
            vertical_divider_count=2,
            bay_width_ratios_ppm=(250_000, 500_000, 250_000),
        )
    )
    result = domain.compile_semantic_intent(
        spec,
        _intent(domain.SemanticComponentKind.BASE_CABINET, "bookcase:cabinet:lower-zone"),
    )

    assert result.spec.parameters.base_cabinet_count == 3
    assert result.spec.parameters.base_cabinet_height_um == domain.mm(680)
    assert result.spec.parameters.base_cabinet_depth_um == domain.mm(350)
