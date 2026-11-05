import json
from copy import deepcopy
from io import StringIO

from custombuild_cad.sideboard_checks import audit_sideboard
from custombuild_domain.sideboard_construction import SideboardConstruction
from custombuild_worker.sideboard_review import sideboard_files

from .support import response_json


def exercise_constructed_sideboard(client, case, e, tmp_path):
    path = "/v1/constructions/sideboard"
    before = response_json(client.post(path, json={}))
    package = SideboardConstruction.model_validate(before)
    plan = {"front_percent": [21, 17, 23, 20, 19], "review": {}}
    if case.level == "easy":
        assessed = response_json(client.post(path, json=plan))
        e.equal(
            "door_motion_without_supplier_path",
            [r["state"] for r in assessed["assessment"]["door_motion"]],
            ["missing_path"] * 5,
        )
        e.equal(
            "parts_joints_fittings_operations",
            [
                len(package.assembly.parts),
                len(package.assembly.joints),
                len(package.fittings),
                sum(len(p.operations) for p in package.assembly.parts),
            ],
            [38, 90, 35, 310],
        )
        return
    if case.level == "impossible":
        for invalid in (
            {"front_percent": [21, 17, 23, 21, 19]},
            {"review": {"hinge_paths": [{"article": "70T3750.TL", "poses": []}]}},
            {"width_mm": 0},
            {"door_count": 6},
            {"height_mm": 650, "leg_height_mm": 220},
            {"material": "solid-walnut"},
        ):
            response_json(client.post(path, json=invalid), 422)
        assert response_json(client.post(path, json={})) == before
        e.note("rejection", "Nollbredd, sex luckor, för låg stomme och fel material avvisas.")
        return
    if case.level == "medium":
        for dimensions in (
            dict(width_mm=1800, depth_mm=400, height_mm=650),
            dict(width_mm=2600, depth_mm=550, height_mm=850, leg_height_mm=220),
        ):
            sample = SideboardConstruction.model_validate(
                response_json(client.post(path, json=dimensions))
            )
            audit, _ = audit_sideboard(sample)
            assert audit["wood_and_boring_checks_passed"], audit
        layout = SideboardConstruction.model_validate(response_json(client.post(path, json=plan)))
        assert audit_sideboard(layout)[0]["wood_and_boring_checks_passed"]
        e.equal(
            "front_widths_um",
            [p.size_um[0] for p in layout.assembly.parts if p.id.startswith("DOOR-")],
            [490140, 396780, 536820, 466800, 443460],
        )
        e.note("boundary_sizes", "1800/400/650 och 2600/550/850 mm har full CAD-passning.")
        return
    audit, _ = audit_sideboard(package)
    assert audit["wood_and_boring_checks_passed"], audit
    assert not audit["physical_cutting_authorized"] and not audit["hinge_motion_verified"]
    e.equal("wood_pairs", audit["pairs_checked"], 703)
    if case.level == "hard":
        failed_plan = response_json(
            client.post(path, json={**plan, "review": {"edge_error_mm": 1.2}})
        )
        assert all(g["state"] == "insufficient" for g in failed_plan["assessment"]["hinge_gaps"])
        fixed = response_json(client.post(path, json=plan))
        assert all(g["state"] == "within_table" for g in fixed["assessment"]["hinge_gaps"])
        e.note(
            "gap_repair", "Kantfel ±1,2 mm stoppar springkravet; ±0,2 mm ger godkänt katalogprov."
        )
        bad = deepcopy(before)
        back = next(p for p in bad["assembly"]["parts"] if p["id"] == "BACK-3")
        back["size_um"][2] = 150000
        back["operations"] = []
        failed, _ = audit_sideboard(SideboardConstruction.model_validate(bad))
        assert not failed["wood_and_boring_checks_passed"]
        assert any(not b["passed"] for b in failed["back_coverage"])
        assert audit_sideboard(package)[0]["wood_and_boring_checks_passed"]
        e.note(
            "repair", "Rygg ersatt med 150 mm remsa: täckningsfel fångat; full rygg godkänd igen."
        )
    if case.level == "very-hard":
        import cadquery as cq
        import ezdxf

        package = SideboardConstruction.model_validate(response_json(client.post(path, json=plan)))
        files = sideboard_files(package)
        report = json.loads(files["validation/planning.json"])
        assert report["closed_stability"]["positive_reserve"]
        assert report["assessment"]["total_shelf_payload_kg"] == 100
        assert sum(p.endswith(".dxf") for p in files) == 228
        drawing = ezdxf.read(StringIO(files["parts/DOOR-1/back.dxf"].decode()))
        assert drawing.units == 4
        cups = {
            (c.dxf.center.x, c.dxf.center.y)
            for c in drawing.modelspace().query("CIRCLE")
            if c.dxf.radius == 17.5
        }
        assert cups == {(24.5, 80), (24.5, 436)}
        step = tmp_path / "door.step"
        step.write_bytes(files["parts/DOOR-1/DOOR-1.step"])
        restored = cq.importers.importStep(str(step)).val()
        for z in (287, 643):
            hole = cq.Solid.makeCylinder(17.5, 13, cq.Vector(51.5, 19, z), cq.Vector(0, -1, 0))
            assert restored.intersect(hole).Volume() < 1e-6
            skin = cq.Solid.makeCylinder(10, 5, cq.Vector(51.5, 5, z), cq.Vector(0, -1, 0))
            assert restored.intersect(skin).Volume() > 1500
        e.equal(
            "STEP_parts", sum(p.startswith("parts/") and p.endswith(".step") for p in files), 38
        )
        e.equal("DXF_face_files", 228, 228)
        e.note(
            "independent_cup_positions",
            "Ø35 x 13; centrum 24,5/80 och 24,5/436 mm. Frontskikt kvar.",
        )
