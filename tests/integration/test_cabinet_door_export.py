import csv
import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from app.main import app
from custombuild_cad.bookcase_motion import verify_assembly_space
from custombuild_domain.enums import AssemblyDirection
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_worker import tasks
from custombuild_worker.bookcase_readback import verify_panel_documents, verify_panels_from_order
from custombuild_worker.cabinet_delivery import verify_cabinet_delivery
from fastapi.testclient import TestClient
from pypdf import PdfReader

from tests.furniture_review_storage import install_review_storage

HEADERS = {"Authorization": "Bearer demo-nordic-owner"}
TENANT = "11111111-1111-4111-8111-111111111111"


def test_previous_cabinet_without_angles_keeps_its_original_joints():
    workspace = json.loads(Path("examples/furniture/bookcase-walnut-base-700.json").read_bytes())
    workspace["design"]["intent"].pop("trial_angle")
    with TestClient(app) as client:
        saved = client.post(
            "/v1/furniture/projects",
            headers=HEADERS,
            json={"name": str(uuid4()), "workspace": workspace},
        )
        assert saved.status_code == 201, saved.text
        body = saved.json()
        path = f"/v1/furniture/projects/{body['project']['id']}"
        eligibility = client.get(path + "/workshop-eligibility", headers=HEADERS).json()
        assert not eligibility["available"] and "91 bärande stomfogar" in eligibility["detail"]
        order = client.post(
            path + "/exports",
            headers=HEADERS,
            json={
                "expected_revision": body["draft"]["revision"],
                "expected_design_hash": eligibility["design_hash"],
                "purpose": "workshop_drawings",
            },
        )
        assert order.status_code == 409 and order.json()["detail"] == eligibility["detail"]


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
        assert blocked.status_code == 409 and "uppmätt" in blocked.json()["detail"]
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
        preview = draft["preview"]
        rule = next(r for r in preview["rules"]["evaluations"] if r["rule_id"] == "CB-HARDWARE-001")
        assert rule["status"] == "WARNING"
        assert rule["values"]["unit"] == "fysiskt oprövade beslagssystem"
        assert all(sku in rule["detail"] for sku in ("J95-C24/0T-NI", "J95-P6T-NI", "SPAX"))
        assert "Provning av öppningsrörelse" in rule["detail"]
        handoff = json.loads(files["manufacturing/workshop-handoff.json"])
        assert handoff == preview["workshop_handoff"]
        assert [(o["bay"], o["zone"], o["state"]) for o in handoff["rear_openings"]] == [
            (bay, zone, state)
            for zone, state in (("upper", "covered"), ("base", "open"))
            for bay in range(1, 7)
        ]
        assert set(handoff["hardware_requirements"]) == {
            "hinge_and_plate_fit_in_actual_stock",
            "door_opening_clearance_with_neighbour_and_wall",
            "screw_withdrawal_in_actual_walnut",
            "loaded_door_cycles_and_moisture_movement",
        }
        report = json.loads(files["validation/review.json"])
        assert report["rules"] == preview["rules"]
        assert report["dependencies"]["hardware"] == preview["dependencies"]["hardware"]
        protocol = files["inspection/provprotokoll.txt"].decode()
        assert rule["detail"] in protocol and "inspection/gangjarnsprov.txt" in protocol
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
        assert proof["carcass_joints_without_retention"] == []
        assert (proof["trial_retention_angles"], proof["trial_retention_pilots"]) == (91, 182)
        pages = [
            p.extract_text() for p in PdfReader(io.BytesIO(files["documents/assembly.pdf"])).pages
        ]
        brackets = [
            i for i, p in enumerate(pages) if "Fäst låsvinklarna efter stommonteringen" in p
        ]
        doors = [i for i, p in enumerate(pages) if "Haka på luckan i öppet läge" in p]
        assert len(doors) == 6 and len(brackets) == 12 and max(brackets) < min(doors)
        space = json.loads(files["assembly/required-space.json"])
        assert (space["world_min_um"], space["world_max_um"]) == (
            [-6000, -248000, 0],
            [4286000, 260000, 2518000],
        )
        carcass = result.shelving_result
        steps = carcass.assembly_graph.steps
        assert [s["step_number"] for s in space["steps"]] == [s.step_number for s in steps[:-6]]
        assert all(s.joint_ids == () for s in steps[-6:])
        # Lowering a shelf from above after the top is fitted crosses the upper panel.
        shelf = next(p for p in result.parts if p.semantic_key == "shelf-r0-b0")
        modified = tuple(
            step.model_copy(
                update={
                    "direction": AssemblyDirection.NEG_Z,
                    "motion_path": (AssemblyDirection.NEG_Z,),
                }
            )
            if shelf.part_id in step.moving_part_ids
            else step
            for step in steps
        )
        wrong_plan = carcass.model_copy(
            update={"assembly_graph": carcass.assembly_graph.model_copy(update={"steps": modified})}
        )
        with pytest.raises(ValueError, match="fri införingsväg"):
            verify_assembly_space(wrong_plan, files["design/model.step"])
        # Detect a consistent model/drawing error that would pass comparison with the model alone.
        altered = shelf.model_copy(
            update={
                "placement": shelf.placement.model_copy(
                    update={"z_um": shelf.placement.z_um + 1000}
                )
            }
        )
        with pytest.raises(ValueError, match="placering avviker"):
            verify_panels_from_order(
                result.model_copy(
                    update={"parts": tuple(altered if p == shelf else p for p in result.parts)}
                )
            )
        intent_path = "manufacturing/manufacturing-intent.json"
        intent = json.loads(files[intent_path])
        intent["parts"][0]["axis_mapping"]["thickness_axis"] = "wrong"
        with pytest.raises(ValueError, match="ämnesmått"):
            verify_panel_documents(result, {**files, intent_path: json.dumps(intent).encode()})
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
    "damage",
    [
        "missing",
        "duplicate",
        "wrong_support",
        "wrong_bores",
        "wrong_screws",
        "wrong_material_batch",
    ],
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
        if damage == "wrong_material_batch":
            name = "assembly/parts.csv"
            rows = list(csv.DictReader(io.StringIO(files[name].decode("utf-8-sig"))))
            rows[0]["material_batch_id"] = "unrelated-lot"
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
            files[name] = buffer.getvalue().encode("utf-8-sig")
            return files
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
        with pytest.raises(ValueError, match="förbindels|Dubblerade|batch"):
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
