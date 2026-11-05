"""Read exact inspection evidence only from a committed, hash-verified review ZIP."""

import io
import json
import zipfile
from typing import Any

from custombuild_domain.measured_assembly import (
    JointCheck,
    MeasuredReport,
    review_measured_assembly,
)
from custombuild_domain.trestle_construction import construct_trestle
from custombuild_domain.trestle_snapshot import TrestleWorkspace
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

router = APIRouter(prefix="/v1/constructions/trestle-table")


@router.get(
    "/projects/{project_id}/exports/{job_id}/inspection",
    response_model=MeasuredReport,
    dependencies=[_ARTIFACT_STREAM_OPERATION_DEPENDENCY],
)
def inspect_trestle(
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
        workspace = TrestleWorkspace.model_validate(request.get("workspace"))
        package = construct_trestle(workspace.request)
        if (
            workspace.project_id != project_id
            or workspace.revision != request["revision"]
            or package.construction_sha256 != request["design_hash"]
        ):
            raise ValueError("Konstruktionens identitet har ändrats.")
        verified = open_verified_stored_object(
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
            raw = b"".join(verified.iter_bytes())
        finally:
            verified.close()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:

            def read(name: str) -> dict[str, Any]:
                entries = [i for i in archive.infolist() if i.filename == name]
                if len(entries) != 1 or entries[0].file_size > 512 * 1024:
                    raise ValueError("Kontrollprotokollet saknas eller är för stort.")
                result = json.loads(archive.read(entries[0]))
                if not isinstance(result, dict):
                    raise ValueError("Fel format på kontrollprotokollet.")
                return result

            cad = read("validation/cad.json")
            movement = read("validation/tabletop-movement.json")
        joints, bearings = cad.get("joints", []), cad.get("bearings", [])
        if (
            cad.get("assembly_sha256") != package.assembly_sha256
            or cad.get("geometric_checks_passed") is not True
            or cad.get("physical_cutting_authorized") is not False
            or cad.get("load_rating_verified") is not False
            or cad.get("connected_parts") != len(package.assembly.parts)
            or cad.get("pairs_checked")
            != len(package.assembly.parts) * (len(package.assembly.parts) - 1) // 2
            or cad.get("wood_intersections") != []
            or cad.get("hardware_intersections") != []
            or len(joints) != len(package.assembly.joints)
            or {j["id"] for j in joints} != {j.id for j in package.assembly.joints}
            or any(j.get("passed") is not True or j.get("errors") != [] for j in joints)
            or len(bearings) != len(package.assembly.bearings)
            or {b["id"] for b in bearings} != {b.id for b in package.assembly.bearings}
            or any(b.get("passed") is not True or b.get("area_mm2", 0) <= 0 for b in bearings)
            or movement.get("passed") is not True
        ):
            raise ValueError("Kontrollprotokollet godkänner inte alla anslutningar.")
        original = review_measured_assembly(package.assembly)
        return original.model_copy(
            update=dict(
                geometry_method="exact_cad",
                issues={p.id: () for p in package.assembly.parts},
                overlaps=(),
                overlaps_truncated=False,
                unreviewed_profile_parts=(),
                fastener_interferences=(),
                joints=tuple(
                    JointCheck(
                        id=j["id"],
                        state="aligned",
                        offset_um=(0, 0, 0),
                        reason="Exakt CAD: bearbetade motdelar och beslag passar nominellt. "
                        "Bärförmåga och tillverkningstoleranser ingår inte.",
                    )
                    for j in joints
                ),
                bearings=tuple(
                    JointCheck(
                        id=b["id"],
                        state="aligned",
                        offset_um=(0, 0, 0),
                        contact_area_um2=b["area_mm2"] * 1e6,
                        reason="Exakt CAD: gemensamt horisontellt upplag verifierat.",
                    )
                    for b in bearings
                ),
            )
        )
    except ArtifactStorageUnavailableError as exc:
        raise HTTPException(503, "Kontrollprotokollets lagring är inte tillgänglig.") from exc
    except (ArtifactIntegrityError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise HTTPException(409, "Den exakta granskningen kunde inte verifieras.") from exc
