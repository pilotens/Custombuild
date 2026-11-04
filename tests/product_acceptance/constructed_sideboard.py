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
    if case.level == "easy":
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
        e.note("boundary_sizes", "1800/400/650 och 2600/550/850 mm har full CAD-passning.")
        return
    audit, _ = audit_sideboard(package)
    assert audit["wood_and_boring_checks_passed"], audit
    assert not audit["physical_cutting_authorized"] and not audit["hinge_motion_verified"]
    e.equal("wood_pairs", audit["pairs_checked"], 703)
    if case.level == "hard":
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

        files = sideboard_files(package)
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
