from __future__ import annotations

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


def test_customer_module_plan_preserves_saved_source_and_blocks_unresolved_installation():
    original = json.loads(Path("examples/furniture/bookcase-4340x2540x280.json").read_text())
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
    original = json.loads(Path("examples/furniture/bookcase-4340x2540x280.json").read_text())
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
