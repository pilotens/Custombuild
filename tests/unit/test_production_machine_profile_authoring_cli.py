from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from custombuild_cam import MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
from custombuild_manufacturing import (
    SERVER_OWNED_PRODUCTION_PROFILE,
    WORKSHOP_ACCEPTED_STATUS,
    canonical_json_bytes,
    sha256_hex,
)
from custombuild_manufacturing.production_machine_profile import (
    load_production_machine_profile,
)
from custombuild_postprocessors import (
    CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY,
    EXTERNAL_AXIS_OFFSET_POLICY,
    METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
    LinuxCNCProductionMachineProfile,
)

from scripts import production_machine_profile as authoring
from tests.integration.test_shelving_cam_candidate import (
    _review_bundle_with_production_clearance,
    _test_only_profile,
)


@pytest.fixture(scope="module")
def review_bundle() -> bytes:
    return _review_bundle_with_production_clearance().zip_bytes


def _accepted_string(value: str) -> str:
    replacements = (
        ("EXTERNAL", "EOFFSET"),
        ("external", "eoffset"),
        ("TEST_ONLY", "CI_ACCEPTED"),
        ("test-only", "ci-accepted"),
        ("test_", "ci_"),
        ("test-", "ci-"),
        ("test:", "ci:"),
        ("TEST_", "CI_"),
        ("TEST-", "CI-"),
        ("TEST:", "CI:"),
    )
    for source, target in replacements:
        value = value.replace(source, target)
    return value


def _accepted_value(value: Any) -> Any:
    if isinstance(value, str):
        return _accepted_string(value)
    if isinstance(value, list):
        return [_accepted_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _accepted_value(item) for key, item in value.items()}
    return value


def _nested_array(depth: int) -> object:
    value: object = "not-a-controller-version"
    for _index in range(depth):
        value = [value]
    return value


def _completed_draft(review_bundle: bytes) -> dict[str, Any]:
    draft, source = authoring.build_profile_draft(review_bundle)
    test_document = json.loads(_test_only_profile(source))
    payload = _accepted_value(test_document["payload"])
    payload["profile_class"] = SERVER_OWNED_PRODUCTION_PROFILE
    payload["acceptance"] = {
        "evidence_id": "ci-accepted-workshop-profile",
        "evidence_sha256": sha256_hex(b"ci accepted workshop profile evidence"),
        "evidence_version": "ci-v1",
        "status": WORKSHOP_ACCEPTED_STATUS,
    }
    payload["machine"]["postprocessor_profile_sha256"] = {
        authoring.COMPUTED_KEY: authoring.POSTPROCESSOR_HASH_VALUE
    }
    draft["payload"] = payload
    return draft


@pytest.fixture(scope="module")
def production_profile(review_bundle: bytes) -> bytes:
    profile, diagnostics = authoring.finalize_profile_draft(
        review_bundle,
        json.dumps(_completed_draft(review_bundle)).encode(),
    )
    assert not diagnostics
    assert profile is not None
    return profile


def test_init_derives_source_bindings_but_no_shop_facts(review_bundle: bytes) -> None:
    draft, source = authoring.build_profile_draft(review_bundle)

    assert draft["deployable"] is False
    assert draft["design_review_sha256"] == sha256_hex(review_bundle)
    payload = draft["payload"]
    assert isinstance(payload, dict)
    assert payload["profile_class"] == SERVER_OWNED_PRODUCTION_PROFILE
    assert payload["acceptance"]["status"] == {authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE}
    machine = payload["machine"]
    assert machine["source_machine_profile_id"] == source.machine_profile_id
    assert machine["machine_profile_id"] == {authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE}
    assert machine["postprocessor_profile_sha256"] == {
        authoring.COMPUTED_KEY: authoring.POSTPROCESSOR_HASH_VALUE
    }
    postprocessor = payload["postprocessor_profile"]
    assert postprocessor["external_axis_offset_policy"] == EXTERNAL_AXIS_OFFSET_POLICY
    assert (
        postprocessor["continuous_spindle_speed_interlock_policy"]
        == CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY
    )
    assert (
        postprocessor["metric_xyz_identity_kinematics_policy"]
        == METRIC_XYZ_IDENTITY_KINEMATICS_POLICY
    )
    assert postprocessor["metric_xyz_identity_kinematics_evidence_id"] == {
        authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE
    }
    assert postprocessor["linear_units_mm_verified"] == {
        authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE
    }
    assert postprocessor["exactly_three_joints_verified"] == {
        authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE
    }
    assert postprocessor["external_axis_offset_evidence_id"] == {
        authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE
    }
    assert postprocessor["continuous_spindle_speed_feed_inhibit_verified"] == {
        authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE
    }
    first_setup = payload["setups"][0]
    source_setup = sorted(source.setups, key=lambda value: value.setup_id)[0]
    assert first_setup["source_material_version"] == source_setup.material_version
    assert first_setup["material_version"] == {authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE}
    assert first_setup["material_evidence_sha256"] == {
        authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE
    }
    assert first_setup["machine_wcs_xy_rotation_mdeg"] == {
        authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE
    }
    first_tool = payload["tools"][0]
    for field in (
        "drill_point_length_um",
        "expected_length_offset_x_um",
        "expected_length_offset_y_um",
    ):
        assert first_tool[field] == {authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE}
    assert payload["recipes"][0]["material_id"] == {
        authoring.UNRESOLVED_KEY: authoring.UNRESOLVED_VALUE
    }
    assert len(payload["setups"]) == len(source.setups)
    assert len(payload["tools"]) == len(source.tools)
    assert len(payload["recipes"]) == len(draft["requirements"]["recipe_bindings"])
    assert authoring._find_unresolved(payload, pointer="/payload")


def test_init_cli_exclusively_writes_editable_owner_only_draft(
    review_bundle: bytes,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review_path = tmp_path / "review.zip"
    review_path.write_bytes(review_bundle)
    output = tmp_path / "profile.draft.json"

    assert (
        authoring.main(
            (
                "init",
                "--design-review",
                str(review_path),
                "--output",
                str(output),
            )
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    draft = json.loads(output.read_bytes())
    assert receipt["status"] == "DRAFT_CREATED"
    assert receipt["unresolved_workshop_fact_count"] > 0
    assert draft["deployable"] is False
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    assert (
        authoring.main(
            (
                "init",
                "--design-review",
                str(review_path),
                "--output",
                str(output),
            )
        )
        == 2
    )
    failure = json.loads(capsys.readouterr().err)
    assert failure["diagnostics"][0]["code"] == "OUTPUT_CREATE_FAILED"


def test_init_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "review.fifo"
    os.mkfifo(fifo)

    completed = subprocess.run(  # noqa: S603 -- fixed interpreter and module
        (
            sys.executable,
            "-m",
            "scripts.production_machine_profile",
            "init",
            "--design-review",
            str(fifo),
            "--output",
            str(tmp_path / "draft.json"),
        ),
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert completed.returncode == 2
    report = json.loads(completed.stderr)
    assert report["diagnostics"][0]["code"] == "INPUT_NOT_REGULAR"


def test_exclusive_output_enforces_mode_0600_despite_umask(tmp_path: Path) -> None:
    output = tmp_path / "owner-only.json"
    previous_umask = os.umask(0o777)
    try:
        authoring._write_new_file(output, b"{}")
    finally:
        os.umask(previous_umask)

    assert output.read_bytes() == b"{}"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_unresolved_draft_reports_every_pointer_and_writes_nothing(
    review_bundle: bytes,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    draft, _source = authoring.build_profile_draft(review_bundle)
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    review_path = tmp_path / "review.zip"
    review_path.write_bytes(review_bundle)
    output = tmp_path / "production.json"

    assert (
        authoring.main(
            (
                "finalize",
                "--design-review",
                str(review_path),
                "--draft",
                str(draft_path),
                "--output",
                str(output),
            )
        )
        == 2
    )
    report = json.loads(capsys.readouterr().err)
    pointers = {item["pointer"] for item in report["diagnostics"]}
    assert report["status"] == "INVALID"
    assert "/payload/acceptance/status" in pointers
    assert "/payload/machine/machine_profile_id" in pointers
    assert "/payload/postprocessor_profile/external_axis_offset_evidence_id" in pointers
    assert "/payload/postprocessor_profile/metric_xyz_identity_kinematics_evidence_id" in pointers
    assert "/payload/postprocessor_profile/linear_units_mm_verified" in pointers
    assert "/payload/postprocessor_profile/exactly_three_joints_verified" in pointers
    assert not output.exists()


def test_finalize_computes_hash_chain_and_validate_is_read_only(
    review_bundle: bytes,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review_path = tmp_path / "review.zip"
    review_path.write_bytes(review_bundle)
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(_completed_draft(review_bundle)), encoding="utf-8")
    output = tmp_path / "production.json"

    assert (
        authoring.main(
            (
                "finalize",
                "--design-review",
                str(review_path),
                "--draft",
                str(draft_path),
                "--output",
                str(output),
            )
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    profile_bytes = output.read_bytes()
    document = json.loads(profile_bytes)
    loaded = load_production_machine_profile(profile_bytes)
    assert profile_bytes == canonical_json_bytes(document)
    assert not profile_bytes.endswith(b"\n")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert document["payload_sha256"] == sha256_hex(canonical_json_bytes(document["payload"]))
    assert document["payload"]["machine"]["postprocessor_profile_sha256"] == sha256_hex(
        canonical_json_bytes(document["payload"]["postprocessor_profile"])
    )
    assert receipt["document_sha256"] == sha256_hex(profile_bytes) == loaded.document_sha256
    assert receipt["physical_cutting_authorized"] is False

    before = output.stat()
    assert (
        authoring.main(
            (
                "validate",
                "--design-review",
                str(review_path),
                "--profile",
                str(output),
            )
        )
        == 0
    )
    validation_receipt = json.loads(capsys.readouterr().out)
    after = output.stat()
    assert validation_receipt["candidate_generation_ready"] is True
    assert validation_receipt["physical_cutting_authorized"] is False
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )


def test_validate_reports_exact_tampered_source_pointer(
    review_bundle: bytes,
    production_profile: bytes,
) -> None:
    document = json.loads(production_profile)
    document["payload"]["tools"][0]["source_tool_sha256"] = "f" * 64
    document["payload_sha256"] = sha256_hex(canonical_json_bytes(document["payload"]))

    loaded, diagnostics = authoring.validate_profile_bytes(
        review_bundle,
        canonical_json_bytes(document),
    )

    assert loaded is None
    assert any(
        diagnostic.code == "SOURCE_BINDING_MISMATCH"
        and diagnostic.pointer == "/payload/tools/0/source_tool_sha256"
        for diagnostic in diagnostics
    )


@pytest.mark.parametrize(
    ("collection", "field", "code"),
    (
        ("setups", "setup_id", "SOURCE_SETUP_COVERAGE_MISMATCH"),
        ("tools", "source_tool_id", "SOURCE_TOOL_COVERAGE_MISMATCH"),
    ),
)
def test_validate_reports_malformed_binding_ids_without_crashing(
    review_bundle: bytes,
    production_profile: bytes,
    collection: str,
    field: str,
    code: str,
) -> None:
    document = json.loads(production_profile)
    document["payload"][collection][0][field] = {"not": "a string"}
    document["payload_sha256"] = sha256_hex(canonical_json_bytes(document["payload"]))

    loaded, diagnostics = authoring.validate_profile_bytes(
        review_bundle,
        canonical_json_bytes(document),
    )

    assert loaded is None
    assert any(diagnostic.code == code for diagnostic in diagnostics)


def test_validate_requires_recipes_for_actual_workshop_material(
    review_bundle: bytes,
    production_profile: bytes,
) -> None:
    document = json.loads(production_profile)
    document["payload"]["recipes"][0]["material_id"] = "wrong-workshop-material"
    document["payload_sha256"] = sha256_hex(canonical_json_bytes(document["payload"]))

    loaded, diagnostics = authoring.validate_profile_bytes(
        review_bundle,
        canonical_json_bytes(document),
    )

    assert loaded is None
    assert any(
        diagnostic.code == "RECIPE_COVERAGE_MISMATCH" and diagnostic.pointer == "/payload/recipes"
        for diagnostic in diagnostics
    )


@pytest.mark.parametrize(
    ("path", "value", "pointer"),
    (
        (("schema_version",), "unsupported-schema", "/schema_version"),
        (("payload", "profile_class"), "UNSUPPORTED_CLASS", "/payload/profile_class"),
        (("payload", "acceptance", "status"), "NOT_ACCEPTED", "/payload/acceptance/status"),
        (("payload", "setups", 0, "raw_allowance_um"), 1, "/payload/setups"),
        (("payload", "setups", 0, "reference_surface"), "MACHINE_BED_Z0", "/payload/setups"),
        (("payload", "tools", 0, "drill_point_length_um"), 1, "/payload/tools"),
    ),
)
def test_validate_maps_unlabelled_semantic_failures_to_actionable_pointers(
    review_bundle: bytes,
    production_profile: bytes,
    path: tuple[str | int, ...],
    value: object,
    pointer: str,
) -> None:
    document = json.loads(production_profile)
    target: Any = document
    for token in path[:-1]:
        target = target[token]
    target[path[-1]] = value
    document["payload_sha256"] = sha256_hex(canonical_json_bytes(document["payload"]))

    loaded, diagnostics = authoring.validate_profile_bytes(
        review_bundle,
        canonical_json_bytes(document),
    )

    assert loaded is None
    assert diagnostics[0].code == "PROFILE_SEMANTIC_INVALID"
    assert diagnostics[0].pointer == pointer


def test_validate_requires_exact_linuxcnc_postgeneration_within_line_limit(
    review_bundle: bytes,
    production_profile: bytes,
) -> None:
    document = json.loads(production_profile)
    first_setup = document["payload"]["setups"][0]
    physical_sheet = (first_setup["stock_id"], first_setup["sheet_index"])
    for setup in document["payload"]["setups"]:
        if (setup["stock_id"], setup["sheet_index"]) == physical_sheet:
            setup["material_evidence_id"] = "m" * 240
    document["payload_sha256"] = sha256_hex(canonical_json_bytes(document["payload"]))

    loaded, diagnostics = authoring.validate_profile_bytes(
        review_bundle,
        canonical_json_bytes(document),
    )

    assert loaded is None
    assert diagnostics[0].code == "PROFILE_SEMANTIC_INVALID"
    assert "LinuxCNC file line limit" in diagnostics[0].message


def test_finalize_and_validate_bound_adversarial_json_nesting(
    review_bundle: bytes,
    production_profile: bytes,
) -> None:
    draft = _completed_draft(review_bundle)
    draft["payload"]["machine"]["controller_version"] = _nested_array(64)

    profile_bytes, diagnostics = authoring.finalize_profile_draft(
        review_bundle,
        json.dumps(draft).encode(),
    )

    assert profile_bytes is None
    assert diagnostics[0].code == "NESTING_TOO_DEEP"
    assert diagnostics[0].pointer.startswith("/payload/machine/controller_version")

    document = json.loads(production_profile)
    document["payload"]["machine"]["controller_version"] = _nested_array(64)
    malformed_profile = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    loaded, diagnostics = authoring.validate_profile_bytes(review_bundle, malformed_profile)

    assert loaded is None
    assert diagnostics[0].code == "NESTING_TOO_DEEP"
    assert diagnostics[0].pointer.startswith("/payload/machine/controller_version")


def test_machine_readable_contract_is_generated_from_live_closed_field_sets() -> None:
    contract_path = (
        Path(__file__).resolve().parents[2]
        / "packages/contracts/production-machine-profile.v1.schema.json"
    )
    contract = json.loads(contract_path.read_bytes())
    expected = authoring.production_profile_json_schema()
    postprocessor = contract["properties"]["payload"]["properties"]["postprocessor_profile"]

    assert contract == expected
    assert set(postprocessor["required"]) == {
        field.name for field in fields(LinuxCNCProductionMachineProfile)
    }
    assert set(postprocessor["properties"]) == set(postprocessor["required"])
    assert postprocessor["additionalProperties"] is False
    assert (
        postprocessor["properties"]["external_axis_offset_policy"]["const"]
        == EXTERNAL_AXIS_OFFSET_POLICY
    )
    assert (
        postprocessor["properties"]["continuous_spindle_speed_interlock_policy"]["const"]
        == CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY
    )
    assert (
        postprocessor["properties"]["metric_xyz_identity_kinematics_policy"]["const"]
        == METRIC_XYZ_IDENTITY_KINEMATICS_POLICY
    )
    assert postprocessor["properties"]["exactly_three_joints_verified"] == {"const": True}
    tool = contract["properties"]["payload"]["properties"]["tools"]["items"]
    assert tool["properties"]["controller_tool_number"]["maximum"] == (
        MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
    )
    assert tool["properties"]["length_offset_number"]["maximum"] == (
        MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
    )


def test_finalize_rejects_changed_derived_requirements(review_bundle: bytes) -> None:
    draft = _completed_draft(review_bundle)
    changed = deepcopy(draft)
    changed["requirements"]["design_hash"] = "0" * 64

    profile, diagnostics = authoring.finalize_profile_draft(
        review_bundle,
        json.dumps(changed).encode(),
    )

    assert profile is None
    assert any(
        diagnostic.code == "DERIVED_REQUIREMENTS_CHANGED" and diagnostic.pointer == "/requirements"
        for diagnostic in diagnostics
    )
