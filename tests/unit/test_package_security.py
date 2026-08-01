from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace

import pytest
from custombuild_cad import CADDependencyUnavailable
from custombuild_domain import (
    BookcaseDesignSpec,
    BookcaseParameters,
    build_bookcase,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_manufacturing import (
    ArtifactFile,
    ManifestContext,
    ProductionBlockedError,
    StockSheet,
    build_deterministic_zip,
    build_production_bundle,
    linuxcnc_reference_router_1325,
    read_and_verify_package,
)
from custombuild_manufacturing.errors import ArtifactError
from custombuild_manufacturing.pipeline import CadQueryAdapter


def package_fixture():
    context = ManifestContext(
        "project",
        "1",
        "a" * 64,
        "app-1",
        "engine-1",
        "template-1",
        "rules-1",
        ("mdf@1",),
        "joints-1",
        "machine",
        "1",
        "none",
        "NOT_REQUESTED",
        "f" * 64,
        {"schema_version": "test-production-context.v1"},
    )
    artifacts = (ArtifactFile("data/file.txt", b"payload", "text/plain", "TEST"),)
    return context, artifacts, build_deterministic_zip(context, artifacts)


def zip_entries(payload: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def make_zip(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


@pytest.mark.parametrize("path", ("", "../x", "/abs", "a\\b", "a/./b", "a//b", "x\x00y"))
def test_artifact_paths_reject_ambiguous_or_escaping_names(path: str) -> None:
    with pytest.raises(ArtifactError):
        ArtifactFile(path, b"x", "text/plain", "TEST")


def test_duplicate_artifact_paths_and_missing_release_files_are_blocked() -> None:
    context, artifacts, _ = package_fixture()
    with pytest.raises(ArtifactError, match="duplicate"):
        build_deterministic_zip(context, artifacts + artifacts)
    with pytest.raises(ProductionBlockedError, match="genuine STEP"):
        build_deterministic_zip(context, artifacts, production_release=True)


def test_package_reader_rejects_corruption_missing_manifest_and_unsafe_entries() -> None:
    with pytest.raises(ArtifactError, match="invalid production ZIP"):
        read_and_verify_package(b"not-a-zip")
    with pytest.raises(ArtifactError, match="manifest"):
        read_and_verify_package(make_zip([("file.txt", b"x")]))
    with pytest.raises(ArtifactError, match="unsafe artifact path"):
        read_and_verify_package(make_zip([("../manifest.json", b"{}")]))
    with pytest.raises(ArtifactError, match="directory"):
        read_and_verify_package(make_zip([("folder/", b""), ("manifest.json", b"{}")]))
    bomb = make_zip([("manifest.json", b"0" * 2_000_000)])
    with pytest.raises(ArtifactError, match="compression ratio"):
        read_and_verify_package(bomb)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda manifest: b"not-json", "valid manifest"),
        (lambda manifest: b"[]", "JSON object"),
        (
            lambda manifest: json.dumps({**manifest, "schema_version": "wrong"}).encode(),
            "schema",
        ),
        (
            lambda manifest: json.dumps({**manifest, "artifacts": {}}).encode(),
            "must be an array",
        ),
        (
            lambda manifest: json.dumps({**manifest, "artifacts": ["bad"]}).encode(),
            "must be an object",
        ),
        (
            lambda manifest: json.dumps(
                {**manifest, "artifacts": [{**manifest["artifacts"][0], "path": 1}]}
            ).encode(),
            "path must be a string",
        ),
        (
            lambda manifest: json.dumps({**manifest, "project_id": "tampered"}).encode(),
            "context_hash mismatch",
        ),
        (
            lambda manifest: json.dumps(
                {**manifest, "artifacts": manifest["artifacts"] * 2}
            ).encode(),
            "duplicate artifact paths",
        ),
    ),
)
def test_package_reader_validates_manifest_structure_and_context(mutation, message: str) -> None:
    _, _, payload = package_fixture()
    entries = zip_entries(payload)
    manifest = json.loads(entries["manifest.json"])
    entries["manifest.json"] = mutation(manifest)

    with pytest.raises(ArtifactError, match=message):
        read_and_verify_package(make_zip(list(entries.items())))


def test_package_reader_rejects_checksum_missing_and_unlisted_files() -> None:
    _, _, payload = package_fixture()
    entries = zip_entries(payload)
    tampered = {**entries, "data/file.txt": b"changed"}
    with pytest.raises(ArtifactError, match="checksum mismatch"):
        read_and_verify_package(make_zip(list(tampered.items())))

    missing = {name: data for name, data in entries.items() if name != "data/file.txt"}
    with pytest.raises(ArtifactError, match="missing from ZIP"):
        read_and_verify_package(make_zip(list(missing.items())))

    unlisted = {**entries, "extra.txt": b"extra"}
    with pytest.raises(ArtifactError, match="outside the manifest"):
        read_and_verify_package(make_zip(list(unlisted.items())))


def production_request():
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="pipeline-errors",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    machine = linuxcnc_reference_router_1325()
    stock = (
        StockSheet(
            "mdf18",
            design.spec.material.material_id,
            design.spec.material.version,
            2_440_000,
            1_220_000,
            18_000,
            quantity=2,
            grain_direction="X",
        ),
        StockSheet(
            "mdf6",
            design.spec.back_material.material_id,
            design.spec.back_material.version,
            2_440_000,
            1_220_000,
            6_000,
            grain_direction="X",
        ),
    )
    context = ManifestContext(
        "project",
        "1",
        design.design_hash,
        "app",
        "engine",
        "template",
        "rules",
        (),
        "joints",
        machine.profile_id,
        machine.version,
        "none",
        "NOT_REQUESTED",
        "f" * 64,
        {"schema_version": "test-production-context.v1"},
    )
    return design, machine, stock, context


def test_pipeline_rejects_context_stock_and_capacity_mismatches() -> None:
    design, machine, stock, context = production_request()
    with pytest.raises(ProductionBlockedError, match="design_hash"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=replace(context, design_hash="b" * 64),
            include_step=False,
        )
    with pytest.raises(ProductionBlockedError, match="machine profile"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=replace(context, machine_profile_version="wrong"),
            include_step=False,
        )
    with pytest.raises(ProductionBlockedError, match="at least one stock"):
        build_production_bundle(
            design, stock=(), machine=machine, context=context, include_step=False
        )
    with pytest.raises(ProductionBlockedError, match="stock_id"):
        build_production_bundle(
            design,
            stock=(stock[0], stock[0]),
            machine=machine,
            context=context,
            include_step=False,
        )
    with pytest.raises(ProductionBlockedError, match="STOCK_PROFILE_MISSING"):
        build_production_bundle(
            design,
            stock=(stock[0],),
            machine=machine,
            context=context,
            include_step=False,
        )
    with pytest.raises(ProductionBlockedError, match="NESTING_UNPLACED"):
        build_production_bundle(
            design,
            stock=(replace(stock[0], quantity=1), stock[1]),
            machine=machine,
            context=context,
            include_step=False,
        )


def test_pipeline_cad_failure_and_no_program_branch_are_explicit(monkeypatch) -> None:
    design, machine, stock, context = production_request()

    def fail_export(self, value):
        raise CADDependencyUnavailable("missing kernel")

    monkeypatch.setattr(CadQueryAdapter, "export_design", fail_export)
    with pytest.raises(ProductionBlockedError, match="authoritative CAD"):
        build_production_bundle(
            design, stock=stock, machine=machine, context=context, include_step=True
        )

    bundle = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=False,
        include_validation_program=False,
    )
    assert bundle.manifest["postprocessor_version"] == "none"
    assert not any(artifact.path.endswith(".ngc") for artifact in bundle.artifacts)
