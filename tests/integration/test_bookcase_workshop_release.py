"""Normal customer job and direct download boundaries; certificates here exist only in tmp_path."""

import hashlib
import io
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zipfile import ZipFile

import custombuild_manufacturing.bookcase_qualification as qualifications
import custombuild_worker.furniture_review as exporter
import pytest
from app.db import get_session_factory
from app.main import app
from app.models import GenerationJob, Project
from custombuild_cad import CadQueryAdapter
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_domain.identity import content_hash
from custombuild_manufacturing.bookcase_test_results import bookcase_test_limits
from custombuild_manufacturing.model import um_to_mm
from custombuild_rules.furniture import evaluate_furniture
from custombuild_worker import tasks
from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas

from tests.furniture_review_storage import install_review_storage
from tests.integration.test_furniture_workspace import HEADERS, OTHER, TENANT
from tests.unit.test_bookcase_angles import trial


def measured_order():
    w = trial(shelf_count=1, divider_count=0, height_um=900_000, depth_um=500_000, shelf_load_n=50)
    source = w.model_dump(mode="json")
    design = source["design"]
    design["installation"] = dict(
        width_um=1_000_000,
        height_um=1_100_000,
        depth_um=520_000,
        left_allowance_um=50_000,
        right_allowance_um=50_000,
        top_allowance_um=200_000,
        rear_allowance_um=20_000,
        bottom_allowance_um=0,
        front_allowance_um=0,
        additional_bounded_walls=["left", "right", "rear"],
        measured_ceiling_height_um=1_100_000,
        measured_front_space_um=600_000,
        qualified_site_ref="test-fixture",
        measured_left_wall_um=50_000,
        measured_right_wall_um=50_000,
        measured_rear_wall_um=20_000,
    )
    design["material"]["batch_id"] = "test-stock"
    design["back_material"]["batch_id"] = "test-back"
    return FurnitureWorkspace.model_validate(source)


def _install_test_profile(tmp_path, monkeypatch, workspace):
    monkeypatch.setattr(qualifications, "REGISTRY", tmp_path)
    slug = "synthetic-test-only"
    folder = tmp_path / slug
    folder.mkdir()
    docs = []
    for category in sorted(qualifications.REQUIRED_REPORTS):
        file = category + ".pdf"
        canvas = Canvas(str(folder / file))
        canvas.drawString(30, 780, "SYNTHETIC FIXTURE ONLY - " + category)
        canvas.save()
        docs.append(
            {
                "category": category,
                "file": file,
                "sha256": hashlib.sha256((folder / file).read_bytes()).hexdigest(),
            }
        )
    now = datetime.now(UTC)
    model = build_furniture(workspace.design)
    limits = bookcase_test_limits(model.shelving_result, evaluate_furniture(model)["evaluations"])
    profile = dict(
        profile_id=slug,
        organization_id=TENANT,
        scope_sha256=qualifications.bookcase_scope(workspace),
        evidence=docs,
        physical_results=dict(
            schema_version="custombuild.bookcase-physical-results.v1",
            issuer="Test fixture only",
            report_reference="Synthetic physical case A",
            tested_on=(now - timedelta(days=3)).date().isoformat(),
            joint_design_shear_capacity_n=limits.joint_shear_n,
            joint_withdrawal_demand_n=100,
            joint_design_withdrawal_capacity_n=100,
            verified_row_payload_n=limits.row_payload_n,
            measured_shelf_deflection_um=limits.maximum_shelf_deflection_um - 100,
            deflection_uncertainty_um=100,
            tested_horizontal_force_n=limits.horizontal_force_n,
            verified_tip_factor_permille=limits.minimum_tip_factor_permille,
            completed_assembly_steps=list(limits.assembly_steps),
        ),
        valid_from=(now - timedelta(days=1)).isoformat(),
        valid_until=(now + timedelta(days=1)).isoformat(),
    )
    (tmp_path / "profiles.json").write_text(json.dumps([profile]))
    return profile


def create(client, order):
    response = client.post(
        "/v1/furniture/projects",
        headers=HEADERS,
        json={"name": f"Verkstadsprov-{uuid4()}", "workspace": order.model_dump(mode="json")},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    draft = created["draft"]
    return (
        f"/v1/furniture/projects/{created['project']['id']}",
        {
            "expected_revision": draft["revision"],
            "expected_design_hash": draft["preview"]["design"]["design_hash"],
        },
    )


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("width_um", "bredd"),
        ("height_um", "höjd"),
        ("depth_um", "djup"),
        ("divider_count", "avdelare"),
        ("shelf_count", "hyllrader"),
        ("shelf_load_n", "nyttolast per hyllrad"),
        ("shelf_load_per_metre_n", "nyttolast per meter"),
    ],
)
def test_incomplete_api_order_never_inherits_a_qualified_default(
    tmp_path, monkeypatch, field, message
):
    order = measured_order()
    _install_test_profile(tmp_path, monkeypatch, order)
    partial = order.model_dump(mode="json")
    intent = partial["design"]["intent"]
    if field == "shelf_load_per_metre_n":
        intent["shelf_load_basis"] = "per_metre"
    intent.pop(field, None)
    with TestClient(app) as client:
        created = client.post(
            "/v1/furniture/projects",
            headers=HEADERS,
            json={"name": "Ofullständig", "workspace": partial},
        )
        assert created.status_code == 422 and message in created.json()["detail"]
        path, bound = create(client, order)
        saved = client.put(
            path + "/draft",
            headers=HEADERS,
            json={"expected_revision": bound["expected_revision"], "workspace": partial},
        )
        assert saved.status_code == 422 and message in saved.json()["detail"]
        assert client.get(path + "/draft", headers=HEADERS).json()["revision"] == 1


def test_explicit_zero_per_metre_survives_reopen_and_resave():
    workspace = measured_order().model_dump(mode="json")
    workspace["design"]["intent"].update(shelf_load_basis="per_metre", shelf_load_per_metre_n=0)
    with TestClient(app) as client:
        created = client.post(
            "/v1/furniture/projects",
            headers=HEADERS,
            json={"name": "Nollast", "workspace": workspace},
        )
        assert created.status_code == 201, created.text
        path = f"/v1/furniture/projects/{created.json()['project']['id']}/draft"
        current = client.get(path, headers=HEADERS).json()
        assert current["workspace"]["design"]["intent"]["shelf_load_per_metre_n"] == 0
        saved = client.put(
            path,
            headers=HEADERS,
            json={
                "expected_revision": 1,
                "workspace": current["workspace"],
            },
        )
        assert saved.status_code == 200 and saved.json()["revision"] == 1


@pytest.mark.cad
def test_historic_order_requires_an_explicit_resave_before_production(tmp_path, monkeypatch):
    order = measured_order()
    _install_test_profile(tmp_path, monkeypatch, order)
    storage = install_review_storage(monkeypatch)
    with TestClient(app) as client:
        path, bound = create(client, order)
        exports = path + "/exports"
        request = {**bound, "purpose": "workshop_drawings"}
        job = client.post(exports, headers=HEADERS, json=request).json()["job_id"]
        assert tasks.generate_furniture_review.run(job, TENANT)["state"] == "succeeded"
        old_download = f"{exports}/{job}/download"
        assert client.get(old_download, headers=HEADERS).status_code == 200
        with get_session_factory().begin() as session:
            project = session.get(Project, path.rsplit("/", 1)[1])
            project.draft_workspace_json = {"manufacturing": None}
        assert client.get(old_download, headers=HEADERS).status_code == 409
        refused = client.post(exports, headers=HEADERS, json=request)
        assert refused.status_code == 409 and "spara en ny revision" in refused.json()["detail"]
        assert client.post(exports, headers=HEADERS, json=bound).status_code == 202
        current = client.get(path + "/draft", headers=HEADERS).json()
        resaved = client.put(
            path + "/draft",
            headers=HEADERS,
            json={
                "expected_revision": 1,
                "workspace": current["workspace"],
            },
        )
        assert resaved.status_code == 200 and resaved.json()["revision"] == 2
        request.update(
            expected_revision=2,
            expected_design_hash=resaved.json()["preview"]["design"]["design_hash"],
        )
        new = client.post(exports, headers=HEADERS, json=request)
        assert new.status_code == 202
        current_job = new.json()["job_id"]
        assert tasks.generate_furniture_review.run(current_job, TENANT)["state"] == "succeeded"
        assert len(storage.put_calls) == 2
        assert client.get(f"{exports}/{current_job}/download", headers=HEADERS).status_code == 200
        assert client.get(old_download, headers=HEADERS).status_code == 409


def test_worker_cannot_publish_a_preexisting_job_without_an_input_receipt(tmp_path, monkeypatch):
    order = measured_order()
    _install_test_profile(tmp_path, monkeypatch, order)
    storage = install_review_storage(monkeypatch)
    with TestClient(app) as client:
        path, bound = create(client, order)
        exports = path + "/exports"
        job = client.post(
            exports, headers=HEADERS, json={**bound, "purpose": "workshop_drawings"}
        ).json()["job_id"]
        with get_session_factory().begin() as session:
            row = session.get(GenerationJob, job)
            row.request_json = {
                k: v for k, v in row.request_json.items() if k != "bookcase_order_sha256"
            }
            row.production_engine_context_json = {
                k: v
                for k, v in row.production_engine_context_json.items()
                if k != "bookcase_order_sha256"
            }
            row.production_context_hash = content_hash(row.request_json)
        with pytest.raises(tasks.ProductionBlockedError, match="explicitly saved order inputs"):
            tasks.generate_furniture_review.run(job, TENANT)
        assert not storage.put_calls
        assert client.get(f"{exports}/{job}/download", headers=HEADERS).status_code == 404


@pytest.mark.cad
@pytest.mark.parametrize("fault", ["shifted-pilot", "open-groove", "stale-drawing", "step-pilot"])
def test_normal_workshop_job_stops_bad_geometry_before_storage(tmp_path, monkeypatch, fault):
    w = measured_order()
    _install_test_profile(tmp_path, monkeypatch, w)
    storage = install_review_storage(monkeypatch)
    changed = []
    original = exporter.dxf_for_part
    previous_drawings = {}

    def damaged(part, side, *, order_identity):
        if fault == "stale-drawing" and previous_drawings and not changed:
            changed.append(part.part_id)
            return previous_drawings[f"parts/{part.part_id}/{side.value}.dxf"]
        data = original(part, side, order_identity=order_identity)
        if fault == "shifted-pilot" and not changed:
            pattern = rb"(8\nDRILL\n100\nAcDbCircle\n10\n)([0-9.]+)\n"

            def move(match):
                changed.append(part.part_id)
                return match[1] + str(float(match[2]) + 1).encode() + b"\n"

            return re.sub(pattern, move, data, count=1)
        if fault == "open-groove" and not changed:
            for f in part.features:
                if (
                    f.side != side
                    or not f.metadata.get("front_stop_clearance_um")
                    or not f.width_um
                ):
                    continue
                end = f.x_um + f.width_um
                if end >= part.width_um:
                    continue
                marker = f"999\nFEATURE:{f.feature_id}\n".encode()
                before, sep, body = data.partition(marker)
                needle = f"\n10\n{um_to_mm(end)}\n".encode()
                if sep and needle in body:
                    changed.append(part.part_id)
                    return (
                        before
                        + sep
                        + body.replace(needle, f"\n10\n{um_to_mm(part.width_um)}\n".encode(), 2)
                    )
        return data

    monkeypatch.setattr(exporter, "dxf_for_part", damaged)
    if fault == "step-pilot":
        make_panel = CadQueryAdapter._part_shape

        def misplaced(self, cq, part, design=None):
            original_features = part.features
            if part.semantic_key == "left-side":
                edits = []
                for f in original_features:
                    if f.kind.value == "drill":
                        f = f.model_copy(
                            update={
                                "origin": f.origin.model_copy(update={"y_um": f.origin.y_um + 1000})
                            }
                        )
                        changed.append(part.part_id)
                    edits.append(f)
                part = part.model_copy(update={"features": tuple(edits)})
            return make_panel(self, cq, part, design)

        monkeypatch.setattr(CadQueryAdapter, "_part_shape", misplaced)
    with TestClient(app) as client:
        if fault == "stale-drawing":
            first = w.model_copy(
                update={
                    "design": w.design.model_copy(
                        update={"intent": w.design.intent.model_copy(update={"shelf_load_n": 49})}
                    )
                }
            )
            path, body = create(client, first)
            old = client.post(path + "/exports", headers=HEADERS, json=body).json()["job_id"]
            assert tasks.generate_furniture_review.run(old, TENANT)["state"] == "succeeded"
            raw = client.get(f"{path}/exports/{old}/download", headers=HEADERS)
            with ZipFile(io.BytesIO(raw.content)) as z:
                previous_drawings.update({n: z.read(n) for n in z.namelist() if n.endswith(".dxf")})
            saved = client.put(
                path + "/draft",
                headers=HEADERS,
                json={"expected_revision": 1, "workspace": w.model_dump(mode="json")},
            )
            assert saved.status_code == 200, saved.text
            body = {
                "expected_revision": 2,
                "expected_design_hash": saved.json()["preview"]["design"]["design_hash"],
            }
        else:
            path, body = create(client, w)
        previous_uploads = len(storage.put_calls)
        response = client.post(
            path + "/exports", headers=HEADERS, json={**body, "purpose": "workshop_drawings"}
        )
        assert response.status_code == 202, response.text
        job = response.json()["job_id"]
        reason = {
            "shifted-pilot": "DXF-geometrin",
            "open-groove": "DXF-geometrin",
            "stale-drawing": "fel order eller revision",
            "step-pilot": "STEP avviker",
        }
        with pytest.raises(ValueError, match=reason[fault]):
            tasks.generate_furniture_review.run(job, TENANT)
        assert client.get(f"{path}/exports/{job}", headers=HEADERS).json()["state"] == "failed"
        assert changed and len(storage.put_calls) == previous_uploads
        assert client.get(f"{path}/exports/{job}/download", headers=HEADERS).status_code == 404


@pytest.mark.cad
def test_exact_profile_releases_current_revision_and_revocation_closes_direct_api(
    tmp_path, monkeypatch
):
    w = measured_order()
    install_review_storage(monkeypatch)
    with TestClient(app) as client:
        path, body = create(client, w)
        request = {**body, "purpose": "workshop_drawings"}
        assert client.post(path + "/exports", headers=HEADERS, json=request).status_code == 409
        profile = _install_test_profile(tmp_path, monkeypatch, w)
        # Identical design reservation cannot stand in for the measured installation.
        unknown = w.model_copy(
            update={
                "design": w.design.model_copy(
                    update={
                        "installation": w.design.installation.model_copy(
                            update={"measured_left_wall_um": None}
                        )
                    }
                )
            }
        )
        with pytest.raises(ValueError, match="Mät avståndet"):
            qualifications.qualified_bookcase(unknown, TENANT)

        r = client.post(path + "/exports", headers=HEADERS, json=request)
        assert r.status_code == 202, r.text
        job = r.json()["job_id"]
        assert client.post(path + "/exports", headers=HEADERS, json=request).json()["job_id"] == job
        assert client.post(path + "/exports", headers=HEADERS, json=body).json()["job_id"] != job
        assert tasks.generate_furniture_review.run(job, TENANT)["state"] == "succeeded"
        assert client.post(path + "/exports", headers=HEADERS, json=request).json()["job_id"] == job
        download = f"{path}/exports/{job}/download"
        assert client.get(download, headers=OTHER).status_code == 404
        response = client.get(download, headers=HEADERS)
        assert response.status_code == 200, (
            response.text[:200] if response.status_code != 200 else ""
        )
        with ZipFile(io.BytesIO(response.content)) as z:
            m = json.loads(z.read("manifest.json"))
            physical = json.loads(z.read("inspection/kvalificeringsprov.json"))
            assert physical["type_test_results"] == profile["physical_results"]
            assert (
                physical["order_requirements"]["joint_shear_n"]
                == profile["physical_results"]["joint_design_shear_capacity_n"]
            )
            report = json.loads(z.read("inspection/geometry-readback.json"))
            assert m["production_qualified"] and not m["physical_cutting_authorized"]
            assert (
                m["qualification"] == profile["profile_id"]
                and report["design_hash"] == body["expected_design_hash"]
            )
            assert (
                json.loads(z.read("inspection/qualification.json"))["scope_sha256"]
                == profile["scope_sha256"]
            )
            assert (
                z.read("documents/installation.pdf")
                == (tmp_path / profile["profile_id"] / "installation.pdf").read_bytes()
            )
        registry = tmp_path / "profiles.json"
        registry.write_text("[]")
        assert client.get(download, headers=HEADERS).status_code == 409
        expired = {
            **profile,
            "valid_from": (datetime.now(UTC) - timedelta(days=3)).isoformat(),
            "valid_until": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
        }
        registry.write_text(json.dumps([expired]))
        assert client.get(download, headers=HEADERS).status_code == 409
        registry.write_text(json.dumps([profile]))
        evidence = tmp_path / profile["profile_id"] / "connection.pdf"
        original = evidence.read_bytes()
        evidence.write_bytes(original + b"changed")
        assert client.get(download, headers=HEADERS).status_code == 409
        evidence.write_bytes(original)
        changed = w.model_dump(mode="json")
        changed["design"]["intent"]["shelf_load_n"] += 1
        assert (
            client.put(
                path + "/draft",
                headers=HEADERS,
                json={"expected_revision": 1, "workspace": changed},
            ).status_code
            == 200
        )
        assert client.get(download, headers=HEADERS).status_code == 409
        assert client.post(path + "/exports", headers=HEADERS, json=request).status_code == 409


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("joint_design_shear_capacity_n", 1, "Fogskjuvning"),
        ("joint_design_withdrawal_capacity_n", 99, "Fogutdrag"),
        ("verified_row_payload_n", 49, "brukslast"),
        ("deflection_uncertainty_um", 101, "Nedböjning"),
        ("tested_horizontal_force_n", 49, "Horisontalkraft"),
        ("verified_tip_factor_permille", 1499, "tippmarginal"),
        ("completed_assembly_steps", [1, 2], "monteringssteg"),
        ("missing", None, "mätresultat"),
    ],
)
def test_failing_type_test_denies_a_normal_order_even_with_five_reports(
    tmp_path, monkeypatch, key, value, reason
):
    w = measured_order()
    profile = _install_test_profile(tmp_path, monkeypatch, w)
    if key == "missing":
        del profile["physical_results"]
    else:
        profile["physical_results"][key] = value
    (tmp_path / "profiles.json").write_text(json.dumps([profile]))
    with TestClient(app) as client:
        path, body = create(client, w)
        failed = client.post(
            path + "/exports", headers=HEADERS, json={**body, "purpose": "workshop_drawings"}
        )
        assert failed.status_code == 409 and reason in failed.json()["detail"], failed.text
        assert client.post(path + "/exports", headers=HEADERS, json=body).status_code == 202


def test_scope_is_shared_between_customer_ids_but_changes_with_material_or_design():
    w = measured_order()
    reference = qualifications.bookcase_scope(w)
    copy = w.model_copy(
        update={
            "design": w.design.model_copy(update={"design_id": "another-customer", "revision": 3})
        }
    )
    assert qualifications.bookcase_scope(copy) == reference
    changed = w.model_copy(
        update={
            "design": w.design.model_copy(
                update={"intent": w.design.intent.model_copy(update={"shelf_count": 2})}
            )
        }
    )
    assert qualifications.bookcase_scope(changed) != reference
