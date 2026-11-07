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
        fit_report = json.loads(z.read("inspection/groove-fits.json"))
        assert (
            fit_report["revision"] == manifest["revision"]
            and fit_report["design_hash"] == model["design_hash"]
        )
        assert all(f["minimum_bottom_gap_um"] == 50 for f in fit_report["joints"])
        datum_rows = list(
            csv.DictReader(
                io.StringIO(z.read("inspection/first-article-checks.csv").decode("utf-8-sig"))
            )
        )
        assert sum(r["origin_semantics"] == "INSTALLED_WORLD_DATUM" for r in datum_rows) == 2 * len(
            fit_report["joints"]
        )
        # Only the occupied quadrant is available for installation from inside the bay.
        assert not steel.isInside((15, 20, 36)) and not steel.isInside((36, 20, 15))
