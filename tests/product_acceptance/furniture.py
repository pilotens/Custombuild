"""Six customer-facing furniture profiles exercised through the authenticated API."""

from math import cos, radians, sin

from app.db import get_session_factory
from app.models import GenerationJob
from sqlalchemy import select

from .catalog import FAMILIES
from .review import verify_download
from .support import check_carcass, response_json, save_furniture

DEFAULTS = {
    "panel-table": dict(
        width_um=1_000_000,
        height_um=740_000,
        depth_um=600_000,
        end_inset_um=60_000,
        stretcher_height_um=100_000,
    ),
    "dining-rectangle": dict(width_um=1_800_000, height_um=750_000, depth_um=900_000),
    "dining-round": dict(
        width_um=1_200_000, height_um=750_000, depth_um=1_200_000, top_shape="round"
    ),
    "chair": dict(
        width_um=460_000,
        height_um=850_000,
        depth_um=500_000,
        seat_depth_um=450_000,
        seat_height_um=450_000,
        back_angle_mdeg=10_000,
        seat_load_n=900,
    ),
    "dresser": dict(
        width_um=800_000,
        height_um=900_000,
        depth_um=500_000,
        drawer_count=3,
        drawer_columns=1,
        front_gap_um=3_000,
        drawer_load_n=100,
    ),
    "shelving": dict(width_um=900_000, height_um=1_800_000, depth_um=320_000),
}


def workspace(product, patch):
    intent = DEFAULTS[product] | {"family": FAMILIES[product]} | patch
    runner = intent.pop("runner", "9114275")
    design = {"intent": intent}
    if product == "panel-table":
        design["hardware"] = {"catalog_id": "panel-table-connectors-layout"}
    if product == "dresser":
        design["hardware"] = {"catalog_id": "hettich-ka4532-" + runner}
    return {"design": design}


def world_point(part, feature):
    p, o = part["placement"], feature["origin"]
    a = radians(p["rotation_x_mdeg"] / 1000)
    return (
        p["x_um"] + o["x_um"],
        p["y_um"] + o["y_um"] * cos(a) - o["z_um"] * sin(a),
        p["z_um"] + o["y_um"] * sin(a) + o["z_um"] * cos(a),
    )


def check_screw_pairs(parts, joints):
    by_id = {p["part_id"]: p for p in parts}
    for joint in joints:
        pair = []
        for member in joint["members"]:
            p = by_id[member["part_id"]]
            f = next(f for f in p["features"] if f["feature_id"] == member["feature_ids"][0])
            assert f["joint_id"] == joint["joint_id"]
            pair.append((world_point(p, f), f))
        (start, hole), (end, pilot) = pair
        assert hole["through"] and not pilot["through"]
        sign, axis = joint["assembly_direction"]
        distance = hole["dimensions"]["depth_um"] * (1 if sign == "+" else -1)
        for i, direction in enumerate("xyz"):
            expected = distance if axis == direction else 0
            assert abs((end[i] - start[i]) - expected) <= 1, joint["joint_id"]


def check_frame(case, d, by_name, rules, p, e):
    chair = case.product == "chair"
    e.equal("parts_joints", [len(d["parts"]), len(d["joints"])], [10, 24] if chair else [9, 20])
    top = by_name["chair-seat" if chair else "table-top"]
    depth = p["seat_depth_um"] if chair else p["depth_um"]
    level = p["seat_height_um"] if chair else p["height_um"]
    e.equal(
        "top_width_depth_elevation_um",
        [
            top["finished_size"]["width_um"],
            top["finished_size"]["depth_um"],
            top["placement"]["z_um"] + 18_000,
        ],
        [p["width_um"], depth, level],
    )
    recess = [f for f in top["features"] if f["dimensions"]["diameter_um"] == 11_000]
    e.equal(
        "flush_recesses",
        [len(recess), sorted({f["dimensions"]["depth_um"] for f in recess})],
        [4, [4_000]],
    )
    assert not any(f["through"] for f in recess)
    check_screw_pairs(d["parts"], d["joints"])
    leg = by_name["leg-rear-right"]
    if chair:
        e.equal(
            "rear_edge_um",
            leg["placement"]["y_um"] + leg["finished_size"]["depth_um"],
            p["depth_um"],
        )
        e.equal(
            "back_angle_mdeg",
            by_name["chair-back"]["placement"]["rotation_x_mdeg"],
            -p["back_angle_mdeg"],
        )
        rule = rules["CB-CHAIR-PROTOTYPE-001"]
        e.equal(
            "prototype_block_load_n",
            [rule["status"], rule["values"]["requested_seat_load_n"]],
            ["BLOCK", p["seat_load_n"]],
        )
        assert rules["CB-CHAIR-REAR-TIP-001"]["values"]["estimated_force_n"] > 0
        return
    circular = case.product == "dining-round"
    e.equal("circular_outline", top.get("circular_outline", False), circular)
    assert rules["CB-TABLE-CONNECTION-001"]["status"] == "BLOCK"
    section = leg["finished_size"]["width_um"]
    front_left = by_name["leg-front-left"]["placement"]
    rail = by_name["rail-front"]
    e.equal(
        "rail_between_legs_um",
        rail["finished_size"]["width_um"],
        leg["placement"]["x_um"] - front_left["x_um"] - section,
    )
    if circular:
        assert rules["CB-TABLE-ROUND-TOP-001"]["status"] == "BLOCK"
        radius = p["width_um"] / 2
        for part in d["parts"]:
            if part["role"] != "frame_leg":
                continue
            for x in [0, section]:
                for y in [0, section]:
                    assert (
                        (part["placement"]["x_um"] + x - radius) ** 2
                        + (part["placement"]["y_um"] + y - radius) ** 2
                    ) <= radius**2
    elif case.level == "hard":
        rule = rules["CB-TABLE-DEFLECTION-001"]
        e.equal("deflection_screen", rule["status"], "BLOCK")
        assert rule["values"]["calculated_mm"] > rule["values"]["allowed_mm"]


def check_drawers(d, by_name, p, rules, e):
    count, columns = p["drawer_count"], p["drawer_columns"]
    short = d["hardware_profile"]["catalog_id"].endswith("9114273")
    travel = 355_000 if short else 460_000
    stops = [37_000, 257_000] if short else [37_000, 165_000, 331_000]
    case_stops = [37_000, 165_000, 229_000 if short else 261_000]
    e.equal(
        "parts_groups",
        [len(by_name), len(d["moving_groups"])],
        [5 + 2 * (columns - 1) + 6 * count, count],
    )
    walls = sorted(
        [i for i in d["parts"] if i["role"] in ("left_side", "right_side", "divider")],
        key=lambda i: i["placement"]["x_um"],
    )
    fronts = []
    for i, group in enumerate(d["moving_groups"]):
        key = f"drawer-{i + 1}"
        left, right = by_name[key + "-left"], by_name[key + "-right"]
        front = by_name[key + "-front"]
        assert front["placement"]["y_um"] == 0
        assert set(group["part_ids"]) == {
            by_name[key + "-" + n]["part_id"]
            for n in ("left", "right", "bottom", "front", "box-front", "back")
        }
        assert (group["travel_um"], group["rated_pair_load_kg"], group["requested_load_n"]) == (
            travel,
            35,
            p["drawer_load_n"],
        )
        col = i % columns
        assert left["placement"]["x_um"] - walls[col]["placement"]["x_um"] - 18_000 == 12_700
        assert walls[col + 1]["placement"]["x_um"] - right["placement"]["x_um"] - 18_000 == 12_700
        for side in (left, right):
            assert (
                sorted(f["origin"]["y_um"] for f in side["features"] if not f["joint_id"]) == stops
            )
        if col == 0:
            fronts.append(front)
    gap = p["front_gap_um"]
    assert fronts[0]["placement"]["z_um"] == 18_000 + gap
    top_edge = 18_000 + gap
    for i, front in enumerate(fronts):
        assert front["placement"]["z_um"] == top_edge
        height = front["finished_size"]["height_um"]
        if i < len(p.get("row_front_heights_um", [])):
            assert height == p["row_front_heights_um"][i]
        top_edge += height
        if i < len(fronts) - 1:
            top_edge += gap if p.get("gap_basis") == "clear_reveal" else 2 * gap
    e.equal("top_front_edge_um", top_edge, p["height_um"] - 18_000 - gap)
    for wall in walls:
        bores = [f for f in wall["features"] if f["feature_id"].startswith("run_")]
        assert sorted({f["origin"]["y_um"] - 18_000 for f in bores}) == case_stops
        if wall["role"] == "divider":
            h = {
                face: {f["origin"]["z_um"] for f in bores if f["face"] == face}
                for face in ("left", "right")
            }
            assert min(abs(a - b) for a in h["left"] for b in h["right"]) >= 12_000
    e.equal("runner_travel_um", travel, d["moving_groups"][0]["travel_um"])
    if p["drawer_load_n"] == 400:
        e.equal("overload", rules["CB-RUNNER-LOAD-001"]["status"], "BLOCK")


def inspect(case, preview, p, e):
    d = preview["design"]
    e.equal("physical_cutting_authorized", preview["physical_cutting_authorized"], False)
    assert preview["production_qualified"] is False
    by_name = {part["semantic_key"]: part for part in d["parts"]}
    assert len(by_name) == len(d["parts"]) == len({part["part_id"] for part in d["parts"]})
    assert d["total_weight_g"] == sum(part["weight_g"] for part in d["parts"])
    rules = {r["rule_id"]: r for r in preview["rules"]["evaluations"]}
    if case.product in ("dining-round", "dining-rectangle", "chair"):
        check_frame(case, d, by_name, rules, p, e)
    elif case.product == "dresser":
        check_drawers(d, by_name, p, rules, e)
    elif case.product == "panel-table":
        e.equal("parts_joints", [len(by_name), len(d["joints"])], [5, 0])
        assert rules["CB-TABLE-CONNECTION-001"]["status"] == "BLOCK"
        span = p["width_um"] - 2 * (p["end_inset_um"] + 18_000)
        for end in ("front", "back"):
            rail = by_name["table-stretcher-" + end]
            e.equal(end + "_span_um", rail["finished_size"]["width_um"], span)
            assert rail["placement"]["x_um"] == p["end_inset_um"] + 18_000
    else:
        bays = p["divider_count"] + 1
        plinth = p.get("plinth_height_um", 0)
        check_carcass(
            d["parts"],
            width=p["width_um"],
            height=p["height_um"],
            depth=p["depth_um"],
            rows=p["shelf_count"],
            bays=bays,
            backs=bays,
            plinth=plinth,
            evidence=e,
            ratios=p.get("bay_width_ratios_ppm"),
            floor=plinth + 18_000,
            centres=[v / 1_000_000 for v in p.get("shelf_height_ratios_ppm", [])],
        )


def exercise_furniture(client, case, e, monkeypatch, tmp_path):
    w = workspace(case.product, case.order.inputs)
    p = w["design"]["intent"]
    if case.level == "impossible" and case.product != "shelving":
        assert case.order.rejection, "An impossible build must specify its rejection reason"
        path, original = save_furniture(client, case.id, workspace(case.product, {}))
        for response in (
            client.post("/v1/furniture/preview", json=w),
            client.put(
                f"{path}/draft", json={"expected_revision": original["revision"], "workspace": w}
            ),
        ):
            detail = response_json(response, 422)["detail"]
            e.note("rejection", detail)
            assert case.order.rejection in detail
        assert response_json(client.get(f"{path}/draft")) == original
        return
    path, draft = save_furniture(client, case.id, w)
    inspect(case, draft["preview"], p, e)
    request = {
        "expected_revision": draft["revision"],
        "expected_design_hash": draft["preview"]["design"]["design_hash"],
    }
    if case.product == "shelving" and case.level in ("hard", "impossible"):
        blocked = case.level == "impossible"
        e.equal(
            "parts_at_export_limit",
            len(draft["preview"]["design"]["parts"]),
            129 if blocked else 128,
        )
        job = response_json(client.post(f"{path}/exports", json=request), 422 if blocked else 202)
        if blocked:
            e.equal("rejection", job["detail"], "Granskningspaketet stöder högst 128 delar.")
        else:
            e.equal("export_state", job["state"], "queued")
        with get_session_factory()() as session:
            jobs = session.scalars(
                select(GenerationJob.id).where(GenerationJob.project_id == draft["project_id"])
            ).all()
        e.equal("queued_jobs", list(jobs), [] if blocked else [job["job_id"]])
        assert response_json(client.get(f"{path}/draft")) == draft
    if case.level == "very-hard":
        verify_download(client, path, draft, request, p, case, e, monkeypatch, tmp_path)
