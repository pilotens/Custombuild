from __future__ import annotations

from custombuild_rules import RULES_VERSION
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import (
    DEV_ORG_ATELIER,
    DEV_ORG_NORDIC,
    DEV_USER_ATELIER,
    DEV_USER_NORDIC,
)
from .db import set_tenant_context
from .design_service import preview
from .models import DesignStatus, DesignVersion, Membership, Organization, Project, Role, User
from .repository import canonical_hash

NORDIC_DEMO_PROJECT = "33333333-3333-4333-8333-333333333333"
NORDIC_DEMO_VERSION = "55555555-5555-4555-8555-555555555555"
ATELIER_DEMO_PROJECT = "44444444-4444-4444-8444-444444444444"
ATELIER_DEMO_VERSION = "66666666-6666-4666-8666-666666666666"

DEMO_SPEC = {
    "width_mm": 900,
    "height_mm": 2000,
    "depth_mm": 320,
    "material_id": "mdf",
    "nominal_thickness_mm": 18,
    "measured_thickness_mm": 18,
    "shelf_count": 5,
    "load_per_shelf_kg": 30,
    "back_panel": True,
    "plinth": True,
    "divider_count": 0,
    "joint_system": "dado",
    "reinforcement_mode": "manual",
    "wall_anchor_required": False,
}


def seed_development(session: Session) -> None:
    organizations = (
        (DEV_ORG_NORDIC, "Nordic Woodworks Demo", "nordic-demo"),
        (DEV_ORG_ATELIER, "Atelier Demo", "atelier-demo"),
    )
    users = (
        (
            DEV_USER_NORDIC,
            "demo:nordic-owner",
            "owner@nordic.example",
            "Nordic Demo Owner",
        ),
        (
            DEV_USER_ATELIER,
            "demo:atelier-owner",
            "owner@atelier.example",
            "Atelier Demo Owner",
        ),
    )
    for organization_id, name, slug in organizations:
        if session.get(Organization, organization_id) is None:
            session.add(Organization(id=organization_id, name=name, slug=slug))
    for user_id, subject, email, name in users:
        if session.get(User, user_id) is None:
            session.add(User(id=user_id, oidc_sub=subject, email=email, name=name))
    session.flush()
    for organization_id, user_id, project_id, version_id, project_name in (
        (
            DEV_ORG_NORDIC,
            DEV_USER_NORDIC,
            NORDIC_DEMO_PROJECT,
            NORDIC_DEMO_VERSION,
            "Arkitektväggen",
        ),
        (
            DEV_ORG_ATELIER,
            DEV_USER_ATELIER,
            ATELIER_DEMO_PROJECT,
            ATELIER_DEMO_VERSION,
            "Seedad bokhylla – Atelier",
        ),
    ):
        set_tenant_context(session, organization_id)
        membership = session.scalar(
            select(Membership).where(
                Membership.organization_id == organization_id,
                Membership.user_id == user_id,
            )
        )
        if membership is None:
            session.add(
                Membership(
                    organization_id=organization_id,
                    user_id=user_id,
                    role=Role.owner,
                )
            )
        session.flush()
        project = session.get(Project, project_id)
        if project is None:
            project = Project(
                id=project_id,
                organization_id=organization_id,
                name=project_name,
                description="Körbart acceptansprojekt för den vertikala bokhyllelösningen.",
                furniture_type="bookcase",
                current_revision=1,
            )
            session.add(project)
            session.flush()
        if session.get(DesignVersion, version_id) is None:
            spec, result, presented = preview(
                DEMO_SPEC,
                design_id=project_id,
                revision=1,
            )
            materials = [
                {
                    "material_id": material.material_id,
                    "version": material.version,
                }
                for material in (spec.material, spec.back_material)
                if material is not None
            ]
            session.add(
                DesignVersion(
                    id=version_id,
                    organization_id=organization_id,
                    project_id=project_id,
                    revision=1,
                    status=DesignStatus.draft,
                    design_hash=result.design_hash,
                    context_hash=canonical_hash(
                        {
                            "design_hash": result.design_hash,
                            "engine_version": result.engine_version,
                            "template_version": result.template_version,
                            "rule_version": f"bookcase-rules@{RULES_VERSION}",
                            "materials": sorted(
                                materials,
                                key=lambda item: (item["material_id"], item["version"]),
                            ),
                        }
                    ),
                    spec_json=spec.model_dump(mode="json"),
                    result_json=presented,
                    engine_version=result.engine_version,
                    template_version=f"bookcase@{result.template_version}",
                    rule_version=f"bookcase-rules@{RULES_VERSION}",
                    created_by=user_id,
                )
            )
            session.flush()
