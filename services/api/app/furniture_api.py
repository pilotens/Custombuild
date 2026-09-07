"""Furniture design drafts and profile changes, separate from production release."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any
from uuid import uuid4

from celery import Celery
from custombuild_domain.furniture import (
    FURNITURE_ENGINE_VERSION,
    FurnitureWorkspace,
    ProfileChange,
)
from custombuild_domain.furniture_catalog import furniture_catalog
from custombuild_domain.identity import content_hash
from custombuild_manufacturing.furniture_profiles import (
    compare_furniture_profiles,
    furniture_machine_catalog,
    preview_furniture,
)
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import Capability, Principal, get_principal, require_capability
from .config import get_settings
from .models import AuditEvent, OutboxEvent, Project
from .repository import audit, tenant_project, tenant_session

router = APIRouter(prefix="/v1/furniture", tags=["furniture-design"])
SessionDep = Annotated[Session, Depends(tenant_session, scope="function")]
ReaderDep = Annotated[Principal, Depends(get_principal)]
DesignerDep = Annotated[Principal, Depends(require_capability(Capability.DESIGN))]
GeneratorDep = Annotated[Principal, Depends(require_capability(Capability.GENERATE))]
_SCHEMA = "custombuild.furniture-design.v1"


class FurnitureDraftSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    workspace: FurnitureWorkspace


class FurnitureExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    expected_design_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def _workspace(project: Project) -> FurnitureWorkspace | None:
    if project.draft_spec_json is None:
        return None
    if project.draft_spec_json.get("schema_version") != _SCHEMA:
        raise HTTPException(
            409, detail="Projektet använder den tidigare studion. Skapa ett nytt projekt."
        )
    return FurnitureWorkspace.model_validate(
        {
            "design": project.draft_spec_json,
            "manufacturing": (project.draft_workspace_json or {}).get("manufacturing"),
        }
    )


def _preview(workspace: FurnitureWorkspace) -> dict[str, Any]:
    try:
        return preview_furniture(workspace)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/catalog")
def catalog(principal: ReaderDep, session: SessionDep) -> dict[str, Any]:
    return {**furniture_catalog(), "machines": furniture_machine_catalog()}


@router.post("/preview")
def preview(
    payload: FurnitureWorkspace, principal: ReaderDep, session: SessionDep
) -> dict[str, Any]:
    return _preview(payload)


@router.post("/profile-change")
def profile_change(
    payload: ProfileChange, principal: ReaderDep, session: SessionDep
) -> dict[str, Any]:
    try:
        return compare_furniture_profiles(payload)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/projects/{project_id}/draft")
def read_draft(project_id: str, principal: ReaderDep, session: SessionDep) -> dict[str, Any]:
    project = tenant_project(session, principal, project_id)
    workspace = _workspace(project)
    return {
        "project_id": project.id,
        "revision": project.draft_revision,
        "workspace": workspace.model_dump(mode="json") if workspace else None,
        "preview": _preview(workspace) if workspace else None,
    }


@router.put("/projects/{project_id}/draft")
def save_draft(
    project_id: str,
    payload: FurnitureDraftSave,
    principal: DesignerDep,
    session: SessionDep,
) -> dict[str, Any]:
    project = tenant_project(session, principal, project_id)
    session.refresh(project, with_for_update=True)
    _workspace(project)  # Protect an existing legacy draft from accidental replacement.
    if project.draft_revision != payload.expected_revision:
        raise HTTPException(
            409, detail="Utkastet har ändrats. Hämta aktuell revision före nästa sparning."
        )
    revision = project.draft_revision + 1
    document = payload.workspace.model_dump(mode="json")
    document["design"].update(design_id=project.id, revision=revision)
    workspace = FurnitureWorkspace.model_validate(document)
    preview_result = _preview(workspace)
    project.draft_revision = revision
    project.draft_template_id = workspace.design.intent.family
    project.furniture_type = workspace.design.intent.family
    project.draft_design_hash = preview_result["design"]["design_hash"]
    project.draft_spec_json = workspace.design.model_dump(mode="json")
    project.draft_workspace_json = {"manufacturing": document["manufacturing"]}
    project.draft_result_json = preview_result
    project.draft_updated_by = principal.user_id
    audit(
        session,
        principal,
        "furniture.draft.saved",
        "project",
        project.id,
        {
            "revision": revision,
            "workspace": workspace.model_dump(mode="json"),
            "design_hash": project.draft_design_hash,
            "engine_version": FURNITURE_ENGINE_VERSION,
            "dependencies": preview_result["dependencies"],
        },
    )
    session.flush()
    return {
        "project_id": project.id,
        "revision": revision,
        "workspace": workspace.model_dump(mode="json"),
        "preview": preview_result,
    }


@router.get("/projects/{project_id}/history")
def history(
    project_id: str,
    principal: ReaderDep,
    session: SessionDep,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> dict[str, Any]:
    tenant_project(session, principal, project_id)
    events = list(
        session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == principal.organization_id,
                AuditEvent.entity_type == "project",
                AuditEvent.entity_id == project_id,
                AuditEvent.action == "furniture.draft.saved",
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .offset(offset)
            .limit(26)
        )
    )
    return {
        "items": [{"id": e.id, "created_at": e.occurred_at, **e.payload_json} for e in events[:25]],
        "next_offset": offset + 25 if len(events) > 25 else None,
    }


@router.post("/projects/{project_id}/exports", status_code=202)
def request_export(
    project_id: str,
    payload: FurnitureExportRequest,
    principal: GeneratorDep,
    session: SessionDep,
) -> dict[str, str]:
    project = tenant_project(session, principal, project_id)
    session.refresh(project, with_for_update=True)
    workspace = _workspace(project)
    if (
        workspace is None
        or project.draft_revision != payload.expected_revision
        or (project.draft_design_hash != payload.expected_design_hash)
    ):
        raise HTTPException(409, detail="Spara och granska den aktuella revisionen före export.")
    preview_result = _preview(workspace)
    if len(preview_result["design"]["parts"]) > 128:
        raise HTTPException(422, detail="Granskningspaketet stöder högst 128 delar.")
    if preview_result["design"]["design_hash"] != project.draft_design_hash:
        raise HTTPException(
            409, detail="Modellmotorn har ändrats. Spara en ny revision före export."
        )
    now = datetime.now(UTC)
    # Repeated clicks reuse a recent committed request for the same snapshot.
    recent = session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.organization_id == principal.organization_id,
            OutboxEvent.topic == "furniture.review.requested",
            OutboxEvent.created_at >= now - timedelta(minutes=5),
            OutboxEvent.payload_json["project_id"].as_string() == project.id,
            OutboxEvent.payload_json["design_hash"].as_string() == project.draft_design_hash,
            OutboxEvent.payload_json["revision"].as_integer() == project.draft_revision,
            OutboxEvent.dead_lettered_at.is_(None),
        )
        .order_by(OutboxEvent.created_at.desc())
        .limit(1)
    )
    if recent is not None:
        return {"job_id": str(recent.payload_json["job_id"]), "state": "queued"}
    job_id = str(uuid4())
    session.add(
        OutboxEvent(
            organization_id=principal.organization_id,
            event_key=f"furniture-review:{job_id}",
            topic="furniture.review.requested",
            payload_json={
                "job_id": job_id,
                "organization_id": principal.organization_id,
                "project_id": project.id,
                "revision": project.draft_revision,
                "workspace": workspace.model_dump(mode="json"),
                "design_hash": project.draft_design_hash,
                "engine_version": FURNITURE_ENGINE_VERSION,
            },
        )
    )
    audit(
        session,
        principal,
        "furniture.export.requested",
        "project",
        project.id,
        {
            "job_id": job_id,
            "revision": project.draft_revision,
            "design_hash": project.draft_design_hash,
        },
    )
    session.flush()
    return {"job_id": job_id, "state": "queued"}


@lru_cache(maxsize=1)
def review_result_client() -> Any:
    client = Celery("furniture-review-results", backend=get_settings().redis_url)
    client.conf.update(
        accept_content=["json"],
        result_serializer="json",
        result_accept_content=["json"],
        redis_socket_connect_timeout=5,
        redis_socket_timeout=5,
    )
    return client


@router.get("/projects/{project_id}/exports/{job_id}")
def export_result(
    project_id: str,
    job_id: str,
    principal: ReaderDep,
    session: SessionDep,
) -> dict[str, Any]:
    tenant_project(session, principal, project_id)
    event = session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.organization_id == principal.organization_id,
            OutboxEvent.event_key == f"furniture-review:{job_id}",
            OutboxEvent.topic == "furniture.review.requested",
            OutboxEvent.payload_json["project_id"].as_string() == project_id,
        )
    )
    if event is None:
        raise HTTPException(404, detail="Exporten finns inte i detta projekt.")
    if event.dead_lettered_at is not None:
        return {"state": "failed", "message": "Exporten kunde inte köas."}
    created_at = (
        event.created_at.replace(tzinfo=UTC)
        if event.created_at.tzinfo is None
        else event.created_at
    )
    if datetime.now(UTC) - created_at > timedelta(hours=1):
        return {"state": "expired", "message": "Exporten har löpt ut. Skapa ett nytt paket."}
    try:
        task = review_result_client().AsyncResult(job_id)
        state = task.state
        if state == "FAILURE":
            return {
                "state": "failed",
                "message": "CAD-/dokumentkontrollen misslyckades. Ingen fil godkändes.",
            }
        if state != "SUCCESS":
            return {"state": "running" if event.dispatched_at else "queued"}
        result = task.result
    except Exception as exc:
        raise HTTPException(503, detail="Exporttjänsten kan inte nås just nu.") from exc
    if (
        not isinstance(result, dict)
        or result.get("design_hash") != event.payload_json["design_hash"]
    ):
        raise HTTPException(409, detail="Exportens identitet stämmer inte med beställningen.")
    if result.get("workspace_sha256") != content_hash(event.payload_json["workspace"]) or (
        result.get("revision") != event.payload_json["revision"]
    ):
        raise HTTPException(409, detail="Exportens profiler eller revision stämmer inte.")
    encoded = result.get("content_base64")
    if not isinstance(encoded, str) or len(encoded) > 23 * 1024 * 1024:
        raise HTTPException(409, detail="Exportens innehåll är ogiltigt.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise HTTPException(409, detail="Exportens innehåll är ogiltigt.") from exc
    if hashlib.sha256(data).hexdigest() != result.get("sha256"):
        raise HTTPException(409, detail="Exportens kontrollsumma stämmer inte.")
    return {
        "state": "succeeded",
        "file_name": f"custombuild-design-review-{event.payload_json['revision']}.zip",
        "content_base64": encoded,
        "sha256": result["sha256"],
        "design_hash": result["design_hash"],
        "physical_cutting_authorized": False,
    }
