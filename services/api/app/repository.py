from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import Principal, get_principal
from .db import session_scope, set_tenant_context
from .models import AuditEvent, DesignVersion, Membership, Project


def tenant_session(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Iterator[Session]:
    for session in session_scope():
        set_tenant_context(session, principal.organization_id)
        membership = session.scalar(
            select(Membership).where(
                Membership.organization_id == principal.organization_id,
                Membership.user_id == principal.user_id,
            )
        )
        if membership is None or membership.role != principal.role:
            raise HTTPException(status_code=403, detail="Active organization membership required")
        yield session


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def tenant_project(session: Session, principal: Principal, project_id: str) -> Project:
    project = session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.organization_id == principal.organization_id,
        )
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def tenant_version(
    session: Session, principal: Principal, project_id: str, revision: int
) -> DesignVersion:
    project = tenant_project(session, principal, project_id)
    version = session.scalar(
        select(DesignVersion).where(
            DesignVersion.project_id == project.id,
            DesignVersion.organization_id == principal.organization_id,
            DesignVersion.revision == revision,
        )
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Design version not found")
    return version


def audit(
    session: Session,
    principal: Principal,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload_json=payload or {},
        )
    )
