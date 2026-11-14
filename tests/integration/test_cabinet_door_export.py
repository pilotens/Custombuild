import csv
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
from pypdf import PdfReader

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
        assert proof["hinge_connections"] == 12
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
        connections = files["assembly/connections.csv"]
        hinges = {
            r["joint_id"]: r
            for r in csv.DictReader(io.StringIO(connections.decode("utf-8-sig")))
            if r["hardware"] == "J95-C24/0T-NI"
        }
        assert len(hinges) == 12
        overview = "\n".join(
            p.extract_text()
            for p in PdfReader(io.BytesIO(files["documents/assembly-overview.pdf"])).pages
        )
        for i in range(6):
            door = f"cabinet-front-{i}"
            support = "left-side" if i == 0 else f"base-side-{i}"
            assert f"{door} / {support}\nJ95-C24/0T-NI\n2\n" in overview
            for position in (1, 2):
                row = hinges[f"J95-{i + 1}-{position}"]
                assert (row["part_1"], row["part_2"]) == (door, support)
                assert (row["quantity"], row["plate_quantity"], row["screw_quantity"]) == (
                    "1",
                    "1",
                    "6",
                )
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


@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "wrong_support", "wrong_bores", "wrong_screws"]
)
def test_hinge_map_damage_is_rejected_before_storage(monkeypatch, damage):
    from custombuild_worker import furniture_review

    storage = install_review_storage(monkeypatch)
    workspace = json.loads(Path("examples/furniture/bookcase-walnut-base-700.json").read_bytes())
    workspace["design"].pop("installation")
    workspace["design"]["intent"].update(
        width_um=1_054_001,
        height_um=1_200_000,
        divider_count=1,
        bay_width_ratios_ppm=[200_000, 800_000],
        shelf_count=1,
    )
    original = furniture_review.assembly_documents

    def damaged_register(result):
        files = original(result)
        name = "assembly/connections.csv"
        lines = files[name].splitlines(keepends=True)
        index = next(i for i, line in enumerate(lines) if line.startswith(b"J95-2-1,"))
        row = lines[index]
        lines[index] = {
            "missing": b"",
            "duplicate": row + row,
            "wrong_support": row.replace(b",base-side-1,right,", b",left-side,right,"),
            "wrong_bores": row.replace(b"J95-2-1-cup-screw-1", b"J95-2-2-cup-screw-1"),
            "wrong_screws": row.replace(b",6\n", b",5\n"),
        }[damage]
        assert lines[index] != row
        files[name] = b"".join(lines)
        return files

    monkeypatch.setattr(furniture_review, "assembly_documents", damaged_register)
    with TestClient(app) as client:
        project = client.post("/v1/projects", headers=HEADERS, json={"name": str(uuid4())}).json()
        route = f"/v1/furniture/projects/{project['id']}"
        draft = client.put(
            route + "/draft", headers=HEADERS, json={"expected_revision": 0, "workspace": workspace}
        )
        assert draft.status_code == 200, draft.text
        queued = client.post(
            route + "/exports",
            headers=HEADERS,
            json={
                "expected_revision": 1,
                "expected_design_hash": draft.json()["preview"]["design"]["design_hash"],
            },
        )
        assert queued.status_code == 202, queued.text
        job = queued.json()["job_id"]
        with pytest.raises(ValueError, match="förbindels|Dubblerade"):
            tasks.generate_furniture_review.run(job, TENANT)
        assert not storage.put_calls
        assert client.get(route + f"/exports/{job}", headers=HEADERS).json()["state"] == "failed"
        assert client.get(route + f"/exports/{job}/download", headers=HEADERS).status_code == 404


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
