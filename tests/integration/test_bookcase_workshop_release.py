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
        surveyed_fields=("ceiling_height", "front_space", "left_wall", "right_wall", "rear_wall"),
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
    is_cabinet = bool(workspace.design.intent.base_doors)
    for category in sorted(
        qualifications.REQUIRED_REPORTS | ({"door_service"} if is_cabinet else set())
    ):
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

    def measured_lot(stock):
        return dict(
            material_id=stock.material_id,
            material_version=stock.version,
            batch_id=stock.batch_id,
            usable_formats=[dict(along_grain_um=3000000, across_grain_um=1500000)],
            minimum_thickness_um=stock.measured_thickness_um - 80,
            maximum_thickness_um=stock.measured_thickness_um + 80,
            uncertainty_um=20,
        )

    profile = dict(
        profile_id=slug,
        organization_id=TENANT,
        scope_sha256=qualifications.bookcase_scope(workspace),
        evidence=docs,
        physical_results=dict(
            schema_version="custombuild.bookcase-physical-results.v2",
            carcass_lot=measured_lot(workspace.design.material),
            back_lot=measured_lot(workspace.design.back_material),
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
    if workspace.design.material.material_id == "solid-walnut":
        profile["physical_results"]["wood_movement"] = dict(
            use_min_permille=65,
            use_max_permille=115,
            tested_dry_permille=63,
            tested_wet_permille=117,
            moisture_uncertainty_permille=2,
            minimum_remaining_movement_um=80,
            clearance_uncertainty_um=10,
        )
    if is_cabinet:
        profile["physical_results"]["door_service"] = dict(
            duty_specification="SYNTHETIC-ONLY",
            required_cycles=12500,
            required_screw_capacity_n=140,
            tested_openings=[
                dict(
                    leaf=p.semantic_key,
                    tested_mass_g=p.weight_g,
                    achieved_opening_mdeg=93000,
                    angle_uncertainty_mdeg=0,
                    minimum_clearance_um=1000,
                    clearance_uncertainty_um=50,
                    cycles_completed=12500,
                    retained_screw_capacity_n=140,
                )
                for p in model.parts
                if p.role.value == "cabinet_front"
            ],
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


def test_workshop_width_interval_binds_both_endpoints_site_and_lots(tmp_path, monkeypatch):
    """Only the test registry contains range evidence; the actual order still passes CAD."""

    def order(width):
        data = measured_order().model_dump(mode="json")
        data["design"]["intent"].update(
            width_um=width, base_doors="J95-C24/0T-NI", divider_count=1, height_um=1_200_000
        )
        data["design"]["material"]["material_id"] = "solid-walnut"
        data["design"]["installation"].update(
            width_um=width + 100_000, height_um=1_400_000, measured_ceiling_height_um=1_400_000
        )
        return FurnitureWorkspace.model_validate(data)

    low, mid, high = order(880_000), order(889_123), order(900_000)
    profile = _install_test_profile(tmp_path, monkeypatch, high)
    registry = tmp_path / "profiles.json"
    profile["width_range"] = {
        "minimum_um": low.design.intent.width_um,
        "maximum_um": high.design.intent.width_um,
        "minimum_scope_sha256": qualifications.bookcase_scope(low),
    }
    registry.write_text(json.dumps([profile]))
    with pytest.raises(ValueError, match="giltiga rapporter"):
        qualifications.qualified_bookcase(mid, TENANT)
    interval_report = tmp_path / profile["profile_id"] / "width-range.pdf"
    canvas = Canvas(str(interval_report))
    canvas.drawString(30, 780, "SYNTHETIC WORKFLOW TEST - entire width interval")
    canvas.save()
    profile["evidence"].append(
        dict(
            category="width_range",
            file=interval_report.name,
            sha256=hashlib.sha256(interval_report.read_bytes()).hexdigest(),
        )
    )
    registry.write_text(json.dumps([profile]))
    # The original maximum-width fixture has too much deflection for the minimum span.
    with pytest.raises(ValueError, match="Nedböjning"):
        qualifications.qualified_bookcase(high, TENANT)
    profile["physical_results"]["measured_shelf_deflection_um"] = 900
    registry.write_text(json.dumps([profile]))
    for bound in [low, high]:
        assert qualifications.qualified_bookcase(bound, TENANT).width_range is not None
    for outside in [order(879_999), order(900_001)]:
        with pytest.raises(ValueError, match="utanför registrerat intervall.*880–900 mm"):
            qualifications.qualified_bookcase(outside, TENANT)
    for key, field, value in [
        ("material", "batch_id", "other-lot"),
        ("installation", "qualified_site_ref", "other-site"),
    ]:
        data = mid.model_dump(mode="json")
        data["design"][key][field] = value
        with pytest.raises(ValueError, match="Ingen entydigt"):
            qualifications.qualified_bookcase(FurnitureWorkspace.model_validate(data), TENANT)
    install_review_storage(monkeypatch)
    with TestClient(app) as client:
        path, bound = create(client, mid)
        eligibility = path + "/workshop-eligibility"
        assert client.get(eligibility, headers=OTHER).status_code == 404
        check = client.get(eligibility, headers=HEADERS).json()
        assert check["available"] and check["design_hash"] == bound["expected_design_hash"]
        assert "880" in check["detail"] and "900" in check["detail"]
        res = client.post(
            path + "/exports", headers=HEADERS, json={**bound, "purpose": "workshop_drawings"}
        )
        assert res.status_code == 202, res.text
        job = res.json()["job_id"]
        assert tasks.generate_furniture_review.run(job, TENANT)["state"] == "succeeded"
        download = path + f"/exports/{job}/download"
        with ZipFile(io.BytesIO(client.get(download, headers=HEADERS).content)) as z:
            assert z.read("documents/breddintervall.pdf") == interval_report.read_bytes()
            assert (
                json.loads(z.read("design/resolved.json"))["spec"]["intent"]["width_um"] == 889_123
            )
        interval_report.unlink()
        assert not client.get(eligibility, headers=HEADERS).json()["available"]
        assert client.get(download, headers=HEADERS).status_code == 409


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


def test_legacy_sample_measurements_cannot_release_a_workshop_order(tmp_path, monkeypatch):
    reference = measured_order()
    _install_test_profile(tmp_path, monkeypatch, reference)
    payload = reference.model_dump(mode="json")
    payload["design"]["installation"].pop("surveyed_fields")
    unverified = FurnitureWorkspace.model_validate(payload)
    with TestClient(app) as client:
        path, bound = create(client, unverified)
        response = client.post(
            path + "/exports", headers=HEADERS, json={**bound, "purpose": "workshop_drawings"}
        )
        assert response.status_code == 409
        assert "takhöjd" in response.json()["detail"].lower()
        assert client.post(path + "/exports", headers=HEADERS, json=bound).status_code == 202


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


@pytest.mark.parametrize(
    ("role", "field", "value", "reason"),
    [
        ("carcass_lot", "batch_id", "different-stock", "Stomme"),
        ("back_lot", "material_id", "other-plywood", "Rygg"),
        ("back_lot", "material_version", "other-version", "Rygg"),
        (
            "carcass_lot",
            "usable_formats",
            [
                {"along_grain_um": 3000000, "across_grain_um": 100},
                {"along_grain_um": 100, "across_grain_um": 3000000},
            ],
            "råformat",
        ),
        (
            "back_lot",
            "usable_formats",
            [{"along_grain_um": 100, "across_grain_um": 3000000}],
            "råformat",
        ),
        ("carcass_lot", "uncertainty_um", 21, "mätosäkerhet"),
        ("back_lot", "minimum_thickness_um", 6200, "omvänt"),
        ("back_lot", "batch_id", None, "mätresultat"),
    ],
)
def test_material_lots_gate_workshop_export(tmp_path, monkeypatch, role, field, value, reason):
    workspace = measured_order()
    profile = _install_test_profile(tmp_path, monkeypatch, workspace)
    with TestClient(app) as client:
        path, bound = create(client, workspace)
        profile["physical_results"][role][field] = value
        (tmp_path / "profiles.json").write_text(json.dumps([profile]))
        refused = client.post(
            path + "/exports", headers=HEADERS, json={**bound, "purpose": "workshop_drawings"}
        )
        assert refused.status_code == 409 and reason in refused.json()["detail"]


@pytest.mark.cad
def test_combined_cabinet_needs_door_trials_for_every_opening_and_rechecks_download(
    tmp_path, monkeypatch
):
    source = measured_order().model_dump(mode="json")
    d = source["design"]
    d["intent"].update(base_doors="J95-C24/0T-NI", divider_count=1, height_um=1200000)
    d["installation"].update(height_um=1400000, measured_ceiling_height_um=1400000)
    d["material"]["material_id"] = "solid-walnut"
    w = FurnitureWorkspace.model_validate(source)
    storage = install_review_storage(monkeypatch)
    profile = _install_test_profile(tmp_path, monkeypatch, w)
    with TestClient(app) as client:
        path, bound = create(client, w)
        req = {**bound, "purpose": "workshop_drawings"}
        valid = json.dumps([profile])
        results = profile["physical_results"]["door_service"]["tested_openings"]
        results[1]["cycles_completed"] -= 1
        (tmp_path / "profiles.json").write_text(json.dumps([profile]))
        bad = client.post(path + "/exports", headers=HEADERS, json=req)
        assert bad.status_code == 409 and "luckprovet" in bad.json()["detail"], bad.text
        (tmp_path / "profiles.json").write_text(valid)
        ok = client.post(path + "/exports", headers=HEADERS, json=req)
        assert ok.status_code == 202, ok.text
        job = ok.json()["job_id"]
        assert tasks.generate_furniture_review.run(job, TENANT)["state"] == "succeeded"
        download = f"{path}/exports/{job}/download"
        response = client.get(download, headers=HEADERS)
        assert response.status_code == 200 and len(storage.put_calls) == 1
        with ZipFile(io.BytesIO(response.content)) as z:
            assert "inspection/gangjarnsprov.txt" not in z.namelist()
            assert "inspection/luckprov-per-fack.csv" not in z.namelist()
            proof = json.loads(z.read("inspection/door-hardware.json"))
            assert not proof["carcass_joints_without_retention"]
            assert proof["trial_retention_pilots"] == 2 * proof["trial_retention_angles"]
            assert (
                len(z.read("inspection/fogprov.csv").splitlines())
                == 1 + proof["trial_retention_pilots"]
            )
            assert (
                z.read("documents/luckprov.pdf")
                == (tmp_path / profile["profile_id"] / "door_service.pdf").read_bytes()
            )
        for key, value in [
            ("leaf", results[0]["leaf"]),
            ("angle_uncertainty_mdeg", 1),
            ("clearance_uncertainty_um", 1000),
            ("tested_mass_g", 1),
            ("retained_screw_capacity_n", 139),
        ]:
            damaged = json.loads(valid)
            damaged[0]["physical_results"]["door_service"]["tested_openings"][1][key] = value
            (tmp_path / "profiles.json").write_text(json.dumps(damaged))
            with pytest.raises(ValueError, match="uckprov"):
                qualifications.qualified_bookcase(w, TENANT)
            assert client.get(download, headers=HEADERS).status_code == 409
        for key, value in [
            ("tested_dry_permille", 64),
            ("tested_wet_permille", 116),
            ("clearance_uncertainty_um", 80),
        ]:
            bad = json.loads(valid)
            bad[0]["physical_results"]["wood_movement"][key] = value
            (tmp_path / "profiles.json").write_text(json.dumps(bad))
            with pytest.raises(ValueError, match="Fuktprovet"):
                qualifications.qualified_bookcase(w, TENANT)
        repaired = json.loads(valid)
        repaired[0]["evidence"] = [
            e for e in repaired[0]["evidence"] if e["category"] != "door_service"
        ]
        (tmp_path / "profiles.json").write_text(json.dumps(repaired))
        assert client.get(download, headers=HEADERS).status_code == 409
