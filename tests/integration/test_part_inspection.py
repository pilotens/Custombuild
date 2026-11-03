from collections import Counter
from copy import deepcopy

import pytest
from app.main import app
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import FurnitureResult, build_furniture
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.integration.test_api_design_flow import (
    HEADERS,
    valid_production_context,
    valid_spec,
    valid_workspace_intent,
    version_payload,
)
from tests.product_acceptance.support import response_json


@pytest.fixture
def buyer():
    with TestClient(app, headers=HEADERS) as client:
        yield client


def test_chair_leg_removal_uses_real_mates_and_leaves_the_saved_chair_intact(buyer):
    w = {"design": {"intent": {"family": "chair", "seat_depth_um": 410_000, "depth_um": 551_007}}}
    saved = response_json(
        buyer.post("/v1/furniture/projects", json={"name": "Part inspection", "workspace": w}), 201
    )
    draft = saved["draft"]
    result = draft["preview"]["design"]
    leg = next(p for p in result["parts"] if p["semantic_key"] == "leg-front-left")
    order = {
        "workspace": draft["workspace"],
        "expected_design_hash": result["design_hash"],
        "part_id": leg["part_id"],
    }
    path = "/v1/furniture/inspect-part"
    sheet = response_json(buyer.post(path, json=order))
    assert sheet["part"] == leg
    assert len(sheet["removal"]["remaining_frame_feet"]) == 3
    assert len(sheet["removal"]["separated_groups"]) == 1
    assert len(sheet["removal"]["separated_groups"][0]) == 9
    assert {m["semantic_key"] for m in sheet["mates"]} == {"rail-left", "rail-front"}
    assert sum(h["quantity"] for h in sheet["hardware"]) == len(sheet["mates"]) == 4
    assert {f["feature_id"] for f in leg["features"]} == {
        fid
        for m in sheet["mates"]
        for member in m["joint"]["members"]
        if member["part_id"] == leg["part_id"]
        for fid in member["feature_ids"]
    }
    assert (
        response_json(buyer.get(f"/v1/furniture/projects/{saved['project']['id']}/draft")) == draft
    )
    assert buyer.post(path, json={**order, "expected_design_hash": "f" * 64}).status_code == 409
    assert buyer.post(path, json={**order, "part_id": "par_not_in_assembly"}).status_code == 404
    with TestClient(app) as anonymous:
        assert anonymous.post(path, json=order).status_code == 401


@pytest.mark.parametrize("fault", ["displaced_pilot", "missing_mate", "wrong_feature"])
def test_corrupted_connections_cannot_create_a_valid_furniture_result(fault):
    d = build_furniture(
        FurnitureWorkspace.model_validate({"design": {"intent": {"family": "chair"}}}).design
    ).model_dump(mode="json")
    m = d["joints"][0]["members"][1]
    part = next(p for p in d["parts"] if p["part_id"] == m["part_id"])
    f = next(f for f in part["features"] if f["feature_id"] == m["feature_ids"][0])
    if fault == "displaced_pilot":
        f["origin"]["y_um"] += 1000
    elif fault == "missing_mate":
        m["part_id"] = "par_nonexistent"
    else:
        m["feature_ids"][0] = d["joints"][0]["members"][0]["feature_ids"][0]
    with pytest.raises(ValidationError, match="hålcentrum|missing part|another part"):
        FurnitureResult.model_validate(d)


def test_reference_wall_per_part_register_measures_back_and_marks_missing_posts(buyer):
    spec = {
        **valid_spec(),
        "furniture_type": "wall_library",
        "material_id": "birch-plywood",
        "width_mm": 4300,
        "height_mm": 2540,
        "depth_mm": 260,
        "plinth_height_mm": 90,
        "base_cabinet_count": 4,
        "base_cabinet_height_mm": 700,
        "base_cabinet_depth_mm": 260,
        "divider_count": 4,
        "shelf_count": 3,
    }
    d = response_json(buyer.post("/v1/designs/preview", json=spec))
    p = next(p for p in d["parts"] if p["name"] == "shelf-r1-b2")
    request = {
        "spec": spec,
        "part_id": p["part_id"],
        "expected_design_hash": d["design_hash"],
        "production_context": valid_production_context(),
        "construction": {
            "uprights": "separate_posts",
            "back": "all_openings",
            "shelves": "unknown",
            "material": "solid-walnut",
            "unresolved_detail": "Ändstöden behöver egna mått.",
        },
    }
    url = "/v1/designs/inspect-part"
    sheet = response_json(buyer.post(url, json=request))
    assert {r["state"] for r in sheet["rear_openings"] if r["zone"] == "upper"} == {"covered"}
    base = [r for r in sheet["rear_openings"] if r["zone"] == "base"]
    assert len(base) == 4 and all(r["state"] == "open" and not r["back_part_ids"] for r in base)
    assert {r["bottom_um"] for r in base} == {108_000}
    assert {r["top_um"] for r in base} == {772_000}
    assert len(sheet["stock"]) == 1 and sheet["stock"][0]["width_um"] == 2_440_000
    assert sheet["stock"][0]["dimensions_fit"] is True
    assert sheet["stock"][0]["fits"] is None and sheet["stock"][0]["grain"] == "UNBOUND"
    trace = sheet["reference_objects"]
    assert Counter(t["state"] for t in trace if t["key"].startswith("support-")) == {"different": 6}
    assert [len(t["compared_parts"]) for t in trace if t["key"].startswith("shelf-r")] == [5] * 3
    assert any(t["state"] == "different" and t["key"] == "unmodeled-detail" for t in trace)
    assert {m["semantic_key"] for m in sheet["mates"]} == {"divider-1", "divider-2"}
    assert sheet["part"]["material_id"] == "birch-plywood"
    other = deepcopy(request)
    other["production_context"]["stock_width_mm"] = 730
    different_stock = response_json(buyer.post(url, json=other))
    assert different_stock["design_hash"] == sheet["design_hash"]
    assert different_stock["sheet_sha256"] != sheet["sheet_sha256"]
    assert buyer.post(url, json={**request, "expected_design_hash": "0" * 64}).status_code == 409


def test_saving_a_reference_cannot_discard_its_unresolved_details_at_freeze(buyer):
    project = response_json(buyer.post("/v1/projects", json={"name": "Post reference"}), 201)
    spec = valid_spec()
    ref = {
        "source": "reference_image",
        "import_id": "11111111-1111-4111-8111-111111111111",
        "image_sha256": "a" * 64,
        "file_name": "reference.jpg",
        "image_width_px": 704,
        "image_height_px": 594,
        "confidence": 0.96,
        "detected_shelves": 4,
        "detected_dividers": 8,
        "detected_base_cabinets": True,
        "warnings": [],
        "construction": {"uprights": "separate_posts", "back": "unknown", "shelves": "unknown"},
    }
    root = f"/v1/projects/{project['id']}"
    response_json(
        buyer.put(
            root + "/draft",
            json={
                "template_id": "shelving",
                "expected_draft_revision": 0,
                "spec": spec,
                "workspace_spec": valid_workspace_intent(reference_image_import=ref),
            },
        )
    )
    omitted = response_json(
        buyer.post(root + "/versions", json=version_payload(project["id"], spec)), 409
    )
    assert omitted["detail"]["code"] == "REFERENCE_DRAFT_MISMATCH"
    assert response_json(buyer.get(root + "/versions")) == []


def test_drill_sheet_lists_every_physical_circle_at_the_rendered_uv_position(buyer):
    from decimal import Decimal
    from xml.etree import ElementTree

    w = {
        "design": {
            "intent": {
                "family": "shelving",
                "shelf_count": 3,
                "divider_count": 1,
                "shelf_mount": "adjustable",
            }
        }
    }
    preview = response_json(buyer.post("/v1/furniture/preview", json=w))["design"]
    side = next(p for p in preview["parts"] if p["semantic_key"] == "left-side")
    sheet = response_json(
        buyer.post(
            "/v1/furniture/inspect-part",
            json={
                "part_id": side["part_id"],
                "expected_design_hash": preview["design_hash"],
                "workspace": w,
            },
        )
    )
    drill_ids = {
        f["feature_id"]: f["pattern_count"]
        for f in side["features"]
        if f["kind"] in ("drill", "drill_pattern")
    }
    occurrences: Counter[str] = Counter()
    for drawing in sheet["drawings"]:
        svg = ElementTree.fromstring(drawing["svg"])  # noqa: S314 -- generated locally
        circles = Counter(
            (
                c.attrib["data-feature-id"],
                Decimal(c.attrib["cx"]) * 1000,
                Decimal(c.attrib["cy"]) * 1000,
                Decimal(c.attrib["r"]) * 2000,
            )
            for c in svg.findall(".//{*}circle")
            if c.attrib.get("class") == "drill"
        )
        shown = Counter(
            (h["feature_id"], h["u_um"], h["v_um"], h["diameter_um"]) for h in drawing["holes"]
        )
        assert shown == circles
        occurrences.update(h["feature_id"] for h in drawing["holes"])
    assert occurrences == drill_ids and sum(occurrences.values()) > 0


def test_full_wall_back_covers_every_opening_without_intersecting_base_or_fitting_short_stock(
    buyer,
):
    spec = {
        **valid_spec(),
        "furniture_type": "wall_library",
        "width_mm": 4300,
        "height_mm": 2540,
        "depth_mm": 260,
        "plinth_height_mm": 90,
        "base_cabinet_count": 4,
        "base_cabinet_height_mm": 700,
        "base_cabinet_depth_mm": 260,
        "divider_count": 4,
        "shelf_count": 3,
        "back_panel": "surface_mounted",
    }
    setting = response_json(buyer.post("/v1/designs/preview", json=spec))
    back = next(p for p in setting["parts"] if p["name"] == "back")
    sheet = response_json(
        buyer.post(
            "/v1/designs/inspect-part",
            json={
                "spec": spec,
                "part_id": back["part_id"],
                "expected_design_hash": setting["design_hash"],
                "production_context": valid_production_context(),
            },
        )
    )
    inspected = sheet["part"]
    assert inspected["finished_size"] == {
        "width_um": 4_300_000,
        "height_um": 2_540_000,
        "depth_um": 6_000,
    }
    assert len(sheet["rear_openings"]) == 9
    assert all(o["state"] == "covered" for o in sheet["rear_openings"])
    assert sheet["stock"] and all(
        s["fits"] is False and not s["dimensions_fit"] for s in sheet["stock"]
    )
    assert sheet["physical_cutting_authorized"] is False
    for part in setting["parts"]:
        if part["name"].startswith(("base-side-", "base-bottom-")):
            assert (
                part["placement_um"]["y_um"] + part["size_um"]["depth_um"]
                == inspected["placement"]["y_um"]
            )


def test_measured_reference_posts_have_distinct_geometry_and_no_invented_fasteners(buyer):
    spec = valid_spec()
    preview = response_json(buyer.post("/v1/designs/preview", json=spec))
    side = next(p for p in preview["parts"] if p["name"] == "left-side")
    post = {
        "key": "ref-post-1",
        "name": "Vänster kortsida",
        "shape": "round",
        "width_um": 18_000,
        "depth_um": 18_000,
        "height_um": 300_000,
        "x_um": 0,
        "y_um": -18_000,
        "z_um": 110_000,
    }
    req = {
        "spec": spec,
        "part_id": side["part_id"],
        "expected_design_hash": preview["design_hash"],
        "construction": {"uprights": "separate_posts", "back": "unknown", "posts": [post]},
    }

    def run():
        review = response_json(buyer.post("/v1/designs/inspect-part", json=req))
        item = next(o for o in review["reference_objects"] if o["key"] == post["key"])
        assert item["state"] == "different" and item["measured_post"] == post
        assert review["physical_cutting_authorized"] is False
        return item, review["sheet_sha256"]

    touching, sha = run()
    assert side["part_id"] in touching["touching"] and not touching.get("overlapping")
    post["y_um"] += 1
    overlap, new_sha = run()
    assert side["part_id"] in overlap["overlapping"] and sha != new_sha
    post["y_um"] -= 2
    free, _ = run()
    assert not free.get("touching") and not free.get("overlapping")
    assert response_json(buyer.post("/v1/designs/preview", json=spec)) == preview
