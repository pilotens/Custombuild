"""Acceptance cases for the machine-neutral shelving trial, independent of CAM."""

import csv
import io
import json
from collections import Counter
from zipfile import ZipFile

import pytest
from custombuild_manufacturing.quality import validate_json_schema_instance
from custombuild_worker.furniture_review import build_furniture_review

from tests.unit.test_bookcase_angles import ARTICLE, trial


@pytest.mark.cad
def test_package_matches_independent_corner_geometry_and_hardware_inventory(tmp_path):
    cq = pytest.importorskip("cadquery")
    ezdxf = pytest.importorskip("ezdxf")
    # One bay with one shelf: 6 accessible corners and one captured back panel.
    w = trial(material="solid-walnut", divider_count=0, shelf_count=1)
    assert w.manufacturing is None
    with ZipFile(io.BytesIO(build_furniture_review(w))) as z:
        manifest = json.loads(z.read("manifest.json"))
        model = json.loads(z.read("design/resolved.json"))
        intent = json.loads(z.read("manufacturing/manufacturing-intent.json"))
        validate_json_schema_instance(
            intent, json.loads(z.read("schemas/manufacturing-intent.schema.json"))
        )
        graph = json.loads(z.read("assembly/graph.json"))
        assert (
            graph["design_hash"]
            == intent["document_identity"]["design_hash"]
            == manifest["design_hash"]
        )
        space = json.loads(z.read("assembly/required-space.json"))
        assert space["design_hash"] == manifest["design_hash"]
        assert space["revision"] == manifest["revision"]
        site = space["standing_site"]
        assert site["state"] == "requires_measurement"
        assert site["minimum_height_from_base_um"] == space["world_max_um"][2]
        handoff = json.loads(z.read("manufacturing/workshop-handoff.json"))
        assert site == handoff["dimensions"]["standing_assembly"]
        assert site["issues"][0]["code"] == "STANDING_SITE_UNMEASURED"
        assert site["issues"][0]["message"].encode() in z.read("inspection/provprotokoll.txt")
        assert [s["step_number"] for s in space["steps"]] == [
            s["step_number"] for s in graph["graph"]["steps"]
        ]
        assert space["world_min_um"][0] == -6000 and space["world_max_um"][0] == 906000
        assert b"assembly/required-space.json" in z.read("inspection/provprotokoll.txt")
        assert not manifest["physical_cutting_authorized"] and not manifest["production_qualified"]
        named = {p["semantic_key"]: p for p in model["parts"]}
        left, bottom = named["left-side"], named["bottom"]
        assert len(named) == 6 and len(model["trial_angles"]) == 6
        hardware = list(csv.DictReader(io.StringIO(z.read("bom/hardware.csv").decode("utf-8-sig"))))
        assert {r["item"]: int(r["quantity"]) for r in hardware} == {
            ARTICLE: 6,
            "DIN-96-A4-5x16": 12,
        }
        connections = list(
            csv.DictReader(io.StringIO(z.read("assembly/connections.csv").decode("utf-8-sig")))
        )
        assert sum(int(c["angle_quantity"]) for c in connections) == 6
        assert sum(int(c["screw_quantity"]) for c in connections) == 12
        occurrences = Counter(j for s in graph["graph"]["steps"] for j in s["joint_ids"])
        assert occurrences == Counter({j["joint_id"]: 1 for j in model["joints"]})
        # Inner left cheek x=18, bottom top z=18. The hole offsets are 18 from
        # the steel bend and 20 back from the front. DXF has lower-left UV in mm.
        path = f"parts/{left['part_id']}/B.dxf"
        dxf = ezdxf.read(io.StringIO(z.read(path).decode()))
        holes = [
            (e.dxf.center.x, e.dxf.center.y, e.dxf.radius)
            for e in dxf.modelspace().query("CIRCLE")
            if e.dxf.layer == "DRILL"
        ]
        assert holes.count((20.0, 36.0, 1.75)) == 1
        step = tmp_path / "with-angles.step"
        step.write_bytes(z.read("design/with-mounted-angles.step"))
        assembly = cq.Assembly.load(str(step))
        assert len(assembly.toCompound().Solids()) == 12
        ls = assembly.objects["timber/" + left["part_id"]].toCompound().Solids()[0]
        bs = assembly.objects["timber/" + bottom["part_id"]].toCompound().Solids()[0]
        # Sample the blind holes after reimport: open to 12.5 mm, with material behind.
        assert not ls.isInside((7, 20, 36)) and ls.isInside((5, 20, 36))
        assert not bs.isInside((36, 20, 7)) and bs.isInside((36, 20, 5))
        angle = next(
            a
            for a in model["trial_angles"]
            if a["upright_id"] == left["part_id"] and a["horizontal_id"] == bottom["part_id"]
        )
        pairs = list(
            csv.DictReader(io.StringIO(z.read("inspection/fogprov.csv").decode("utf-8-sig")))
        )
        type_test = json.loads(z.read("inspection/kvalificeringsprov.json"))
        assert (
            type_test["design_hash"] == manifest["design_hash"]
            and type_test["type_test_results"] is None
        )
        assert type_test["order_requirements"]["assembly_steps"] == list(range(1, 6))
        assert type_test["order_requirements"]["row_payload_n"] == w.design.intent.shelf_load_n
        assert len(pairs) == 12
        assert Counter(r["joint_id"] for r in pairs) == {
            a["joint_id"]: 2 for a in model["trial_angles"]
        }
        pair = {r["part_name"]: r for r in pairs if r["joint_id"] == angle["joint_id"]}
        for role, mate, world, uv, face in (
            ("left-side", "bottom", ("18", "20", "36"), ("20", "36"), "right"),
            ("bottom", "left-side", ("36", "20", "18"), ("24", "20"), "top"),
        ):
            row = pair[role]
            assert row["mate_id"] == pair[mate]["part_id"] and row["physical_face"] == face
            assert (row["world_X_mm"], row["world_Y_mm"], row["world_Z_mm"]) == world
            assert (row["U_mm"], row["V_mm"]) == uv
            assert (row["diameter_mm"], row["depth_mm"], row["model_tolerance_mm"]) == (
                "3.5",
                "12.5",
                "0.1",
            )
            assert row["drawing"] == f"parts/{row['part_id']}/{row['side']}.dxf"
            assert row["drawing"] in z.namelist() and row["revision"] == str(manifest["revision"])
            assert row["design_hash"] == manifest["design_hash"]
            assert not any(
                v for k, v in row.items() if k.startswith("measured_") or k == "inspection_result"
            )
        steel = assembly.objects["angle-" + angle["joint_id"]].toCompound().Solids()[0]
        assert not steel.isInside((20, 20, 36)) and steel.isInside((20, 25, 36))
        assert not steel.isInside((36, 20, 20)) and steel.isInside((36, 25, 20))
        # The left cheek occupies X=0..18. The back edge is captured at X=12.
        # On the trial profile the real STEP must be empty at X=11.8 and contain
        # wood at X=11.5: the extra depth belongs to the groove, not the back.
        back = next(p for p in model["parts"] if p["role"] == "back")
        ply = assembly.objects["timber/" + back["part_id"]].toCompound().Solids()[0]
        bb = ply.BoundingBox()
        y = (bb.ymin + bb.ymax) / 2
        assert bb.xmin == pytest.approx(12, abs=0.001)
        assert ply.isInside((12.2, y, 600)) and not ls.isInside((12.2, y, 600))
        assert not ply.isInside((11.8, y, 600)) and not ls.isInside((11.8, y, 600))
        assert ls.isInside((11.5, y, 600))
        # Shelf ends at Y=257.65; the rear wall of each side groove is Y=258.
        shelf = next(p for p in model["parts"] if p["role"] == "shelf")
        wood = assembly.objects["timber/" + shelf["part_id"]].toCompound().Solids()[0]
        bb = wood.BoundingBox()
        zmid = (bb.zmin + bb.zmax) / 2
        assert bb.ymax == pytest.approx(257.65, abs=0.001)
        assert wood.isInside((15, 257.55, zmid)) and not ls.isInside((15, 257.55, zmid))
        assert not wood.isInside((15, 257.85, zmid)) and not ls.isInside((15, 257.85, zmid))
        assert ls.isInside((15, 258.2, zmid))
        fit_report = json.loads(z.read("inspection/groove-fits.json"))
        assert (
            fit_report["revision"] == manifest["revision"]
            and fit_report["design_hash"] == model["design_hash"]
        )
        assert all(f["minimum_bottom_gap_um"] == 50 for f in fit_report["joints"])
        stop_fits = [f for f in fit_report["joints"] if f["minimum_front_stop_gap_um"] is not None]
        assert len(stop_fits) == 2 and all(f["minimum_front_stop_gap_um"] == 50 for f in stop_fits)
        datum_rows = list(
            csv.DictReader(
                io.StringIO(z.read("inspection/first-article-checks.csv").decode("utf-8-sig"))
            )
        )
        alignment_rows = [r for r in datum_rows if r["origin_semantics"] == "INSTALLED_FRONT_DATUM"]
        assert len(alignment_rows) == 2 and all(
            r["model_tolerance"] == "0.100" for r in alignment_rows
        )
        for check in ("finished_local_U", "finished_local_V", "actual_thickness"):
            rows = [r for r in datum_rows if r["check"] == check]
            assert len(rows) == len(named) and all(r["model_tolerance"] == "0.100" for r in rows)
        outlines = [r for r in datum_rows if r["feature_id"].startswith("outline:")]
        assert outlines and all(
            r["model_tolerance"] == "0.100" for r in outlines if r["check"] != "pattern_count"
        )
        assert sum(r["origin_semantics"] == "INSTALLED_WORLD_DATUM" for r in datum_rows) == 2 * len(
            fit_report["joints"]
        )
        # Only the occupied quadrant is available for installation from inside the bay.
        assert not steel.isInside((15, 20, 36)) and not steel.isInside((36, 20, 15))


@pytest.mark.cad
def test_swept_cheek_intersects_real_skirt_at_start_only_in_blocked_package(tmp_path):
    from tests.unit.test_bookcase_site import installed_trial

    cq = pytest.importorskip("cadquery")
    for reservation, expected_state in ((20_000, "blocked"), (27_000, "nominal_clear")):
        w = installed_trial(reservation, reservation, ceiling=1_400_000, front=600_000)
        with ZipFile(io.BytesIO(build_furniture_review(w))) as z:
            motion = json.loads(z.read("assembly/required-space.json"))
            site = motion["standing_site"]
            assert site["state"] == expected_state
            handoff = json.loads(z.read("manufacturing/workshop-handoff.json"))
            assert site == handoff["dimensions"]["standing_assembly"]
            assert motion["revision"] == w.design.revision
            checks = z.read("inspection/provprotokoll.txt").decode()
            assert "Angiven fri takhöjd: 1400 mm." in checks
            assert all(i["message"] in checks for i in site["issues"])
            step_file = tmp_path / "wood.step"
            step_file.write_bytes(z.read("design/model.step"))
            tree = cq.Assembly.load(str(step_file))
            parts = json.loads(z.read("design/resolved.json"))["parts"]
            cheek_id = next(p["part_id"] for p in parts if p["semantic_key"] == "left-side")
            cheek = tree.objects[cheek_id].toCompound()
            # Independent oracle in mm: side wall at -reservation, rectangular
            # 20 x 90 skirting, full 280 mm depth. At the start of the +X
            # insertion the cheek is 6 mm left of its final position.
            trim = cq.Solid.makeBox(20, 280, 90, cq.Vector(-reservation / 1000, 0, 0))
            assert cheek.intersect(trim).Volume() < 1e-6
            intersection = cheek.translate((-6, 0, 0)).intersect(trim).Volume()
            assert (intersection > 100_000) if reservation == 20_000 else intersection < 1e-6
