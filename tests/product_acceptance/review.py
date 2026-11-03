"""Measure the delivered review files, after the real worker finishes the saved job."""

import csv
import hashlib
import io
import json
import struct
import zipfile
from decimal import Decimal

import pytest
from custombuild_worker import tasks

from tests.furniture_review_storage import install_review_storage

from .support import response_json, verify_cut


def check_bundle(client, archive, draft, p, case, e, tmp_path):
    import cadquery as cq

    design = draft["preview"]["design"]
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["design_hash"] == design["design_hash"]
    assert manifest["revision"] == draft["revision"]
    assert manifest["physical_cutting_authorized"] is manifest["production_qualified"] is False
    names = archive.namelist()
    inventory = manifest["files"]
    assert sorted(names) == sorted(["manifest.json", *(item["path"] for item in inventory)])
    assert len(names) == len(set(names))
    for item in inventory:
        data = archive.read(item["path"])
        assert (
            len(data) == item["size_bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"]
        )
    assert not any(name.lower().endswith((".nc", ".ngc", ".gcode", ".tap")) for name in names)
    assert json.loads(archive.read("design/workspace.json")) == draft["workspace"]
    assert json.loads(archive.read("design/resolved.json")) == design
    assert archive.read("documents/part-drawings.pdf").startswith(b"%PDF-")
    assert archive.read("documents/assembly.pdf").startswith(b"%PDF-")
    glb = archive.read("design/model.glb")
    assert struct.unpack_from("<4sII", glb) == (b"glTF", 2, len(glb))

    step = tmp_path / "assembly.step"
    step.write_bytes(archive.read("design/model.step"))
    assembly = cq.importers.importStep(str(step)).val()
    bounds = assembly.BoundingBox()
    actual = [bounds.xlen, bounds.ylen, bounds.zlen]
    expected = [p[k] / 1000 for k in ("width_um", "depth_um", "height_um")]
    e.note("STEP_mm", {"actual": actual, "expected": expected, "tolerance_mm": 0.002})
    assert actual == pytest.approx(expected, abs=0.002, rel=0)
    assert [bounds.xmin, bounds.ymin, bounds.zmin] == pytest.approx([0, 0, 0], abs=0.002)
    parts = {part["part_id"]: part for part in design["parts"]}
    e.equal("STEP_solids", len(assembly.Solids()), len(parts))
    rows = list(csv.DictReader(io.StringIO(archive.read("bom/cut-list.csv").decode("utf-8-sig"))))
    assert sorted(r["part_id"] for r in rows) == sorted(parts)
    face_count = 0
    for row in rows:
        part = parts[row["part_id"]]
        verify_cut(row, part)
        sheet = response_json(
            client.post(
                "/v1/furniture/inspect-part",
                json={
                    "workspace": draft["workspace"],
                    "expected_design_hash": design["design_hash"],
                    "part_id": part["part_id"],
                },
            )
        )
        assert sheet["physical_cutting_authorized"] is False and sheet["part"] == part
        assert {mate["joint"]["joint_id"] for mate in sheet["mates"]} == {
            j["joint_id"]
            for j in design["joints"]
            if any(m["part_id"] == part["part_id"] for m in j["members"])
        }
        face_count += len(sheet["drawings"])
        for view in sheet["drawings"]:
            name = f"parts/{part['part_id']}/{view['view']}.svg"
            assert view["svg"].encode() == archive.read(name), name
        assert len(sheet["sheet_sha256"]) == 64
        prefix = f"parts/{part['part_id']}/"
        for face in ("A", "B"):
            assert prefix + face + ".dxf" in names and prefix + face + ".svg" in names
        for feature in part["features"]:
            face = {part["a_side"]: "A", part["b_side"]: "B"}.get(
                feature["face"], feature["face"].upper()
            )
            drawing = archive.read(prefix + face + ".dxf")
            assert ("FEATURE:" + feature["feature_id"]).encode() in drawing
        if case.product == "dining-round" and part["semantic_key"] == "table-top":
            # Parse entity, layer, centre and radius as actual DXF group codes.
            lines = archive.read(prefix + "A.dxf").decode().splitlines()
            tags = list(zip(lines[::2], lines[1::2], strict=True))
            circles = []
            for i, tag in enumerate(tags):
                if tag == ("0", "CIRCLE"):
                    entity = {}
                    for code, value in tags[i + 1 :]:
                        if code == "0":
                            break
                        entity[code] = value
                    if entity.get("8") == "OUTLINE":
                        circles.append([Decimal(entity[key]) for key in ("10", "20", "40")])
            assert circles == [[Decimal(p["width_um"]) / 2000] * 3]
    e.equal("inspected_parts", len(rows), len(parts))
    e.note("verified_face_drawings", face_count)
    e.note("verified_archive", {"files": len(names), "cut_rows": len(rows)})


def verify_download(client, path, draft, request, p, case, e, monkeypatch, tmp_path):
    install_review_storage(monkeypatch)
    exports = f"{path}/exports"
    stale = response_json(client.post(exports, json={**request, "expected_revision": 99}), 409)
    assert "aktuella revisionen" in stale["detail"]
    queued = response_json(client.post(exports, json=request), 202)
    job = queued["job_id"]
    assert response_json(client.post(exports, json=request), 202)["job_id"] == job
    result = tasks.generate_furniture_review.run(job, "11111111-1111-4111-8111-111111111111")
    assert result["state"] == "succeeded", result
    status = response_json(client.get(f"{exports}/{job}"))
    assert status["state"] == "succeeded" and status["revision"] == draft["revision"]
    assert status["physical_cutting_authorized"] is False
    url = status["download_path"]
    assert (
        client.get(url, headers={"Authorization": "Bearer demo-atelier-owner"}).status_code == 404
    )
    downloaded = client.get(url)
    assert downloaded.status_code == 200
    payload = downloaded.content
    assert hashlib.sha256(payload).hexdigest() == status["sha256"]
    assert len(payload) == status["size_bytes"]
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        check_bundle(client, archive, draft, p, case, e, tmp_path)
