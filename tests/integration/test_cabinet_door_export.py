import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from app.main import app
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_worker import tasks
from custombuild_worker.cabinet_delivery import verify_cabinet_delivery
from fastapi.testclient import TestClient

from tests.furniture_review_storage import install_review_storage

HEADERS = {"Authorization": "Bearer demo-nordic-owner"}
TENANT = "11111111-1111-4111-8111-111111111111"


def test_saved_cabinet_revision_download_and_direct_production_gate(monkeypatch, tmp_path):
    storage = install_review_storage(monkeypatch)
    workspace = json.loads(Path("examples/furniture/bookcase-walnut-base-700.json").read_bytes())
    with TestClient(app) as client:
        project = client.post("/v1/projects", headers=HEADERS, json={"name": str(uuid4())}).json()
        route = f"/v1/furniture/projects/{project['id']}"
        saved = client.put(
            route + "/draft",
            headers=HEADERS,
            json={
                "expected_revision": 0,
                "workspace": workspace,
            },
        )
        assert saved.status_code == 200, saved.text
        draft = saved.json()
        body = {
            "expected_revision": 1,
            "expected_design_hash": draft["preview"]["design"]["design_hash"],
        }
        blocked = client.post(
            route + "/exports", headers=HEADERS, json={**body, "purpose": "workshop_drawings"}
        )
        assert blocked.status_code == 409 and "J95" in blocked.text
        queued = client.post(route + "/exports", headers=HEADERS, json=body)
        assert queued.status_code == 202, queued.text
        job = queued.json()["job_id"]
        assert tasks.generate_furniture_review.run(job, TENANT)["state"] == "succeeded"
        downloaded = client.get(route + f"/exports/{job}/download", headers=HEADERS)
        assert downloaded.status_code == 200
        assert len(storage.put_calls) == 1
        (tmp_path / "Custombuild-Valnot-Sex-Gangjarnsluckor.zip").write_bytes(downloaded.content)
        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
            files = {p: archive.read(p) for p in archive.namelist()}
        proof = json.loads(files["inspection/door-hardware.json"])
        assert (proof["doors"], proof["hinges"], proof["plates"], proof["screws"]) == (
            6,
            12,
            12,
            72,
        )
        assert (proof["door_and_plate_bores"], proof["step_wood_parts"], proof["dxf_faces"]) == (
            90,
            62,
            124,
        )
        assert proof["design_hash"] == body["expected_design_hash"]
        assert json.loads(files["manifest.json"])["production_qualified"] is False
        result = build_furniture(FurnitureWorkspace.model_validate(draft["workspace"]).design)
        front = next(p for p in result.parts if p.semantic_key == "cabinet-front-0")
        path = f"parts/{front.part_id}/B.dxf"
        good = files[path]
        # Mutate an actual CIRCLE coordinate without changing its descriptive metadata.
        lines = good.decode().splitlines()
        for index in range(0, len(lines) - 6, 2):
            if lines[index].strip() == "0" and lines[index + 1] == "CIRCLE":
                while lines[index].strip() != "10":
                    index += 2
                lines[index + 1] = str(float(lines[index + 1]) + 1)
                break
        files[path] = ("\n".join(lines) + "\n").encode()
        with pytest.raises(ValueError, match="DXF-geometrin"):
            verify_cabinet_delivery(result, files)
        files[path] = good.replace(b'"revision":1', b'"revision":2')
        with pytest.raises(ValueError, match="fel order eller revision"):
            verify_cabinet_delivery(result, files)
        files[path] = good
        files["bom/hardware.csv"] = files["bom/hardware.csv"].replace(
            b"J95-P6T-NI,12", b"J95-P6T-NI,11"
        )
        with pytest.raises(ValueError, match="Fel artikel eller antal"):
            verify_cabinet_delivery(result, files)


def test_worker_rejects_missing_plate_pilot_before_object_storage(monkeypatch):
    from custombuild_worker import furniture_review

    storage = install_review_storage(monkeypatch)
    workspace = json.loads(Path("examples/furniture/bookcase-walnut-base-700.json").read_bytes())
    workspace["design"].pop("installation")
    workspace["design"]["intent"].update(
        width_um=800_000, height_um=1200_000, divider_count=0, shelf_count=1
    )
    export_dxf = furniture_review.dxf_for_part

    def broken(part, side, **kwargs):
        raw = export_dxf(part, side, **kwargs)
        if any(f.feature_id == "J95-1-1-plate-1-1" and f.side == side for f in part.features):
            # Replace the real bore entity type; leave its operation metadata untouched.
            return raw.replace(b"\nCIRCLE\n", b"\nPOINT\n", 1)
        return raw

    monkeypatch.setattr(furniture_review, "dxf_for_part", broken)
    with TestClient(app) as client:
        project = client.post("/v1/projects", headers=HEADERS, json={"name": str(uuid4())}).json()
        route = f"/v1/furniture/projects/{project['id']}"
        saved = client.put(
            route + "/draft",
            headers=HEADERS,
            json={
                "expected_revision": 0,
                "workspace": workspace,
            },
        )
        assert saved.status_code == 200, saved.text
        queued = client.post(
            route + "/exports",
            headers=HEADERS,
            json={
                "expected_revision": 1,
                "expected_design_hash": saved.json()["preview"]["design"]["design_hash"],
            },
        )
        assert queued.status_code == 202
        job_id = queued.json()["job_id"]
        with pytest.raises(ValueError, match="Oväntad DXF-geometri"):
            tasks.generate_furniture_review.run(job_id, TENANT)
        assert client.get(route + f"/exports/{job_id}", headers=HEADERS).json()["state"] == "failed"
        assert not storage.put_calls
        assert client.get(route + f"/exports/{job_id}/download", headers=HEADERS).status_code == 404
