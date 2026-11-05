from copy import deepcopy
from io import StringIO

from custombuild_cad.measured_machining import audit_machined_assembly
from custombuild_domain.measured_assembly import MeasuredAssembly
from custombuild_domain.trestle_construction import TrestleConstruction
from custombuild_worker.trestle_review import construction_files

from .support import response_json


def exercise_constructed_trestle(client, case, e, tmp_path):
    path = "/v1/constructions/trestle-table"
    before = response_json(client.post(path, json={}))
    package = TrestleConstruction.model_validate(before)
    assert not package.physical_cutting_authorized and not package.sizing.load_rating_verified
    if case.level == "easy":
        e.equal(
            "parts_joints_operations",
            [
                len(package.assembly.parts),
                len(package.assembly.joints),
                sum(len(p.operations) for p in package.assembly.parts),
            ],
            [13, 29, 79],
        )
        assert all(p.operations for p in package.assembly.parts)
        e.equal("automatically_selected_top_mm", package.sizing.top_thickness_mm, 36)
        return
    if case.level == "impossible":
        response_json(client.post(path, json={"central_load_n": 100000}), 422)
        response_json(client.post(path, json={"width_mm": 0}), 422)
        assert response_json(client.post(path, json={})) == before
        e.note(
            "rejection", "100000 N och noll bredd avvisas; återställning ger samma konstruktion."
        )
        return
    if case.level == "medium":
        package = TrestleConstruction.model_validate(
            response_json(
                client.post(
                    path,
                    json={
                        "length_mm": 2600,
                        "width_mm": 1100,
                        "height_mm": 800,
                        "central_load_n": 1500,
                    },
                )
            )
        )
        assert package.sizing.top_thickness_mm > before["sizing"]["top_thickness_mm"]
        assert package.sizing.predicted_deflection_mm <= package.sizing.deflection_target_mm
    audit, _ = audit_machined_assembly(package.assembly)
    assert audit["geometric_checks_passed"], audit
    e.equal("matching_fasteners", sum(j["passed"] for j in audit["joints"]), 29)
    e.equal("connected_parts", audit["connected_parts"], 13)
    if case.level == "hard":
        bad = deepcopy(before["assembly"])
        bad["parts"][1]["origin_um"][0] += 1000
        failed, _ = audit_machined_assembly(MeasuredAssembly.model_validate(bad))
        assert not failed["geometric_checks_passed"] and failed["wood_intersections"]
        assert audit_machined_assembly(package.assembly)[0]["geometric_checks_passed"]
        e.note(
            "repair",
            "Benet flyttas 1 mm in i tvärbalken. Kollision upptäcks; återställning godkänd.",
        )
    if case.level == "very-hard":
        import cadquery as cq
        import ezdxf

        files = construction_files(package)
        assert sum(p.endswith(".dxf") for p in files) == 78
        drawing = ezdxf.read(StringIO(files["parts/TOP/bottom.dxf"].decode()))
        assert drawing.units == 4
        holes = list(drawing.modelspace().query("CIRCLE"))
        expected = {
            (o.u_um / 1000, o.v_um / 1000, 1.75) for o in package.assembly.parts[0].operations
        }
        assert {(c.dxf.center.x, c.dxf.center.y, c.dxf.radius) for c in holes} == expected
        step = tmp_path / "top.step"
        step.write_bytes(files["parts/TOP/TOP.step"])
        restored = cq.importers.importStep(str(step)).val()
        top = package.assembly.parts[0]
        for x, y, radius in expected:
            bore = cq.Solid.makeCylinder(radius, 24, cq.Vector(x, y, top.origin_um[2] / 1000))
            assert restored.intersect(bore).Volume() < 1e-6
        e.equal("STEP_with_actual_pilots", len(holes), 5)
        e.equal("DXF_face_files", 78, 78)
