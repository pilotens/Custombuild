from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.design_service import canonical_preview as server_canonical_preview
from app.schemas import BookcasePreviewInput
from custombuild_cad import CadQueryAdapter
from custombuild_domain import require_template_for_revision
from custombuild_manufacturing import canonical_json_bytes, stock_profiles_fingerprint

import scripts.design_review_gate as design_review_gate
from scripts.design_review_gate import (
    SCREENED_DEFAULTS_CONTRACT_FINGERPRINT,
    DesignReviewGateError,
    GateStatus,
    load_gate_fixture,
    load_screened_defaults_contract,
    main,
    run_design_review_gate,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/design-review-gate.v1.json"
CONTRACT = REPO / "packages/contracts/screened-template-defaults.v1.json"


def _run_actual_default_with_presented_status(
    monkeypatch: pytest.MonkeyPatch,
    status: object,
) -> dict[str, Any]:
    contract = load_screened_defaults_contract(CONTRACT)
    default = contract.templates[0]
    preview_payload = BookcasePreviewInput.model_validate(
        design_review_gate._actual_default_preview_payload(default)
    ).model_dump(mode="json", exclude_none=True)
    canonical_result = server_canonical_preview(
        preview_payload,
        design_id="test-actual-default-construction-gate",
        revision=1,
    )

    def canonical_with_status(*args: Any, **kwargs: Any) -> tuple[Any, Any, dict[str, Any]]:
        del args, kwargs
        spec, design, presented = canonical_result
        return spec, design, {**presented, "status": status}

    def forbidden_downstream_call(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        pytest.fail("construction-rule BLOCK must stop before CAD or manufacturing")

    monkeypatch.setattr(design_review_gate, "canonical_preview", canonical_with_status)
    monkeypatch.setattr(
        design_review_gate,
        "_actual_default_cad_evidence",
        forbidden_downstream_call,
    )
    monkeypatch.setattr(
        design_review_gate,
        "build_production_bundle",
        forbidden_downstream_call,
    )
    capability = require_template_for_revision(
        default.template_id,
        str(default.effective_design_spec["furniture_type"]),
    )
    return design_review_gate._run_actual_default(
        repo=REPO,
        default=default,
        contract=contract,
        capability=capability,
        source_manifest_sha256="0" * 64,
        machine=None,
        engine_context=None,
    )


def test_screened_defaults_contract_is_pinned_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    contract = load_screened_defaults_contract(CONTRACT)

    assert contract.contract_version == "1.0.0"
    assert contract.fingerprint == SCREENED_DEFAULTS_CONTRACT_FINGERPRINT
    assert [item.template_id for item in contract.templates] == [
        "shelving",
        "wall-library",
    ]
    assert all(
        item.effective_design_spec["material_id"] == "birch-plywood" for item in contract.templates
    )
    assert all(
        item.effective_design_spec["wall_anchor_verified"] is False for item in contract.templates
    )

    raw = json.loads(CONTRACT.read_text(encoding="utf-8"))
    raw["purpose"] += " Tampered without a version or fingerprint bump."
    tampered = tmp_path / "screened-template-defaults.v1.json"
    tampered.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DesignReviewGateError, match="fingerprint does not match"):
        load_screened_defaults_contract(tampered)


def test_fixture_is_explicit_validation_data_and_fails_closed_when_incomplete(
    tmp_path: Path,
) -> None:
    fixture = load_gate_fixture(FIXTURE)
    assert fixture.raw["fixture_scope"] == "AUTOMATED_DESIGN_REVIEW_ONLY"
    assert len(fixture.fingerprint) == 64
    assert all(
        registration.method_id.startswith("automated-design-review:")
        for template in fixture.registrations.values()
        for stock in template.values()
        for registration in stock.values()
    )

    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del raw["templates"]["wall-library"]["registrations_by_stock"]
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(DesignReviewGateError, match="keys must be exactly"):
        load_gate_fixture(invalid)


def test_actual_default_construction_block_stops_before_cad_and_manufacturing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _run_actual_default_with_presented_status(monkeypatch, "BLOCK")

    assert report["status"] == GateStatus.BLOCK
    assert report["code"] == "ACTUAL_DEFAULT_CONSTRUCTION_RULES_BLOCKED"
    assert report["blocked_stage"] == "CONSTRUCTION_RULES"
    assert report["design_review_integrity_verified"] is False
    assert report["pipeline_executed"] is False
    assert report["physical_release_status"] == GateStatus.BLOCK
    assert report["physical_cutting_authorized"] is False
    assert report["server_canonical"]["status"] == "BLOCK"
    assert report["stock_profiles"] == []
    assert "cad" not in report


def test_actual_default_rejects_unknown_server_construction_status_before_cad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _run_actual_default_with_presented_status(monkeypatch, "UNKNOWN")

    assert report["status"] == GateStatus.BLOCK
    assert report["code"] == "ACTUAL_DEFAULT_SERVER_CANONICALIZATION_FAILED"
    assert report["blocked_stage"] == "SERVER_CANONICALIZATION"
    assert "must be exactly one of" in report["detail"]
    assert report["design_review_integrity_verified"] is False
    assert report["pipeline_executed"] is False
    assert report["physical_cutting_authorized"] is False
    assert "server_canonical" not in report
    assert "cad" not in report


def test_cli_returns_a_machine_readable_closed_result_on_bad_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "bad.json"
    fixture.write_text("{}", encoding="utf-8")
    output = tmp_path / "nested/report.json"

    assert main(["--repo", str(REPO), "--fixture", str(fixture), "--output", str(output)]) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == GateStatus.BLOCK
    assert report["gate_passed"] is False
    assert report["physical_cutting_authorized"] is False
    assert report["design_review_integrity_verified"] is False
    assert json.loads(capsys.readouterr().out) == report


def test_engine_smoke_stock_fingerprint_uses_canonical_grain_axis() -> None:
    design = design_review_gate.build_bookcase(
        design_review_gate.BookcaseDesignSpec(
            design_id="automated-design-review-shelving",
            template_id="shelving",
            parameters=design_review_gate._template_parameters("shelving"),
            material=design_review_gate.screening_mdf_18(),
            back_material=design_review_gate.screening_mdf_6(),
        )
    )

    stocks = design_review_gate._stock_profiles("shelving", design)

    assert [stock.grain_direction for stock in stocks] == ["NONE", "NONE"]
    assert stock_profiles_fingerprint(stocks) == (
        "6d00b7212c8260892cfff1f443d08a56b88c9e2977aa14c356043eebee3009b0"
    )


@pytest.mark.cad
def test_full_gate_separates_mdf_smoke_from_stockless_actual_birch_defaults() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")

    fixture = load_gate_fixture(FIXTURE)
    report = run_design_review_gate(fixture, repo=REPO)
    repeated_report = run_design_review_gate(fixture, repo=REPO)

    assert canonical_json_bytes(report) == canonical_json_bytes(repeated_report)
    assert report == repeated_report

    assert report["status"] == GateStatus.EXTERNAL_EVIDENCE_REQUIRED
    assert report["gate_passed"] is True
    assert report["design_review_integrity_verified"] is True
    assert report["engine_smoke_integrity_verified"] is True
    assert report["actual_default_readiness_verified"] is True
    assert report["external_blocker_status"] == GateStatus.EXTERNAL_EVIDENCE_REQUIRED
    assert report["physical_release_status"] == GateStatus.BLOCK
    assert report["physical_cutting_authorized"] is False
    assert report["screened_template_defaults_contract"] == {
        "schema_version": "custombuild.screened-template-defaults.v1",
        "contract_version": "1.0.0",
        "fingerprint": SCREENED_DEFAULTS_CONTRACT_FINGERPRINT,
        "physical_cutting_authorized": False,
    }

    smoke_by_id = {item["template_id"]: item for item in report["engine_smoke"]["templates"]}
    assert report["engine_smoke"]["status"] == GateStatus.EXTERNAL_EVIDENCE_REQUIRED
    assert set(smoke_by_id) == {"shelving", "wall-library"}
    for template_id in ("shelving", "wall-library"):
        item = smoke_by_id[template_id]
        assert item["input_kind"] == "DETERMINISTIC_MDF_ENGINE_SMOKE"
        assert item["status"] == GateStatus.EXTERNAL_EVIDENCE_REQUIRED
        assert item["design_review_integrity_verified"] is True
        assert item["pipeline_executed"] is True
        assert item["physical_cutting_authorized"] is False
        assert len(item["package"]["sha256"]) == 64
        assert len(item["package"]["manifest_sha256"]) == 64
        paths = {entry["path"] for entry in item["package"]["artifact_inventory"]}
        assert {
            "model/design.step",
            "model/design.glb",
            "validation/design-review-package-status.json",
            "validation/stock-selection.json",
            "validation/generation-plan.json",
        } <= paths
        assert item["workshop_readiness"]["design_review_ready"] is True
        assert item["workshop_readiness"]["external_evidence_required"]
        assert item["workshop_readiness"]["physical_cutting_authorized"] is False

    actual_by_id = {
        item["template_id"]: item for item in report["actual_default_readiness"]["templates"]
    }
    assert report["actual_default_readiness"]["status"] == GateStatus.EXTERNAL_EVIDENCE_REQUIRED
    assert set(actual_by_id) == {"shelving", "wall-library"}
    expected_back_blanks = {
        "shelving": [2_376_266, 2_296_266],
        "wall-library": [2_376_266, 1_634_066],
    }
    for template_id, expected_blank in expected_back_blanks.items():
        item = actual_by_id[template_id]
        assert item["input_kind"] == "ACTUAL_EFFECTIVE_UI_DEFAULT"
        assert item["status"] == GateStatus.EXTERNAL_EVIDENCE_REQUIRED
        assert item["design_review_integrity_verified"] is True
        assert item["pipeline_executed"] is True
        assert item["physical_cutting_authorized"] is False
        assert item["cad"]["status"] == "PASS"
        assert item["cad"]["authoritative_geometry"] is True
        assert item["cad"]["production_ready"] is False
        assert item["cad"]["physical_cutting_authorized"] is False
        assert len(item["cad"]["step"]["sha256"]) == 64
        assert len(item["cad"]["glb"]["sha256"]) == 64
        assert item["server_canonical"]["physical_cutting_authorized"] is False
        assert item["dfm_status"] == "BLOCK"
        assert item["design_review_package_status"]["blocker_codes"] == ["STOCK_PROFILE_MISSING"]
        assert item["design_review_package_status"]["cam_status"] == "BLOCKED"
        assert item["workshop_readiness"]["design_review_ready"] is False
        paths = {entry["path"] for entry in item["package"]["artifact_inventory"]}
        assert {
            "model/design.step",
            "model/design.glb",
            "validation/dfm-report.json",
            "validation/stock-selection.json",
            "validation/generation-plan.json",
        } <= paths
        assert "materials/stock-purchase.csv" not in paths
        assert "labels/label-index.csv" not in paths
        assert "quality/measurement-plan.json" not in paths
        assert not any(
            path.startswith(("cam/", "nesting/", "machine-validation/")) or path.endswith(".ngc")
            for path in paths
        )
        assert item["blocking_issue_codes"] == ["STOCK_PROFILE_MISSING"]
        assert len(item["blocking_issues"]) == 1
        blocker = item["blocking_issues"][0]
        assert blocker["code"] == "STOCK_PROFILE_MISSING"
        assert blocker["part_name"] == "back"
        assert blocker["inputs"]["material_id"] == "birch-plywood-6"
        assert blocker["inputs"]["blank_um"] == expected_blank
        stock_ids = {stock["stock_id"] for stock in item["stock_profiles"]}
        assert stock_ids == {
            "stock-birch-plywood-2440x1220",
            "stock-birch-plywood-6-2440x1220",
        }
        back_stock = next(
            stock
            for stock in item["stock_profiles"]
            if stock["material_id"] == blocker["inputs"]["material_id"]
        )
        assert back_stock["grain_direction"] == "UNBOUND"
        assert {stock["grain_direction"] for stock in item["stock_profiles"]} == {"UNBOUND"}
        assert {stock["width_um"] for stock in item["stock_profiles"]} == {2_440_000}
        assert {stock["height_um"] for stock in item["stock_profiles"]} == {1_220_000}
        assert item["provenance"]["machine_profile_id"] == ("custombuild-router-1325-linuxcnc")
        assert all("mdf" not in stock_id for stock_id in stock_ids)

    concepts_by_id = {item["template_id"]: item for item in report["concepts"]}
    assert set(concepts_by_id) == {
        "cupboard",
        "hanging-shelf",
        "room-divider",
        "sideboard",
    }
    for template_id in ("cupboard", "hanging-shelf", "room-divider", "sideboard"):
        item = concepts_by_id[template_id]
        assert item["status"] == GateStatus.BLOCK
        assert item["code"] == "CONCEPT_TEMPLATE_NOT_RELEASEABLE"
        assert item["design_review_integrity_verified"] is False
        assert item["pipeline_executed"] is False
        assert item["physical_cutting_authorized"] is False


def test_cli_never_turns_a_blocked_actual_default_green(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = {
        "schema_version": "custombuild.design-review-gate.v2",
        "status": GateStatus.BLOCK,
        "gate_passed": False,
        "design_review_integrity_verified": False,
        "engine_smoke_integrity_verified": True,
        "actual_default_readiness_verified": False,
        "physical_cutting_authorized": False,
    }
    monkeypatch.setattr(
        "scripts.design_review_gate.run_design_review_gate",
        lambda fixture, *, repo: blocked,
    )
    output = tmp_path / "report.json"

    assert main(["--repo", str(REPO), "--fixture", str(FIXTURE), "--output", str(output)]) == 1
    assert json.loads(output.read_text(encoding="utf-8")) == blocked
    assert json.loads(capsys.readouterr().out) == blocked
