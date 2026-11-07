"""Read S1 evidence from the committed immutable review object, never from the browser."""

import io
import json
import zipfile

from custombuild_domain.measured_assembly import (
    JointCheck,
    MeasuredReport,
    review_measured_assembly,
)
from custombuild_domain.sideboard_construction import construct_sideboard
from custombuild_domain.sideboard_snapshot import SideboardWorkspace
from custombuild_domain.sideboard_stability_report import ClosedStability
from custombuild_domain.sideboard_supports import LEGACY
from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from .api import _ARTIFACT_STREAM_OPERATION_DEPENDENCY
from .furniture_api import ReaderDep, SessionDep, _furniture_export_artifact
from .models import GenerationJob, GenerationJobType
from .repository import tenant_project
from .storage import (
    ArtifactIntegrityError,
    ArtifactStorageUnavailableError,
    StoredObjectExpectation,
    open_verified_stored_object,
)

router = APIRouter(prefix="/v1/constructions/sideboard")


@router.get(
    "/projects/{project_id}/exports/{job_id}/inspection",
    response_model=MeasuredReport,
    dependencies=[_ARTIFACT_STREAM_OPERATION_DEPENDENCY],
)
def inspect_sideboard(
    project_id: str, job_id: str, principal: ReaderDep, session: SessionDep
) -> MeasuredReport:
    tenant_project(session, principal, project_id)
    job = session.scalar(
        select(GenerationJob).where(
            GenerationJob.id == job_id,
            GenerationJob.project_id == project_id,
            GenerationJob.organization_id == principal.organization_id,
            GenerationJob.job_type == GenerationJobType.furniture_review,
        )
    )
    if job is None:
        raise HTTPException(404, "Exporten finns inte i detta projekt.")
    artifact, _, request = _furniture_export_artifact(session, job)
    try:
        workspace = SideboardWorkspace.model_validate(request.get("workspace"))
        package = construct_sideboard(workspace.request)
        if (
            workspace.project_id != project_id
            or workspace.revision != request["revision"]
            or package.construction_sha256 != request["design_hash"]
        ):
            raise ValueError("Konstruktionens identitet har ändrats.")
        obj = open_verified_stored_object(
            StoredObjectExpectation(
                object_key=artifact.object_key,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                content_type=artifact.content_type,
                required_metadata=(
                    ("furniture-design-hash", request["design_hash"]),
                    ("workspace-sha256", request["workspace_sha256"]),
                ),
            ),
            max_bytes=16 * 1024 * 1024,
        )
        try:
            raw = b"".join(obj.iter_bytes())
        finally:
            obj.close()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = [i for i in archive.infolist() if i.filename == "validation/sideboard.json"]
            if len(entries) != 1 or entries[0].file_size > 512 * 1024:
                raise ValueError("Kontrollprotokollet saknas eller är för stort.")
            cad = json.loads(archive.read(entries[0]))
        if (
            not isinstance(cad, dict)
            or cad.get("assembly_sha256") != package.assembly_sha256
            or cad.get("wood_and_boring_checks_passed") is not True
            or cad.get("physical_cutting_authorized") is not False
            or cad.get("hinge_motion_verified") is not False
            or cad.get("hardware_body_collision_verified") is not False
            or cad.get("load_rating_verified") is not False
            or cad.get("errors") != []
            or cad.get("wood_intersections") != []
            or cad.get("fastener_intersections") != []
            or cad.get("operations") != sum(len(p.operations) for p in package.assembly.parts)
            or cad.get("parts") != len(package.assembly.parts)
            or cad.get("connected_parts") != len(package.assembly.parts)
            or cad.get("pairs_checked")
            != len(package.assembly.parts) * (len(package.assembly.parts) - 1) // 2
        ):
            raise ValueError("CAD-underlaget är ofullständigt.")
        assert package.assembly.backing is not None
        for field, expected in (
            ("joints", {j.id for j in package.assembly.joints}),
            ("fittings", {f.id for f in package.fittings}),
            ("back_coverage", {o.id for o in package.assembly.backing.openings}),
        ):
            checks = cad.get(field, [])
            if (
                not isinstance(checks, list)
                or not all(isinstance(c, dict) for c in checks)
                or len(checks) != len(expected)
                or {c["id"] for c in checks} != expected
                or any(c.get("passed") is not True for c in checks)
                or (field != "back_coverage" and any(c.get("errors") != [] for c in checks))
            ):
                raise ValueError("Alla anslutningar eller bakytor är inte kontrollerade.")
        support_sets = cad.get("shelf_support_sets")
        if package.request.shelf_pin_article or "shelf_support_sets" in cad:
            article = package.request.shelf_pin_article or LEGACY.article
            expected_sets = {
                f"SHELF-{i}": {
                    "shelf": f"SHELF-{i}",
                    "article": article,
                    "count": 4,
                    "passed": True,
                }
                for i in range(1, 6)
            }
            if (
                not isinstance(support_sets, list)
                or len(support_sets) != len(expected_sets)
                or not all(
                    isinstance(row, dict) and row.get("passed") is True for row in support_sets
                )
                or {row["shelf"]: row for row in support_sets} != expected_sets
            ):
                raise ValueError("Fyra kontrollerade upplag saknas på ett eller flera hyllplan.")
        press = cad.get("door_press", [])
        if (
            not isinstance(press, list)
            or not all(isinstance(p, dict) for p in press)
            or len(press) != 5
            or {p["part"] for p in press} != {f"DOOR-{i}" for i in range(1, 6)}
            or any(
                p.get("passed") is not True or p.get("travel_mm") != 2 or p.get("collisions") != []
                for p in press
            )
        ):
            raise ValueError("Tryckrörelsen är inte kontrollerad.")
        stability = None
        if package.request.review is not None:
            stability = ClosedStability.model_validate(cad.get("closed_stability"))
            if (
                stability.horizontal_force_n != package.request.review.horizontal_force_n
                or stability.top_payload_kg != package.request.review.top_payload_kg
                or abs(
                    stability.push_moment_nm
                    - stability.horizontal_force_n * package.request.height_mm / 1000
                )
                > 1e-8
            ):
                raise ValueError("Tippkontrollen har andra provlaster än ritningen.")
        original = review_measured_assembly(package.assembly)
        return original.model_copy(
            update=dict(
                geometry_method="machined_wood",
                closed_stability=stability,
                issues={},
                overlaps=(),
                overlaps_truncated=False,
                unreviewed_profile_parts=(),
                fastener_interferences=(),
                joints=tuple(
                    JointCheck(
                        id=j["id"],
                        state="aligned",
                        offset_um=(0, 0, 0),
                        reason=(
                            "CAD: borrningar, nominell plugg/skruv och trädelar passar. "
                            "Förbandshållfasthet är inte kvalificerad."
                        ),
                    )
                    for j in cad["joints"]
                ),
                fitting_checks=tuple(
                    JointCheck(
                        id=f["id"],
                        state="aligned",
                        reason=(
                            "CAD har kontrollerat bärarens nominella kropp, borrhål och upplag. "
                            "Fyra separata upplag finns på hyllplanet."
                            if support_sets is not None and f["kind"] == "shelf_pin"
                            else "Borrbild och anslutningsläge kontrollerade. Full "
                            "beslagvolym, gångjärnsrörelse och lastklassning återstår."
                        ),
                    )
                    for f in cad["fittings"]
                ),
            )
        )
    except ArtifactStorageUnavailableError as exc:
        raise HTTPException(503, "Kontrollprotokollets lagring är inte tillgänglig.") from exc
    except (ArtifactIntegrityError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise HTTPException(409, "Skänkens CAD-underlag kunde inte verifieras.") from exc
