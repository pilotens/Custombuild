"""Acceptance cases for the machine-neutral shelving trial, independent of CAM."""

import csv
import io
import json
from collections import Counter
from zipfile import ZipFile

import pytest
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_domain.models import DesignResult
from custombuild_manufacturing.quality import validate_json_schema_instance
from custombuild_worker.furniture_review import build_furniture_review

ARTICLE = "GN-970-ALM-30-30-30-B"


def trial(*, material="birch-plywood", thickness=18_000, **layout):
    return FurnitureWorkspace.model_validate(
        {
            "design": {
                "intent": {
                    "family": "shelving",
                    "width_um": 900_000,
                    "height_um": 1_200_000,
                    "depth_um": 280_000,
                    "trial_angle": ARTICLE,
                    **layout,
                },
                "material": {"material_id": material, "measured_thickness_um": thickness},
            }
        }
    )


@pytest.mark.parametrize(
    ("material", "thickness", "layout", "count"),
    [
        ("birch-plywood", 17_000, {"divider_count": 0, "shelf_count": 0}, 4),
        ("mdf", 19_000, {"divider_count": 0, "shelf_count": 2, "depth_um": 100_000}, 8),
        ("solid-walnut", 18_000, {"divider_count": 1, "shelf_count": 2}, 14),
        (
            "solid-walnut",
            18_000,
            {
                "divider_count": 2,
                "shelf_count": 2,
                "bay_width_ratios_ppm": [250000, 450000, 300000],
            },
            20,
        ),
    ],
)
def test_selected_profile_pairs_every_angle_with_two_distinct_pilots(
    material, thickness, layout, count
):
    result = build_furniture(trial(material=material, thickness=thickness, **layout).design)
    assert len(result.trial_angles) == count
    ids = [fid for a in result.trial_angles for fid in (a.upright_hole, a.horizontal_hole)]
    assert len(set(ids)) == 2 * count
    assert {f.feature_id for p in result.parts for f in p.features if f.joint_id is None} == set(
        ids
    )
    assert result.shelving_result
    doc = result.shelving_result.model_dump(mode="json")
    doc["trial_angles"].pop()
    with pytest.raises(ValueError, match="varje bärande fog"):
        DesignResult.model_validate(doc)


@pytest.mark.parametrize(
    ("layout", "message"),
    [
        ({"width_um": 249999}, "greater than or equal"),
        ({"height_um": 4000001}, "less than or equal"),
        ({"depth_um": 99999}, "greater than or equal"),
        ({"shelf_load_n": 5001}, "less than or equal"),
        ({"shelf_mount": "adjustable"}, "fasta hyllor"),
        ({"plinth_height_um": 90000}, "utan sockel"),
        ({"back_panel": "surface_mounted"}, "spårmonterad rygg"),
    ],
)
def test_orders_outside_the_profile_get_specific_rejections(layout, message):
    with pytest.raises(ValueError, match=message):
        build_furniture(trial(**layout).design)


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
        # Only the occupied quadrant is available for installation from inside the bay.
        assert not steel.isInside((15, 20, 36)) and not steel.isInside((36, 20, 15))
