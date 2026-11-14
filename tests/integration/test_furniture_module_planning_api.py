from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from app.main import app
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_production import shelving_production_spec
from fastapi.testclient import TestClient

HEADERS = {"Authorization": "Bearer demo-nordic-owner"}
GRID = {"columns": 5, "rows": 2, "gap_um": 0, "shelf_count_per_row": [2, 2]}


def test_six_base_doors_survive_stacking_in_drafts_and_cut_list():
    source = json.loads(Path("examples/furniture/bookcase-walnut-base-700.json").read_bytes())
    grid = {"columns": 6, "rows": 2, "gap_um": 0, "shelf_count_per_row": [2, 3]}
    with TestClient(app) as client:

        def plan(columns):
            res = client.post(
                "/v1/furniture/module-plan",
                headers=HEADERS,
                json={"workspace": source, "grid": {**grid, "columns": columns}},
            )
            assert res.status_code == 200, res.text
            return res.json()

        good = plan(6)
        assert good["can_export_drafts"] and not good["can_apply"]
        assert good["source"]["workspace"]["design"]["intent"]["base_doors"] == "J95-C24/0T-NI"
        for module in good["modules"]:
            lower = module["row"] == 0
            parts = module["preview"]["design"]["parts"]
            assert sum(p["role"] == "cabinet_front" for p in parts) == int(lower)
            holes = [f for p in parts for f in p["features"] if f["feature_id"].startswith("J95-")]
            assert len(holes) == (15 if lower else 0)
            assert bool(module["workspace"]["design"]["intent"].get("base_doors")) == lower
            assert module["workspace"]["design"]["intent"]["trial_angle"] == "GN-970-ALM-30-30-30-B"
        cut = list(csv.DictReader(io.StringIO(good["review_cut_list_csv"])))
        fronts = [line for line in cut if line["part_name"].startswith("cabinet-front-")]
        assert [line["row_from_floor"] for line in fronts] == ["1"] * 6
        assert len(cut) == good["screening"]["part_count"]
        for columns in (5, 7):
            refused = plan(columns)
            assert refused["code"] == "BASE_DOOR_COUNT_CHANGED"
            assert refused["modules"] == [] and not refused["can_export_drafts"]
            assert "review_cut_list_csv" not in refused


def test_customer_module_plan_preserves_saved_source_and_blocks_unresolved_installation():
    original = json.loads(Path("tests/fixtures/furniture/bookcase-unresolved.json").read_text())
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": f"Module-plan-{uuid4()}"}
        ).json()
        path = f"/v1/furniture/projects/{project['id']}"
        saved = client.put(
            f"{path}/draft", headers=HEADERS,
            json={"expected_revision": 0, "workspace": original},
        )
        assert saved.status_code == 200, saved.text
        baseline = saved.json()
        response = client.post(
            "/v1/furniture/module-plan", headers=HEADERS,
            json={"workspace": baseline["workspace"], "grid": GRID},
        )
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["state"] == "available"
        assert plan["can_export_drafts"] is True
        assert plan["source"]["workspace"] == baseline["workspace"]
        assert plan["source_design_hash"] == baseline["preview"]["design"]["design_hash"]
        assert client.get(f"{path}/draft", headers=HEADERS).json() == baseline
        assert len(client.get(f"{path}/history", headers=HEADERS).json()["items"]) == 1
        assert len(plan["modules"]) == 10
        assert plan["physical_cutting_authorized"] is False
        assert plan["production_qualified"] is False
        assert plan["can_apply"] is False
        for module in plan["modules"]:
            workspace = module["workspace"]
            assert workspace["design"]["installation"] == original["design"]["installation"]
            assert module["dimensions_um"] == {
                "width_um": 868_000, "height_um": 1_270_000, "depth_um": 280_000,
            }
            assert module["preview"]["design"]["parts"]
            assert "INSTALLATION_ALLOWANCES_MISSING" in {
                requirement["code"] for requirement in module["requirements"]
            }
            with pytest.raises(ValueError, match="Ange utrymmet för list och montage"):
                shelving_production_spec(FurnitureWorkspace.model_validate(workspace))
        repeated = client.post(
            "/v1/furniture/module-plan", headers=HEADERS,
            json={"workspace": baseline["workspace"], "grid": GRID},
        )
        assert repeated.json() == plan


def test_module_plan_requires_authentication_explicit_gaps_and_bounded_grid():
    original = json.loads(Path("tests/fixtures/furniture/bookcase-unresolved.json").read_text())
    with TestClient(app) as client:
        assert client.post(
            "/v1/furniture/module-plan", json={"workspace": original, "grid": GRID}
        ).status_code == 401
        for grid in (
            {key: value for key, value in GRID.items() if key != "gap_um"},
            {**GRID, "columns": 8, "rows": 4, "shelf_count_per_row": [1, 1, 1, 1]},
            {**GRID, "gap_um": -1},
            {**GRID, "columns": True},
            {**GRID, "shelf_count_per_row": [4]},
        ):
            response = client.post(
                "/v1/furniture/module-plan", headers=HEADERS,
                json={"workspace": original, "grid": grid},
            )
            assert response.status_code == 422, response.text
