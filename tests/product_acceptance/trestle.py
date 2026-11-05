from copy import deepcopy
from itertools import combinations
from math import pi

import pytest
from custombuild_cad.measured_reference import reference_intersections, reference_solids
from custombuild_domain.measured_assembly import MeasuredAssembly

from .support import response_json


def exercise_trestle(client, case, e, tmp_path):
    work = response_json(client.get("/v1/measured-assemblies/examples/trestle-table"))
    path = "/v1/measured-assemblies/review"
    before = response_json(client.post(path, json=work))
    e.equal(
        "parts_profiles", [len(work["parts"]), len(before["unreviewed_profile_parts"])], [13, 9]
    )
    assert before["physical_cutting_authorized"] is False
    assert len(before["issues"]) == 13 and not before["joints"]
    if case.level == "easy":
        e.equal("provisional_table_um", work["parts"][0]["size_um"], [2200000, 950000, 32000])
        assert {s["state"] for s in before["stock_checks"]} == {"unresolved"}
        return
    if case.level == "impossible":
        impossible = deepcopy(work)
        impossible["parts"][0]["profile"]["radius_um"] = 475001
        response_json(client.post(path, json=impossible), 422)
        e.note("rejection", "Hörnradien är 1 µm över halva skivbredden.")
        assert response_json(client.post(path, json=work)) == before
        return
    solids = reference_solids(MeasuredAssembly.model_validate(work))
    # Independent analytical volumes, not values copied from the CAD adapter.
    for key, solid in solids.items():
        assert solid.isValid()
        if key == "TOP":
            expected = (2200 * 950 - (4 - pi) * 60**2) * 32
        elif key.startswith("LEG"):
            expected = 70 * 70 * 640
        elif key.startswith("BRACE"):
            expected = 40 * 80 * 400
        elif key.startswith("CROSS"):
            expected = 70 * 670 * 78 if key.endswith("TOP") else 70 * (560 + 540) / 2 * 80
        else:
            expected = 1560 * 50 * (78 if key.endswith("TOP") else 80)
        assert solid.Volume() == pytest.approx(expected, abs=1e-5, rel=1e-10)
    assert not reference_intersections(solids)
    e.equal("reference_pairs_checked", len(list(combinations(solids, 2))), 78)
    assert solids["LEG-L-F"].BoundingBox().zmin == pytest.approx(0, abs=1e-6)
    assert solids["TOP"].BoundingBox().zmax == pytest.approx(750, abs=1e-6)
    if case.level == "hard":
        shifted = deepcopy(work)
        shifted["parts"][4]["origin_um"][1] -= 1000
        collisions = reference_intersections(
            reference_solids(MeasuredAssembly.model_validate(shifted))
        )
        assert any(set(c["parts"]) == {"CROSS-L-LOW", "LEG-L-F"} for c in collisions)
        e.note("reference_collision", collisions)
        assert not reference_intersections(reference_solids(MeasuredAssembly.model_validate(work)))
    if case.level == "very-hard":
        import cadquery as cq

        for key, solid in solids.items():
            output = tmp_path / (key + ".step")
            cq.exporters.export(solid, str(output))
            restored = cq.importers.importStep(str(output)).val()
            assert len(restored.Solids()) == 1
            assert restored.Volume() == pytest.approx(solid.Volume(), rel=1e-8)
            for axis in ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax"):
                assert (
                    abs(getattr(restored.BoundingBox(), axis) - getattr(solid.BoundingBox(), axis))
                    < 0.005
                )
        short = deepcopy(work)
        short["parts"][11]["stock_lbt_um"] = [584999, 61000, 40000]
        checked = response_json(client.post(path, json=short))
        assert (
            next(s for s in checked["stock_checks"] if s["part"] == "BRACE-L")["state"] == "blocked"
        )
        short["parts"][11]["stock_lbt_um"][0] += 1
        checked = response_json(client.post(path, json=short))
        assert next(s for s in checked["stock_checks"] if s["part"] == "BRACE-L")["state"] == "fits"
        e.equal("reference_STEP_parts", len(solids), 13)
        e.equal("offset_rejected_um", 1, 1)
