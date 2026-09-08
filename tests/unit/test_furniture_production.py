from __future__ import annotations

from copy import deepcopy

import pytest
from app.design_service import normalize_preview
from app.furniture_production import furniture_production_input
from custombuild_domain import build_bookcase
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_domain.furniture_production import (
    FurnitureProductionSource,
    assert_furniture_production_spec,
    furniture_production_source,
    shelving_production_spec,
)

from tests.unit.test_furniture_families import workspace


@pytest.mark.parametrize("thickness", [17_001, 17_801, 18_000, 18_999])
@pytest.mark.parametrize("material,back", [("mdf", "birch-plywood-6"), ("birch-plywood", "mdf-6")])
@pytest.mark.parametrize("load", [0, 201, 4903])
def test_bridge_preserves_exact_parts_features_joints_and_loads(thickness, material, back, load):
    value = workspace(
        "shelving",
        width_um=900_001,
        height_um=1_800_007,
        depth_um=320_003,
        shelf_load_n=load,
        shelf_height_ratios_ppm=(100_001, 300_002, 600_003, 800_004),
    ).model_dump(mode="json")
    value["design"]["material"].update(
        material_id=material, measured_thickness_um=thickness, batch_id="measured-batch-1"
    )
    value["design"]["back_material"].update(material_id=back, measured_thickness_um=6_001)
    selected = FurnitureWorkspace.model_validate(value)
    source = furniture_production_source(selected)
    api_input = furniture_production_input(selected)
    actual = normalize_preview(
        api_input.model_dump(exclude_none=True), design_id=selected.design.design_id, revision=9
    )
    assert_furniture_production_spec(source, actual)
    original = build_furniture(selected.design).shelving_result
    assert original is not None
    assert actual.parameters == original.spec.parameters
    assert actual.material == original.spec.material
    assert actual.back_material == original.spec.back_material
    assert actual.parameters.shelf_load_n == load
    assert actual.parameters.actual_thickness_um == thickness
    rebuilt = build_bookcase(actual.model_copy(update={"revision": original.spec.revision}))
    assert rebuilt.parts == original.parts
    assert rebuilt.joints == original.joints
    assert rebuilt.assembly_graph == original.assembly_graph


@pytest.mark.parametrize("family", ["table", "chest_of_drawers"])
def test_layout_families_cannot_enter_the_shelving_production_compiler(family):
    with pytest.raises(ValueError, match="endast hyllsystem"):
        shelving_production_spec(workspace(family))


@pytest.mark.parametrize("field", ["workspace_sha256", "furniture_design_hash"])
def test_source_rejects_changed_identity(field):
    source = furniture_production_source(workspace("shelving")).model_dump(mode="json")
    source[field] = "0" * 64
    with pytest.raises(ValueError):
        FurnitureProductionSource.model_validate(source)


def test_batch_only_change_preserves_parts_but_changes_production_source():
    first = workspace("shelving")
    changed = first.model_dump(mode="json")
    changed["design"]["material"]["batch_id"] = "another-batch"
    second = FurnitureWorkspace.model_validate(changed)
    assert build_furniture(first.design).parts == build_furniture(second.design).parts
    assert (
        furniture_production_source(first).workspace_sha256
        != furniture_production_source(second).workspace_sha256
    )


def test_frozen_source_rejects_changed_thickness_or_model_defaults():
    selected = workspace("shelving")
    source = furniture_production_source(selected)
    spec = shelving_production_spec(selected)
    for patch in (
        {"actual_thickness_um": 17_800},
        {"shelf_load_n": 1},
        {"plinth_height_um": 80_000},
    ):
        candidate = deepcopy(spec.model_dump(mode="json"))
        candidate["parameters"].update(patch)
        with pytest.raises(ValueError, match="production inputs differ"):
            assert_furniture_production_spec(source, type(spec).model_validate(candidate))


def test_real_cad_package_carries_and_verifies_its_furniture_source():
    import io
    import json
    import zipfile
    from dataclasses import replace

    from custombuild_manufacturing import (
        ArtifactFile,
        build_production_bundle,
        canonical_json_bytes,
        read_and_verify_package,
    )
    from custombuild_manufacturing.errors import ArtifactError

    from tests.unit.test_production_bundle import design_and_request

    selected = workspace(
        "shelving", width_um=700_001, height_um=1_000_003, divider_count=0, shelf_count=2
    )
    source = furniture_production_source(selected)
    design = build_bookcase(shelving_production_spec(selected))
    _, machine, original_stock, context = design_and_request()
    stock = tuple(
        replace(s, material_id=m.material_id, material_version=m.version)
        for s, m in zip(
            original_stock, (design.spec.material, design.spec.back_material), strict=True
        )
    )
    context = replace(context, project_id=design.spec.design_id, design_hash=design.design_hash)
    artifact = ArtifactFile(
        "design/furniture-source.json",
        canonical_json_bytes(source),
        "application/json",
        "FURNITURE_PRODUCTION_SOURCE",
    )
    bundle = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        allow_blocked_cam=True,
        additional_artifacts=(artifact,),
    )
    verified = read_and_verify_package(bundle.zip_bytes)
    assert not verified["physical_cutting_authorized"]
    assert bundle.operations is None
    with zipfile.ZipFile(io.BytesIO(bundle.zip_bytes)) as archive:
        assert json.loads(archive.read(artifact.path)) == source.model_dump(mode="json")
        assert archive.read("model/design.step").startswith(b"ISO-10303-21;")

    # A checksummed source from another valid design is still not this design.
    wrong = furniture_production_source(workspace("shelving", width_um=800_000))
    wrong_artifact = replace(artifact, data=canonical_json_bytes(wrong))
    with pytest.raises((ArtifactError, ValueError), match="furniture source"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            allow_blocked_cam=True,
            additional_artifacts=(wrong_artifact,),
        )
