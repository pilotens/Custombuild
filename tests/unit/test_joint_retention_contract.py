from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.api import _frozen_dado_retention_is_unresolved
from app.schemas import BookcasePreviewInput
from custombuild_cad import CADArtifacts, CadQueryAdapter
from custombuild_domain import (
    BookcaseDesignSpec,
    BookcaseParameters,
    JointRetentionContract,
    JointRetentionLoadCase,
    JointRetentionLoadMode,
    JointRetentionMachiningScope,
    JointRetentionMaterialIdentity,
    JointRetentionMethod,
    JointType,
    build_bookcase,
    dado_joint_geometry_fingerprint,
    mm,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_domain.models import (
    JointRetentionApplicationClass,
    captive_inset_back_topology_is_complete,
)
from custombuild_manufacturing import (
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
    JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
    JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
    ArtifactFile,
    ManifestContext,
    StockSheet,
    blocked_design_review_package_status,
    build_production_bundle,
    canonical_json_bytes,
    dado_retention_evidence_missing,
    generated_design_review_package_status,
    linuxcnc_reference_router_1325,
    validate_design_review_status_retention_binding,
)
from custombuild_worker.documents import (
    VerifiedRetentionTrust,
    _assembly_step_hardware_text,
    assembly_manual_pdf,
    assembly_readiness_json,
    bom_pdf,
    hardware_csv,
)
from pydantic import ValidationError

_SYNTHETIC_SIGNED_RETENTION_BYTES = b'{"fixture":"not-authenticated-test-evidence"}'


@pytest.fixture
def structured_retention_contract() -> JointRetentionContract:
    """Synthetic foundation fixture; it does not authenticate external evidence."""

    preview = build_bookcase(
        BookcaseDesignSpec(
            design_id="test-only-retained-bookcase",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )

    return JointRetentionContract(
        system_id="test-only.retention-system",
        system_version="v1",
        method=JointRetentionMethod.MECHANICAL,
        catalog_entry_sha256="1" * 64,
        evidence_id="test-only.capacity-evidence",
        evidence_sha256=hashlib.sha256(_SYNTHETIC_SIGNED_RETENTION_BYTES).hexdigest(),
        installation_instruction_id="test-only.installation-instruction",
        installation_instruction_version="v1",
        installation_instruction_sha256="3" * 64,
        machining_scope=JointRetentionMachiningScope.NO_ADDITIONAL_CNC,
        hardware_sku="test-only.retention-fastener",
        hardware_count_per_joint=2,
        applicable_materials=(
            JointRetentionMaterialIdentity(
                material_id="mdf",
                material_version="screening-2026.1",
            ),
        ),
        joint_geometry_sha256=dado_joint_geometry_fingerprint(
            preview.parts,
            preview.joints,
        ),
        minimum_applicable_thickness_um=mm(17),
        maximum_applicable_thickness_um=mm(19),
        load_cases=(
            JointRetentionLoadCase(
                mode=JointRetentionLoadMode.SHEAR,
                rated_design_load_n=300,
                verified_capacity_n=600,
            ),
            JointRetentionLoadCase(
                mode=JointRetentionLoadMode.WITHDRAWAL,
                rated_design_load_n=50,
                verified_capacity_n=100,
            ),
        ),
        safety_factor_permille=1_800,
    )


@pytest.fixture
def retained_design(structured_retention_contract: JointRetentionContract):
    return build_bookcase(
        BookcaseDesignSpec(
            design_id="test-only-retained-bookcase",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
            joint_retention=structured_retention_contract,
        )
    )


def _verified_retention_trust(
    contract: JointRetentionContract,
) -> VerifiedRetentionTrust:
    return VerifiedRetentionTrust.from_verified_snapshot(
        {
            "schema_version": "custombuild.joint-retention-binding.v2",
            "application_class": "load_bearing_carcass_dado",
            "storage_evidence_id": "44444444-4444-4444-8444-444444444444",
            "storage_evidence_sha256": contract.evidence_sha256,
            "base_design_hash": "4" * 64,
            "joint_geometry_sha256": contract.joint_geometry_sha256,
            "registry_sha256": "5" * 64,
            "issuer_id": "independent-retention-lab",
            "key_id": "independent-retention-lab-2026",
            "signed_evidence_id": contract.evidence_id,
            "signed_evidence_expires_at": "2027-08-30T12:00:00+00:00",
            "system_id": contract.system_id,
            "system_version": contract.system_version,
            "contract_sha256": hashlib.sha256(canonical_json_bytes(contract)).hexdigest(),
        }
    )


def test_missing_contract_remains_fail_closed_through_the_frozen_api_gate() -> None:
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="missing-retention-contract",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    frozen_version = SimpleNamespace(
        spec_json=design.spec.model_dump(mode="json"),
        design_hash=design.design_hash,
    )

    assert "joint_retention" not in frozen_version.spec_json
    assert dado_retention_evidence_missing(design) is True
    assert _frozen_dado_retention_is_unresolved(frozen_version) is True


def test_synthetic_structurally_complete_contract_exercises_future_path_without_release(
    retained_design,
    structured_retention_contract: JointRetentionContract,
) -> None:
    dado_joints = tuple(
        joint
        for joint in retained_design.joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
    )
    back_grooves = tuple(
        joint
        for joint in retained_design.joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
    )
    assert dado_joints
    assert len(back_grooves) == 4
    assert all(joint.retention == structured_retention_contract for joint in dado_joints)
    assert all(
        joint.hardware_sku == structured_retention_contract.hardware_sku
        and joint.hardware_count == structured_retention_contract.hardware_count_per_joint
        for joint in dado_joints
    )
    assert all(
        joint.retention is None
        and joint.hardware_sku is None
        and joint.hardware_count == 0
        for joint in back_grooves
    )
    assert captive_inset_back_topology_is_complete(
        retained_design.parts,
        retained_design.joints,
        retained_design.assembly_graph,
    )
    assert dado_retention_evidence_missing(retained_design) is False
    assert "joint_retention" in retained_design.spec.model_dump(mode="json")

    frozen_version = SimpleNamespace(
        spec_json=retained_design.spec.model_dump(mode="json"),
        design_hash=retained_design.design_hash,
    )
    assert _frozen_dado_retention_is_unresolved(frozen_version) is False

    package_status = generated_design_review_package_status(
        validation_program_included=True
    )
    assert package_status.physical_cutting_authorized is False
    readiness = json.loads(assembly_readiness_json(retained_design))
    assert readiness["physical_assembly_authorized"] is False
    assert readiness["customer_assembly_authorized"] is False
    assert "verified_adhesive_free_joint_retention" in readiness["missing_requirements"]
    assert "named_assembly_safety_approver" in readiness["missing_requirements"]

    rows = tuple(
        csv.DictReader(io.StringIO(hardware_csv(retained_design).decode("utf-8")))
    )
    contract_row = next(
        row
        for row in rows
        if row["hardware_sku"] == structured_retention_contract.hardware_sku
    )
    assert (
        contract_row["selection_status"]
        == "STRUCTURALLY_COMPLETE_RETENTION_APPLICATION"
    )
    assert (
        contract_row["catalog_authenticity_status"]
        == "NOT_ESTABLISHED_BY_CURRENT_MVP"
    )
    assert contract_row["catalog_entry_sha256"] == "1" * 64
    assert contract_row["installation_instruction_sha256"] == "3" * 64
    assert (
        contract_row["joint_geometry_sha256"]
        == structured_retention_contract.joint_geometry_sha256
    )
    assert contract_row["applicable_materials"] == "mdf@screening-2026.1"
    assert contract_row["rated_shear_design_load_n"] == "300"
    assert contract_row["rated_withdrawal_design_load_n"] == "50"

    dado_step = next(
        step
        for step in retained_design.assembly_graph.steps
        if any(joint.joint_id in step.joint_ids for joint in dado_joints)
    )
    assert _assembly_step_hardware_text(retained_design, dado_step).startswith(
        "VERSIONSBUNDET RETENTIONSSYSTEM"
    )


def test_verified_retention_trust_is_reported_without_authorizing_physical_work(
    retained_design,
    structured_retention_contract: JointRetentionContract,
) -> None:
    trust = _verified_retention_trust(structured_retention_contract)
    first_documents = (
        bom_pdf(retained_design, trust),
        hardware_csv(retained_design, trust),
        assembly_manual_pdf(retained_design, trust),
        assembly_readiness_json(retained_design, trust),
    )
    second_documents = (
        bom_pdf(retained_design, trust),
        hardware_csv(retained_design, trust),
        assembly_manual_pdf(retained_design, trust),
        assembly_readiness_json(retained_design, trust),
    )

    assert first_documents == second_documents
    rows = tuple(csv.DictReader(io.StringIO(first_documents[1].decode("utf-8"))))
    contract_row = next(
        row
        for row in rows
        if row["hardware_sku"] == structured_retention_contract.hardware_sku
    )
    assert contract_row["selection_status"] == "SERVER_VERIFIED_RETENTION_APPLICATION"
    assert contract_row["catalog_authenticity_status"] == (
        "CERTIFIER_SIGNED_ENTRY_SERVER_VERIFIED_FOR_GENERATION_SNAPSHOT"
    )
    assert contract_row["storage_evidence_id"] == trust.storage_evidence_id
    assert contract_row["storage_evidence_sha256"] == trust.storage_evidence_sha256
    assert contract_row["trust_registry_sha256"] == trust.registry_sha256
    assert contract_row["certifier_issuer_id"] == trust.issuer_id
    assert contract_row["certifier_key_id"] == trust.key_id
    assert contract_row["signed_evidence_expires_at"] == trust.signed_evidence_expires_at
    assert contract_row["retention_contract_sha256"] == trust.contract_sha256
    assert "current revocation and expiry" in contract_row["required_action"]
    assert "does not authorize physical assembly or cutting" in contract_row["required_action"]
    readiness = json.loads(first_documents[3])
    assert "verified_adhesive_free_joint_retention" not in readiness["missing_requirements"]

    dado_joint_ids = {
        joint.joint_id
        for joint in retained_design.joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
    }
    dado_step = next(
        step
        for step in retained_design.assembly_graph.steps
        if dado_joint_ids.intersection(step.joint_ids)
    )
    manual_text = _assembly_step_hardware_text(retained_design, dado_step, trust)
    assert manual_text.startswith("SERVERVERIFIERAT CERTIFIERARSIGNERAT RETENTIONSSYSTEM")
    assert trust.issuer_id in manual_text
    assert trust.key_id in manual_text
    assert "auktoriserar inte fysisk montering eller kapning" in manual_text


def test_missing_malformed_or_detached_retention_trust_remains_unverified(
    retained_design,
    structured_retention_contract: JointRetentionContract,
) -> None:
    trust = _verified_retention_trust(structured_retention_contract)
    detached = replace(trust, contract_sha256="0" * 64)

    for candidate in (None, detached, {"schema_version": trust.schema_version}):
        rows = tuple(
            csv.DictReader(
                io.StringIO(hardware_csv(retained_design, candidate).decode("utf-8"))
            )
        )
        contract_row = next(
            row
            for row in rows
            if row["hardware_sku"] == structured_retention_contract.hardware_sku
        )
        assert contract_row["catalog_authenticity_status"] == "NOT_ESTABLISHED_BY_CURRENT_MVP"
        assert contract_row["certifier_issuer_id"] == ""
        assert contract_row["selection_status"] == (
            "STRUCTURALLY_COMPLETE_RETENTION_APPLICATION"
        )
        readiness = json.loads(assembly_readiness_json(retained_design, candidate))
        assert "verified_adhesive_free_joint_retention" in readiness["missing_requirements"]

    malformed = trust.__dict__ if hasattr(trust, "__dict__") else {
        field: getattr(trust, field) for field in trust.__dataclass_fields__
    }
    with pytest.raises(ValueError, match="invalid field set"):
        VerifiedRetentionTrust.from_verified_snapshot({**malformed, "unexpected": "value"})


def test_structurally_complete_retention_reaches_next_gate_without_authorizing_cutting(
    retained_design,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = linuxcnc_reference_router_1325()
    assert retained_design.spec.back_material is not None
    stock = (
        StockSheet(
            "test-only-mdf-18-stock",
            retained_design.spec.material.material_id,
            retained_design.spec.material.version,
            2_440_000,
            1_220_000,
            18_000,
            quantity=2,
            grain_direction="X",
        ),
        StockSheet(
            "test-only-mdf-6-stock",
            retained_design.spec.back_material.material_id,
            retained_design.spec.back_material.version,
            2_440_000,
            1_220_000,
            6_000,
            quantity=1,
            grain_direction="X",
        ),
    )
    context = ManifestContext(
        project_id="test-only-project",
        revision="1",
        design_hash=retained_design.design_hash,
        app_version="0.1.0",
        engine_version="derived-by-pipeline",
        template_version="derived-by-pipeline",
        template_id="shelving",
        template_capability_fingerprint="c" * 64,
        template_capability={
            "template_id": "shelving",
            "template_version": "1.0.0",
            "capability_fingerprint": "c" * 64,
        },
        rule_version="rules-1.0.0",
        material_versions=(),
        joint_version="joints-1.0.0",
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        postprocessor_version="derived-by-pipeline",
        cad_status="derived-by-pipeline",
        generation_context_hash="f" * 64,
        production_engine_context={
            "schema_version": "test-production-context.v1",
            "template_capability_registry_version": "test-registry-1.0.0",
        },
    )
    monkeypatch.setattr(
        CadQueryAdapter,
        "export_design",
        lambda _self, _design: CADArtifacts(
            step=b"ISO-10303-21;\nEND-ISO-10303-21;",
            glb=b"glTF" + bytes(20),
            kernel="test-only",
            adapter_version="test-only",
        ),
    )
    monkeypatch.setattr(
        CadQueryAdapter,
        "validate_design_artifacts",
        lambda _self, _design, _artifacts: None,
    )

    bundle = build_production_bundle(
        retained_design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=True,
        include_validation_program=True,
        allow_blocked_cam=True,
        additional_artifacts=(
            ArtifactFile(
                JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
                _SYNTHETIC_SIGNED_RETENTION_BYTES,
                JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
                JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
            ),
        ),
    )

    assert bundle.review_status.blocker_codes == ("TWO_SIDED_REGISTRATION_MISSING",)
    assert "DADO_RETENTION_EVIDENCE_MISSING" not in bundle.review_status.blocker_codes
    assert bundle.operations is None
    assert bundle.review_status.physical_cutting_authorized is False
    assert bundle.workshop_readiness.physical_cutting_authorized is False
    assert bundle.manifest["physical_cutting_authorized"] is False
    with zipfile.ZipFile(io.BytesIO(bundle.zip_bytes)) as archive:
        assert (
            archive.read(JOINT_RETENTION_SIGNED_EVIDENCE_PATH)
            == _SYNTHETIC_SIGNED_RETENTION_BYTES
        )


def test_review_status_is_bound_both_ways_to_frozen_retention(retained_design) -> None:
    unresolved_design = build_bookcase(
        BookcaseDesignSpec(
            design_id="unresolved-status-binding",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    generated = generated_design_review_package_status(
        validation_program_included=True
    )
    blocked = blocked_design_review_package_status(
        (DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,)
    )

    with pytest.raises(ValueError, match="generated CAM status contradicts"):
        validate_design_review_status_retention_binding(generated, unresolved_design)
    with pytest.raises(ValueError, match="blocker contradicts"):
        validate_design_review_status_retention_binding(blocked, retained_design)

    validate_design_review_status_retention_binding(generated, retained_design)
    assert generated.physical_cutting_authorized is False


def test_public_preview_cannot_fabricate_a_retention_catalog_result(
    structured_retention_contract: JointRetentionContract,
) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        BookcasePreviewInput.model_validate(
            {"joint_retention": structured_retention_contract.model_dump(mode="json")}
        )


def test_contract_rejects_capacity_below_its_load_and_safety_factor(
    structured_retention_contract: JointRetentionContract,
) -> None:
    payload = structured_retention_contract.model_dump(mode="python")
    payload["load_cases"][0]["verified_capacity_n"] = 539

    with pytest.raises(ValidationError, match="does not meet the declared design load"):
        JointRetentionContract.model_validate(payload)


def test_design_rejects_a_contract_that_omits_the_carcass_material(
    structured_retention_contract: JointRetentionContract,
) -> None:
    payload = structured_retention_contract.model_dump(mode="python")
    payload["applicable_materials"] = [
        {
            "material_id": "mdf-6",
            "material_version": "screening-2026.1",
        }
    ]
    incomplete_contract = JointRetentionContract.model_validate(payload)

    with pytest.raises(ValidationError, match="every DADO member material version"):
        BookcaseDesignSpec(
            design_id="incomplete-material-applicability",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
            joint_retention=incomplete_contract,
        )


def test_design_result_rejects_retention_for_different_joint_geometry(
    structured_retention_contract: JointRetentionContract,
) -> None:
    wrong_geometry = structured_retention_contract.model_copy(
        update={"joint_geometry_sha256": "4" * 64}
    )
    spec = BookcaseDesignSpec(
        design_id="test-only-retained-bookcase",
        parameters=BookcaseParameters(),
        material=screening_mdf_18(),
        back_material=screening_mdf_6(),
        joint_retention=wrong_geometry,
    )

    with pytest.raises(ValidationError, match="exact DADO geometry fingerprint"):
        build_bookcase(spec)


def test_post_validation_tampering_cannot_resolve_retention(
    retained_design,
    structured_retention_contract: JointRetentionContract,
) -> None:
    tampered_contract = structured_retention_contract.model_copy(
        update={"installation_instruction_sha256": "not-a-sha256"}
    )
    tampered_joints = tuple(
        joint.model_copy(update={"retention": tampered_contract})
        if joint.retention_application_class
        == JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
        else joint
        for joint in retained_design.joints
    )
    tampered_design = retained_design.model_copy(update={"joints": tampered_joints})

    assert dado_retention_evidence_missing(tampered_design) is True


def test_removed_dado_topology_cannot_resolve_retention_with_the_old_hash(
    retained_design,
) -> None:
    tampered_design = retained_design.model_copy(
        update={
            "joints": tuple(
                joint
                for joint in retained_design.joints
                if joint.joint_type != JointType.DADO
            )
        }
    )

    assert tampered_design.design_hash == retained_design.design_hash
    assert dado_retention_evidence_missing(tampered_design) is True
