"""Check the room, the individual walnut carcasses and their shared cutting list."""

import csv
import io
import json
from pathlib import Path

from .support import response_json, save_furniture, verify_cut

EXAMPLE = (
    Path(__file__).resolve().parents[2] / "examples/furniture/walnut-shelving-4340x2540x280.json"
)
# Hand-calculated widths, heights, piece counts and fit for the selected walnut stock.
EXPECTED = {
    "easy": ([2_150_000] * 2, [2_540_000], 20, False),
    "medium": ([2_150_000] * 2, [1_270_000] * 2, 30, False),
    "hard": ([1_431_333, 1_431_333, 1_431_332], [1_268_500, 1_268_499], 69, True),
    "very-hard": ([2_150_000] * 2, [1_270_000] * 2, 62, True),
}


def exercise_modules(client, case, e):
    original = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    path, saved = save_furniture(client, case.id, original)
    grid = case.order.inputs

    def plan_for(settings):
        return response_json(
            client.post(
                "/v1/furniture/module-plan",
                json={"workspace": saved["workspace"], "grid": settings},
            )
        )

    plan = plan_for(grid)
    assert plan["physical_cutting_authorized"] is plan["production_qualified"] is False
    assert plan["source_design_hash"] == saved["preview"]["design"]["design_hash"]
    assert plan["source"]["workspace"] == saved["workspace"]
    assert response_json(client.get(f"{path}/draft")) == saved
    if case.level == "impossible":
        e.equal(
            "blocked_plan",
            [plan["state"], plan["code"], plan["can_export_drafts"]],
            ["unavailable", "PLINTH_TRANSFORMATION_UNSUPPORTED", False],
        )
        assert plan["modules"] == [] and not plan.get("review_cut_list_csv")
        assert any(
            r["code"] == plan["code"] and r["status"] == "blocked" for r in plan["requirements"]
        )
        return
    widths, heights, count, fit = EXPECTED[case.level]
    e.equal("module_widths_um", plan["layout"]["column_widths_um"], widths)
    e.equal("module_heights_um", plan["layout"]["row_heights_um"], heights)
    e.equal("stock_fit", plan["screening"]["all_stock_fit"], fit)
    assert plan["state"] == "available" and plan["can_export_drafts"] is True
    assert plan["layout"]["envelope"] == {
        "width_um": 4_300_000,
        "height_um": 2_540_000,
        "depth_um": 260_000,
    }
    rows = list(csv.DictReader(io.StringIO(plan["review_cut_list_csv"])))
    e.equal("cut_rows", len(rows), count)
    assert {r["plan_hash"] for r in rows} == {plan["plan_hash"]}
    assert len(plan["modules"]) == len(widths) * len(heights)
    coordinates = set()
    for module in plan["modules"]:
        col, row = module["column"], module["row"]
        coordinates.add((col, row))
        assert module["placement"] == {
            "x_um": sum(widths[:col]) + col * grid["gap_um"],
            "y_um": 0,
            "z_um": sum(heights[:row]) + row * grid["gap_um"],
        }
        assert module["dimensions_um"] == {
            "width_um": widths[col],
            "height_um": heights[row],
            "depth_um": 260_000,
        }
        if case.level == "medium":
            groups = {
                g["material_id"]: g for g in module["preview"]["manufacturing"]["stock_groups"]
            }
            assert groups["solid-walnut"]["geometry_compatible"] is True
            back = groups["birch-plywood-6"]
            assert back["geometry_compatible"] is False
            assert "PART_EXCEEDS_STOCK" in {issue["code"] for issue in back["issues"]}
        cut = [r for r in rows if r["module_id"] == module["module_id"]]
        parts = {i["part_id"]: i for i in module["preview"]["design"]["parts"]}
        assert sorted(r["draft_part_id"] for r in cut) == sorted(parts)
        assert sum(r["part_name"] == "plinth" for r in cut) == int(row == 0)
        for item in cut:
            part = parts[item["draft_part_id"]]
            assert item["part_name"] == part["semantic_key"]
            assert item["draft_design_hash"] == module["design_hash"]
            assert [int(item[k]) for k in ("row_from_floor", "column_from_left")] == [
                row + 1,
                col + 1,
            ]
            assert item["material_id"] == (
                "birch-plywood-6" if part["role"] == "back" else "solid-walnut"
            )
            verify_cut(item, part)
        if case.level == "very-hard":
            # Persist each planned module as its own project. Project-specific IDs change,
            # so match workshop dimensions by semantic part name across this boundary.
            _, draft = save_furniture(client, case.id, module["workspace"])
            persisted = {p["semantic_key"]: p for p in draft["preview"]["design"]["parts"]}
            assert sorted(r["part_name"] for r in cut) == sorted(persisted)
            for item in cut:
                verify_cut(item, persisted[item["part_name"]])
            for item in cut:
                part = persisted[item["part_name"]]
                sheet = response_json(
                    client.post(
                        "/v1/furniture/inspect-part",
                        json={
                            "workspace": draft["workspace"],
                            "expected_design_hash": draft["preview"]["design"]["design_hash"],
                            "part_id": part["part_id"],
                        },
                    )
                )
                assert sheet["part"] == part
                assert any(stock["fits"] for stock in sheet["stock"])
                verify_cut(item, sheet["part"])
            assert (
                module["workspace"]["design"]["installation"] == original["design"]["installation"]
            )
    assert coordinates == {(c, r) for c in range(len(widths)) for r in range(len(heights))}
    if case.level == "very-hard":
        e.note("inspected_parts", len(rows))
        e.equal("grain_axes", sorted({r["grain_axis_uv"] for r in rows}), ["U", "V"])
        assert plan_for(grid)["plan_hash"] == plan["plan_hash"]
        assert plan_for({**grid, "gap_um": 1})["plan_hash"] != plan["plan_hash"]
