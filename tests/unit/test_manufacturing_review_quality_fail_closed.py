from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import custombuild_manufacturing as manufacturing
import pytest
from custombuild_domain import (
    BookcaseDesignSpec,
    BookcaseParameters,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_manufacturing import review_status
from custombuild_manufacturing.quality import validate_json_schema_instance


def _complete_retention_case() -> tuple[Any, tuple[Any, ...], dict[str, Any]]:
    retention = SimpleNamespace(
        joint_type="dado",
        application_class="load_bearing_carcass_dado",
        system_id="retention.system",
        system_version="v1",
        evidence_id="capacity.evidence",
        installation_instruction_id="installation.instruction",
        installation_instruction_version="v1",
        hardware_sku="fastener.sku",
        catalog_entry_sha256="1" * 64,
        evidence_sha256="2" * 64,
        installation_instruction_sha256="3" * 64,
        joint_geometry_sha256="4" * 64,
        method="mechanical",
        machining_scope="no_additional_cnc",
        bound_feature_ids=(),
        applicable_materials=(SimpleNamespace(material_id="mdf", material_version="v1"),),
        hardware_count_per_joint=2,
        minimum_applicable_thickness_um=17_000,
        maximum_applicable_thickness_um=19_000,
        safety_factor_permille=1_800,
        load_cases=(
            SimpleNamespace(
                mode="shear",
                rated_design_load_n=100,
                verified_capacity_n=200,
            ),
            SimpleNamespace(
                mode="withdrawal",
                rated_design_load_n=50,
                verified_capacity_n=100,
            ),
        ),
    )
    joint = SimpleNamespace(
        joint_type="dado",
        retention=retention,
        retention_application_class="load_bearing_carcass_dado",
        hardware_sku="fastener.sku",
        hardware_count=2,
        members=(
            SimpleNamespace(part_id="side-left"),
            SimpleNamespace(part_id="shelf-1"),
        ),
    )
    parts = (
        SimpleNamespace(
            part_id="side-left",
            actual_thickness_um=18_000,
            material_id="mdf",
            material_version="v1",
        ),
        SimpleNamespace(
            part_id="shelf-1",
            actual_thickness_um=18_000,
            material_id="mdf",
            material_version="v1",
        ),
    )
    requirements = {
        "required_design_load_n": {"shear": 100, "withdrawal": 50},
        "required_safety_factor_permille": 1_500,
    }
    return joint, parts, requirements


@pytest.mark.parametrize(
    "failure",
    (
        "joint-type",
        "retention-joint-type",
        "application-class",
        "catalog-identity",
        "digest",
        "method",
        "machining-scope",
        "feature-container",
        "duplicate-features",
        "nonempty-features",
        "material-container",
        "material-identity",
        "material-order",
        "numeric-contract",
        "thickness-order",
        "minimum-safety-factor",
        "load-case-count",
        "load-case-order",
        "load-case-number",
        "load-case-capacity",
        "required-load-contract",
        "required-rated-load",
        "required-safety-factor",
        "hardware-sku",
        "hardware-count",
        "member-count",
        "member-thickness-type",
        "member-thickness-range",
        "member-material",
    ),
)
def test_joint_retention_contract_rejects_each_incomplete_safety_binding(
    failure: str,
) -> None:
    joint, parts, requirements = deepcopy(_complete_retention_case())
    retention = joint.retention

    if failure == "joint-type":
        joint.joint_type = "rabbet"
    elif failure == "retention-joint-type":
        retention.joint_type = "rabbet"
    elif failure == "application-class":
        retention.application_class = "decorative_dado"
    elif failure == "catalog-identity":
        retention.system_id = ""
    elif failure == "digest":
        retention.evidence_sha256 = "not-a-sha256"
    elif failure == "method":
        retention.method = "adhesive"
    elif failure == "machining-scope":
        retention.machining_scope = "features_bound_to_joint"
    elif failure == "feature-container":
        retention.bound_feature_ids = None
    elif failure == "duplicate-features":
        retention.bound_feature_ids = ("feature-1", "feature-1")
    elif failure == "nonempty-features":
        retention.bound_feature_ids = ("feature-1",)
    elif failure == "material-container":
        retention.applicable_materials = ()
    elif failure == "material-identity":
        retention.applicable_materials = (SimpleNamespace(material_id="", material_version="v1"),)
    elif failure == "material-order":
        retention.applicable_materials = (
            SimpleNamespace(material_id="mdf", material_version="v2"),
            SimpleNamespace(material_id="mdf", material_version="v1"),
        )
    elif failure == "numeric-contract":
        retention.hardware_count_per_joint = False
    elif failure == "thickness-order":
        retention.minimum_applicable_thickness_um = 20_000
    elif failure == "minimum-safety-factor":
        retention.safety_factor_permille = 999
    elif failure == "load-case-count":
        retention.load_cases = ()
    elif failure == "load-case-order":
        retention.load_cases = tuple(reversed(retention.load_cases))
    elif failure == "load-case-number":
        retention.load_cases[0].rated_design_load_n = 0
    elif failure == "load-case-capacity":
        retention.load_cases[0].verified_capacity_n = 1
    elif failure == "required-load-contract":
        requirements["required_design_load_n"] = None
    elif failure == "required-rated-load":
        retention.load_cases[0].rated_design_load_n = 99
    elif failure == "required-safety-factor":
        requirements["required_safety_factor_permille"] = 1_900
    elif failure == "hardware-sku":
        joint.hardware_sku = "wrong.sku"
    elif failure == "hardware-count":
        joint.hardware_count = 1
    elif failure == "member-count":
        joint.members = ()
    elif failure == "member-thickness-type":
        parts[0].actual_thickness_um = False
    elif failure == "member-thickness-range":
        parts[0].actual_thickness_um = 20_000
    elif failure == "member-material":
        parts[0].material_version = "v2"
    else:
        raise AssertionError(f"unknown failure case: {failure}")

    assert not review_status.joint_retention_contract_is_structurally_complete(
        joint,
        parts=parts,
        expected_contract=retention,
        **requirements,
    )


def test_retention_status_checks_fail_closed_for_invalid_canonical_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_dados = SimpleNamespace(joints=(), spec=SimpleNamespace())
    monkeypatch.setattr(
        review_status,
        "_canonical_design_for_retention_check",
        lambda _design: no_dados,
    )
    assert review_status.dado_retention_evidence_missing(object()) is False

    invalid_load_contract = SimpleNamespace(
        joints=(
            SimpleNamespace(
                joint_type="dado",
                retention_application_class="load_bearing_carcass_dado",
            ),
        ),
        parts=(),
        spec=SimpleNamespace(
            parameters=SimpleNamespace(
                shelf_load_n=-1,
                assumed_horizontal_force_n=50,
                structural_safety_factor_permille=1_800,
            ),
            joint_retention=None,
        ),
    )
    monkeypatch.setattr(
        review_status,
        "_canonical_design_for_retention_check",
        lambda _design: invalid_load_contract,
    )
    assert review_status.dado_retention_evidence_missing(object()) is True


def test_back_panel_retention_checks_all_fail_closed_topology_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert review_status.back_panel_retention_evidence_missing(object()) is False

    spec = BookcaseDesignSpec(
        design_id="coverage-back-panel",
        parameters=BookcaseParameters(),
        material=screening_mdf_18(),
        back_material=screening_mdf_6(),
    )
    design = SimpleNamespace(spec=spec)
    monkeypatch.setattr(
        review_status,
        "_canonical_design_for_retention_check",
        lambda _design: None,
    )
    assert review_status.back_panel_retention_evidence_missing(design) is True

    canonical_without_back = SimpleNamespace(
        spec=SimpleNamespace(parameters=SimpleNamespace(back_panel="none")),
    )
    monkeypatch.setattr(
        review_status,
        "_canonical_design_for_retention_check",
        lambda _design: canonical_without_back,
    )
    assert review_status.back_panel_retention_evidence_missing(design) is False

    canonical_inset_back = SimpleNamespace(
        spec=SimpleNamespace(parameters=SimpleNamespace(back_panel="inset_groove")),
        parts=(),
        joints=(),
        assembly_graph=SimpleNamespace(),
    )
    monkeypatch.setattr(
        review_status,
        "_canonical_design_for_retention_check",
        lambda _design: canonical_inset_back,
    )
    domain_models = import_module("custombuild_domain.models")

    def invalid_topology(*_args: Any) -> bool:
        raise ValueError("invalid canonical topology")

    monkeypatch.setattr(
        domain_models,
        "captive_inset_back_topology_is_complete",
        invalid_topology,
    )
    assert review_status.back_panel_retention_evidence_missing(design) is True


@pytest.mark.parametrize(
    ("instance", "schema", "message"),
    (
        (1, False, "value is prohibited"),
        (1, 1, "schema rule is invalid"),
        (1, {"$ref": "external-schema"}, "schema reference is unsupported"),
        (
            1,
            {"$defs": {}, "$ref": "#/$defs/missing"},
            "schema reference is unresolved",
        ),
        ("outside", {"enum": ["inside"]}, "value is outside enum"),
        (1, {"type": "string"}, "value has the wrong type"),
        ("", {"type": "string", "minLength": 1}, "string is too short"),
        ("xx", {"type": "string", "maxLength": 1}, "string is too long"),
        ("x", {"type": "string", "pattern": "y"}, "string does not match pattern"),
        (0, {"type": "integer", "minimum": 1}, "integer is below minimum"),
        ({}, {"type": "object", "required": "id"}, "required declaration is invalid"),
        (
            {},
            {"type": "object", "required": ["id"]},
            "required properties missing: id",
        ),
        ({}, {"type": "object", "properties": []}, "properties declaration is invalid"),
        ([], {"type": "array", "minItems": 1}, "array has too few items"),
        ([1], {"type": "array", "maxItems": 0}, "array has too many items"),
        ([1, 1], {"type": "array", "uniqueItems": True}, "array items are not unique"),
        (
            [],
            {"type": "array", "prefixItems": {}},
            "prefixItems declaration is invalid",
        ),
        (
            1,
            {"$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"},
            "maximum validation depth exceeded",
        ),
    ),
)
def test_published_schema_validator_rejects_malformed_or_unsafe_contracts(
    instance: Any,
    schema: Any,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_json_schema_instance(instance, schema)


def test_published_schema_validator_accepts_explicit_unconstrained_rule() -> None:
    validate_json_schema_instance({"safe": True}, True)


def test_manufacturing_lazy_exports_cover_both_contract_modules() -> None:
    candidate_value = manufacturing.__getattr__("CAM_CANDIDATE_STATUS")
    package_value = manufacturing.__getattr__("GENERATION_PLAN_SCHEMA_VERSION")

    assert (
        candidate_value
        == import_module("custombuild_manufacturing.cam_candidate_package").CAM_CANDIDATE_STATUS
    )
    assert (
        package_value
        == import_module("custombuild_manufacturing.package").GENERATION_PLAN_SCHEMA_VERSION
    )
    assert "CAM_CANDIDATE_STATUS" in manufacturing.__dir__()
    assert "GENERATION_PLAN_SCHEMA_VERSION" in manufacturing.__dir__()
    with pytest.raises(AttributeError, match="UNKNOWN_MANUFACTURING_EXPORT"):
        manufacturing.__getattr__("UNKNOWN_MANUFACTURING_EXPORT")
