from collections import Counter
from math import hypot

import pytest
from custombuild_domain.identity import content_hash
from custombuild_domain.measured_assembly import (
    MeasuredAssembly,
    measured_assembly_hash,
    review_measured_assembly,
)
from custombuild_domain.trestle_construction import construct_trestle
from custombuild_domain.trestle_planning import TrestleRequest
from pydantic import ValidationError


def test_all_parts_have_grain_allowance_operations_and_bidirectional_connections():
    construction = construct_trestle(TrestleRequest())
    work = construction.assembly
    assert len(work.parts) == 13
    assert len(work.joints) == 29
    assert sum(len(p.operations) for p in work.parts) == 79
    assert construction.assembly_sha256 == measured_assembly_hash(work)
    assert construction.assembly_sha256 == review_measured_assembly(work).assembly_sha256
    assert construction.construction_sha256 == content_hash(
        construction.model_dump(mode="json", exclude={"construction_sha256"})
    )
    assert all(p.operations for p in work.parts)
    assert {end.part for j in work.joints for end in (j.a, j.b)} == {p.id for p in work.parts}
    counts = Counter(a for j in work.joints for a in (j.article, *j.fittings))
    assert {h.specification: h.quantity for h in construction.hardware} == counts
    assert counts["WASHER-8.4x24x2"] == 48
    assert counts["HEX-NUT-M8-6.5"] == 24
    for blank in construction.purchase_blanks:
        assert [
            p - f
            for p, f in zip(blank.purchase_min_lbt_um, blank.finished_blank_lbt_um, strict=True)
        ] == [20000, 4000, 4000]
        assert not blank.supplier_stock_confirmed
    assert construction == construct_trestle(TrestleRequest())


@pytest.mark.parametrize(
    "length,width,height,load",
    [
        (1800, 850, 700, 100),
        (2200, 950, 750, 300),
        (2600, 1100, 800, 1500),
    ],
)
def test_sizing_and_sloped_stock_are_derived_from_dimensions(length, width, height, load):
    package = construct_trestle(
        TrestleRequest(length_mm=length, width_mm=width, height_mm=height, central_load_n=load)
    )
    assert package.sizing.predicted_deflection_mm <= package.sizing.deflection_target_mm
    top = package.assembly.parts[0]
    assert top.size_um[:2] == (length * 1000, width * 1000)
    assert top.origin_um[2] + top.size_um[2] == height * 1000
    leg = package.assembly.parts[1]
    rise = leg.size_um[2]
    shift = rise / 8
    projected_length = hypot(rise, shift) + 70000 * shift / hypot(rise, shift)
    assert projected_length <= leg.blank_lbt_um[0] < projected_length + 1000
    assert not package.sizing.load_rating_verified


def test_higher_requested_load_selects_a_thicker_top_and_regenerates_mates():
    light = construct_trestle(TrestleRequest(central_load_n=100))
    heavy = construct_trestle(TrestleRequest(central_load_n=1500))
    assert heavy.sizing.top_thickness_mm > light.sizing.top_thickness_mm
    assert light.assembly_sha256 != heavy.assembly_sha256
    assert light.assembly.parts[1].size_um[2] > heavy.assembly.parts[1].size_um[2]
    for package in (light, heavy):
        assert all(j.length_um == 45000 for j in package.assembly.joints if j.kind == "screw")
        assert all(o.depth_um == 24000 for o in package.assembly.parts[0].operations)


def test_movement_uses_full_moisture_interval_and_independent_datum():
    package = construct_trestle(TrestleRequest(moisture_min_pct=6, moisture_max_pct=14))
    sizing = package.sizing
    assert sizing.width_change_mm == pytest.approx(950 * 0.00274 * 8)
    assert (sizing.mounting_slot_length_mm - 9) / 2 >= 150 * 0.00274 * 8 + 1
    mounts = [j for j in package.assembly.joints if j.kind == "screw"]
    parts = {p.id: p for p in package.assembly.parts}
    shapes = [
        next(o.shape for o in parts[j.a.part].operations if o.id == j.a.operation) for j in mounts
    ]
    assert Counter(shapes) == {"round_slot": 4, "drill": 1}


def test_dimension_confirmation_does_not_fabricate_load_or_workshop_acceptance():
    a = construct_trestle(TrestleRequest())
    b = construct_trestle(TrestleRequest(dimensions_confirmed=True))
    assert len(a.outstanding_acceptance) == len(b.outstanding_acceptance) + 1
    assert not b.physical_cutting_authorized
    assert not b.sizing.load_rating_verified
    report = review_measured_assembly(b.assembly)
    assert all(j.state == "unresolved" for j in report.joints)


@pytest.mark.parametrize(
    "change",
    [
        dict(length_mm=1799),
        dict(width_mm=1101),
        dict(height_mm=True),
        dict(central_load_n=0),
        dict(length_mm=2200.1),
        dict(material="unknown"),
        dict(moisture_min_pct=12, moisture_max_pct=12),
        dict(moisture_max_pct=15),
    ],
)
def test_unsupported_inputs_fail_without_silent_clamping(change):
    with pytest.raises(ValidationError):
        construct_trestle(TrestleRequest(**change))


def test_roundtrip_preserves_every_connection_and_operation():
    a = construct_trestle(TrestleRequest()).assembly
    assert MeasuredAssembly.model_validate_json(a.model_dump_json()) == a


def test_full_construction_identity_changes_even_when_load_keeps_the_same_geometry():
    a = construct_trestle(TrestleRequest(central_load_n=300))
    b = construct_trestle(TrestleRequest(central_load_n=301))
    assert a.assembly_sha256 == b.assembly_sha256
    assert a.construction_sha256 != b.construction_sha256
