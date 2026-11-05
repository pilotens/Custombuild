from collections import Counter
from copy import deepcopy

import pytest
from custombuild_cad.sideboard_checks import audit_sideboard
from custombuild_domain.identity import content_hash
from custombuild_domain.measured_assembly import MeasuredAssembly, review_measured_assembly
from custombuild_domain.sideboard_construction import (
    SideboardConstruction,
    SideboardRequest,
    construct_sideboard,
)
from pydantic import ValidationError


def test_sideboard_accounts_for_every_piece_fitting_blank_and_article():
    package = construct_sideboard(SideboardRequest())
    assembly = package.assembly
    assert (len(assembly.parts), len(assembly.joints), len(package.fittings)) == (38, 90, 35)
    assert sum(len(p.operations) for p in assembly.parts) == 310
    assert assembly.fittings == package.fittings
    counts = Counter(j.article for j in assembly.joints)
    counts.update(a for f in package.fittings for a in (f.article, *f.accessories))
    assert counts == package.hardware
    assert counts["70T3750.TL"] == 2 and counts["70T3650.TL"] == 8
    assert counts["609.1500"] == 25 and counts["175H7100"] == 10
    assert package.construction_sha256 == content_hash(
        package.model_dump(mode="json", exclude={"construction_sha256"})
    )
    parts = {p.id: p for p in assembly.parts}
    connected = {end.part for j in assembly.joints for end in (j.a, j.b)}
    connected.update(key for f in package.fittings for key in f.connects)
    assert connected == set(parts)
    for blank in package.purchase_blanks:
        part = parts[blank.part_id]
        expected = [20000, 4000, 4000] if part.material == "Massiv valnöt" else [10000, 10000, 0]
        assert [
            p - f for p, f in zip(blank.purchase_min_lbt_um, blank.finished_lbt_um, strict=True)
        ] == expected
        assert not blank.supplier_stock_confirmed
        assert part.size_um["xyz".index(blank.grain_axis)] == blank.finished_lbt_um[0]
    report = review_measured_assembly(assembly)
    assert len(report.fitting_checks) == 35
    assert all(c.state == "unresolved" for c in report.fitting_checks)
    assert not package.physical_cutting_authorized


@pytest.mark.parametrize(
    "input_data",
    [
        {"width_mm": 0},
        {"width_mm": 2400.5},
        {"width_mm": True},
        {"door_count": 6},
        {"material": "solid-walnut"},
        {"height_mm": 650, "leg_height_mm": 220},
        {"invented_load_rating": 100},
    ],
)
def test_invalid_or_unsupported_requests_are_rejected(input_data):
    with pytest.raises(ValidationError):
        SideboardRequest.model_validate(input_data)


def test_empty_fittings_preserve_previous_assembly_identity():
    from custombuild_domain.trestle_construction import construct_trestle
    from custombuild_domain.trestle_planning import TrestleRequest

    data = construct_trestle(TrestleRequest()).assembly.model_dump(mode="json")
    assert "fittings" not in data
    assert MeasuredAssembly.model_validate({**data, "fittings": []}).model_dump(mode="json") == data


@pytest.fixture(scope="module")
def native_sideboard():
    package = construct_sideboard(SideboardRequest())
    audit, _ = audit_sideboard(package)
    assert audit["wood_and_boring_checks_passed"], audit
    return package, audit


@pytest.mark.cad
def test_native_geometry_covers_all_wood_and_hardware_borings(native_sideboard):
    _, audit = native_sideboard
    assert audit["pairs_checked"] == 703 and audit["connected_parts"] == 38
    assert len(audit["joints"]) == 90 and len(audit["fittings"]) == 35
    assert len(audit["back_coverage"]) == 5
    assert all(c["covered_mm2"] == pytest.approx(c["required_mm2"]) for c in audit["back_coverage"])
    assert not audit["hinge_motion_verified"] and not audit["load_rating_verified"]
    assert not audit["hardware_body_collision_verified"]


@pytest.mark.cad
@pytest.mark.parametrize(
    "fault", ["leg_dowel_collision", "latch_pilot", "shelf_web", "hinge_plate"]
)
def test_actual_failures_are_detected_and_repaired(native_sideboard, fault):
    original, _ = native_sideboard
    bad = deepcopy(original.model_dump(mode="json"))
    parts = {p["id"]: p for p in bad["assembly"]["parts"]}
    if fault == "leg_dowel_collision":
        j = next(
            j
            for j in bad["assembly"]["joints"]
            if j["kind"] == "screw" and j["b"]["part"].startswith("LEG")
        )
        for end in (j["a"], j["b"]):
            p = parts[end["part"]]
            for op in p["operations"]:
                if op["id"] in (end["operation"], end["operation"] + "-HEAD"):
                    op["v_um"] = parts[j["b"]["part"]]["origin_um"][1] + 25000 - p["origin_um"][1]
    elif fault == "latch_pilot":
        j = next(j for j in bad["assembly"]["joints"] if j["a"]["part"].startswith("LATCH-BLOCK"))
        next(o for o in parts["TOP"]["operations"] if o["id"] == j["b"]["operation"])[
            "depth_um"
        ] = 18000
    elif fault == "shelf_web":
        for op in parts["DIV-1"]["operations"]:
            if op["id"].startswith("PIN-"):
                op["depth_um"] = 10000
    else:
        f = next(f for f in bad["fittings"] if f["kind"] == "hinge")
        mount = f["mounts"][-1]
        next(o for o in parts[mount["part"]]["operations"] if o["id"] == mount["operation"])[
            "u_um"
        ] += 1000
    failed, _ = audit_sideboard(SideboardConstruction.model_validate(bad))
    assert not failed["wood_and_boring_checks_passed"], fault
    repaired, _ = audit_sideboard(original)
    assert repaired["wood_and_boring_checks_passed"], repaired
