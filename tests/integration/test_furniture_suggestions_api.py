from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from app.main import app
from fastapi.testclient import TestClient

HEADERS = {"Authorization": "Bearer demo-nordic-owner"}


def test_customer_shelf_proposal_is_read_only_until_explicitly_saved():
    original = json.loads(Path("examples/furniture/bookcase-4340x2540x280.json").read_text())
    original["design"]["intent"].update(shelf_load_basis="per_metre", shelf_load_per_metre_n=300)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": f"Shelf-proposal-{uuid4()}"}
        ).json()
        path = f"/v1/furniture/projects/{project['id']}"
        saved = client.put(
            f"{path}/draft",
            headers=HEADERS,
            json={"expected_revision": 0, "workspace": original},
        )
        assert saved.status_code == 200, saved.text
        baseline = saved.json()
        response = client.post(
            "/v1/furniture/shelf-bay-suggestion",
            headers=HEADERS,
            json=baseline["workspace"],
        )
        assert response.status_code == 200, response.text
        suggestion = response.json()
        assert suggestion["state"] == "available"
        assert suggestion["current"] == baseline["preview"]
        assert client.get(f"{path}/draft", headers=HEADERS).json() == baseline
        assert len(client.get(f"{path}/history", headers=HEADERS).json()["items"]) == 1
        proposed = suggestion["proposed"]["workspace"]
        for key in (
            "width_um",
            "height_um",
            "depth_um",
            "shelf_load_basis",
            "shelf_load_per_metre_n",
        ):
            assert proposed["design"]["intent"][key] == original["design"]["intent"][key]
        assert proposed["design"]["installation"] == baseline["workspace"]["design"]["installation"]
        assert proposed["manufacturing"] == baseline["workspace"]["manufacturing"]
        assert not suggestion["production_qualified"]
        assert not suggestion["physical_cutting_authorized"]
        assert (
            suggestion["proposed"]["workshop_handoff"]["dimensions"]["state"]
            == "requires_resolution"
        )
        reviewed = client.post("/v1/furniture/preview", headers=HEADERS, json=proposed)
        assert reviewed.json() == suggestion["proposed"]
        applied = client.put(
            f"{path}/draft",
            headers=HEADERS,
            json={"expected_revision": baseline["revision"], "workspace": proposed},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["revision"] == 2
        assert len(client.get(f"{path}/history", headers=HEADERS).json()["items"]) == 2


def test_shelf_suggestion_requires_authentication_and_valid_geometry():
    original = json.loads(Path("examples/furniture/bookcase-4340x2540x280.json").read_text())
    with TestClient(app) as client:
        assert client.post("/v1/furniture/shelf-bay-suggestion", json=original).status_code == 401
        original["design"]["intent"]["width_um"] = -1
        assert (
            client.post(
                "/v1/furniture/shelf-bay-suggestion", headers=HEADERS, json=original
            ).status_code
            == 422
        )
