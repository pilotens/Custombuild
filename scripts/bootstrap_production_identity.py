"""Provision and safely upgrade explicit production OIDC identities.

This one-shot operator CLI creates the first administrator, then distinct
designer/reviewer identities, or issuer-binds a legacy raw subject. It uses the
short-lived migrator database role and never exposes an HTTP bootstrap route.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from alembic.config import Config
from alembic.script import ScriptDirectory
from app.config_guards import validate_production_database_url
from app.db import set_tenant_context
from app.models import AuditEvent, Membership, Organization, Role, User
from app.oidc_identity import oidc_identity_key, oidc_issuer_sha256
from sqlalchemy import create_engine, or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.engine.base import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

BOOTSTRAP_AUDIT_ACTION = "identity.bootstrap.production_admin"
MEMBER_AUDIT_ACTION = "identity.bootstrap.production_member"
LEGACY_BINDING_AUDIT_ACTION = "identity.bootstrap.legacy_oidc_issuer_binding"
BOOTSTRAP_AUDIT_SCHEMA_VERSION = "custombuild.identity-bootstrap.v1"
BOOTSTRAP_REQUEST_SCHEMA_VERSION = "custombuild.production-identity-bootstrap.v1"
BOOTSTRAP_LOCK_ID = 4_340_449_326_452_121_807
BOOTSTRAP_NAMESPACE = uuid.UUID("b7bb6f49-4fbb-4c45-8ce9-bb2f0afbc136")
CANONICAL_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")
SIMPLE_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MAX_REQUEST_FILE_BYTES = 16_384
REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "organization_id",
        "organization_slug",
        "organization_name",
        "user_id",
        "oidc_issuer",
        "oidc_subject",
        "email",
        "user_name",
        "role",
        "operator_reference",
    }
)
INITIAL_ADMIN_ROLES = frozenset({Role.owner, Role.admin})
ADDITIONAL_MEMBER_ROLES = frozenset({Role.designer, Role.reviewer})


class IdentityBootstrapError(RuntimeError):
    """The requested identity cannot be provisioned without ambiguity."""


@dataclass(frozen=True, slots=True)
class BootstrapRequest:
    organization_id: str
    organization_slug: str
    organization_name: str
    user_id: str
    oidc_issuer: str
    oidc_subject: str
    email: str
    user_name: str
    role: Role
    operator_reference: str


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    status: str
    organization_id: str
    user_id: str
    role: Role
    audit_event_id: str


def _canonical_uuid(value: str, *, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise IdentityBootstrapError(f"{label} must be a canonical UUID") from exc
    canonical = str(parsed)
    if canonical != value:
        raise IdentityBootstrapError(f"{label} must be a canonical UUID")
    return canonical


def _bounded_text(value: str, *, label: str, maximum: int) -> str:
    if not value or value != value.strip() or len(value) > maximum:
        raise IdentityBootstrapError(
            f"{label} must be non-empty, trimmed and at most {maximum} characters"
        )
    if CONTROL_CHARACTERS.search(value) is not None:
        raise IdentityBootstrapError(f"{label} must not contain control characters")
    if value.startswith("__REPLACE_") and value.endswith("__"):
        raise IdentityBootstrapError(f"{label} is still an operator placeholder")
    return value


def canonical_oidc_issuer(value: str) -> str:
    value = _bounded_text(value, label="OIDC issuer", maximum=2048)
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as exc:
        raise IdentityBootstrapError("OIDC issuer must be a canonical HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or "//" in parsed.path
    ):
        raise IdentityBootstrapError("OIDC issuer must be a canonical HTTPS URL")
    return value


def validate_request(
    request: BootstrapRequest,
    *,
    allowed_roles: frozenset[Role] = INITIAL_ADMIN_ROLES,
) -> BootstrapRequest:
    organization_id = _canonical_uuid(request.organization_id, label="organization ID")
    user_id = _canonical_uuid(request.user_id, label="user ID")
    if CANONICAL_SLUG.fullmatch(request.organization_slug) is None:
        raise IdentityBootstrapError(
            "organization slug must contain only lowercase letters, digits and interior hyphens"
        )
    organization_name = _bounded_text(
        request.organization_name,
        label="organization name",
        maximum=160,
    )
    user_name = _bounded_text(request.user_name, label="user name", maximum=160)
    subject = _bounded_text(request.oidc_subject, label="OIDC subject", maximum=255)
    email = _bounded_text(request.email, label="email", maximum=320)
    if email != email.lower() or SIMPLE_EMAIL.fullmatch(email) is None:
        raise IdentityBootstrapError("email must be a canonical lowercase address")
    if request.role not in allowed_roles:
        if allowed_roles == INITIAL_ADMIN_ROLES:
            message = "initial production role must be owner or admin"
        elif allowed_roles == ADDITIONAL_MEMBER_ROLES:
            message = "additional production role must be designer or reviewer"
        else:
            message = "requested role is not allowed for this identity operation"
        raise IdentityBootstrapError(message)
    operator_reference = _bounded_text(
        request.operator_reference,
        label="operator reference",
        maximum=160,
    )
    return BootstrapRequest(
        organization_id=organization_id,
        organization_slug=request.organization_slug,
        organization_name=organization_name,
        user_id=user_id,
        oidc_issuer=canonical_oidc_issuer(request.oidc_issuer),
        oidc_subject=subject,
        email=email,
        user_name=user_name,
        role=request.role,
        operator_reference=operator_reference,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audit_identity(
    request: BootstrapRequest,
    *,
    action: str = BOOTSTRAP_AUDIT_ACTION,
) -> tuple[str, dict[str, str]]:
    audit_event_id = str(
        uuid.uuid5(
            BOOTSTRAP_NAMESPACE,
            "\x1f".join(
                (
                    request.organization_id,
                    request.user_id,
                    request.oidc_issuer,
                    request.oidc_subject,
                    request.role.value,
                    action,
                )
            ),
        )
    )
    payload = {
        "schema_version": BOOTSTRAP_AUDIT_SCHEMA_VERSION,
        "organization_slug": request.organization_slug,
        "organization_name_sha256": _digest(request.organization_name),
        "role": request.role.value,
        "oidc_issuer_sha256": _digest(request.oidc_issuer),
        "oidc_subject_sha256": _digest(request.oidc_subject),
        "email_sha256": _digest(request.email),
        "user_name_sha256": _digest(request.user_name),
        "performed_by": "external_operator_via_production_bootstrap_cli",
        "operator_reference_sha256": _digest(request.operator_reference),
        "actor_id_semantics": "provisioned_target_identity_not_external_operator",
        "operation": action,
    }
    return audit_event_id, payload


def _one_or_conflict(values: Sequence[object], *, label: str) -> object | None:
    if len(values) > 1:
        raise IdentityBootstrapError(f"{label} identifiers resolve to different records")
    return values[0] if values else None


def _all_memberships_for_user(
    session: Session,
    *,
    user_id: str,
    restore_organization_id: str,
) -> tuple[tuple[str, Role], ...]:
    """Enumerate a global user's memberships without bypassing tenant RLS."""

    organization_ids = tuple(
        session.scalars(select(Organization.id).order_by(Organization.id)).all()
    )
    memberships: list[tuple[str, Role]] = []
    for organization_id in organization_ids:
        set_tenant_context(session, organization_id)
        membership = session.scalar(
            select(Membership).where(
                Membership.organization_id == organization_id,
                Membership.user_id == user_id,
            )
        )
        if membership is not None:
            memberships.append((organization_id, membership.role))
    set_tenant_context(session, restore_organization_id)
    return tuple(memberships)


def _assert_database_issuer_binding(
    session: Session,
    *,
    expected_issuer_sha256: str,
) -> None:
    conflicting_identity_id = session.scalar(
        select(User.id)
        .where(
            User.oidc_issuer_sha256.is_not(None),
            User.oidc_issuer_sha256 != expected_issuer_sha256,
        )
        .limit(1)
    )
    if conflicting_identity_id is not None:
        raise IdentityBootstrapError(
            "database contains an identity bound to another OIDC issuer"
        )


def bootstrap_identity(session: Session, raw_request: BootstrapRequest) -> BootstrapResult:
    """Create or verify one exact organization/user/membership/audit tuple.

    The caller owns the surrounding transaction.  Any partial or drifting state
    is rejected rather than repaired, renamed, reassigned or promoted.
    """

    request = validate_request(raw_request)
    set_tenant_context(session, request.organization_id)

    organizations = session.scalars(
        select(Organization).where(
            or_(
                Organization.id == request.organization_id,
                Organization.slug == request.organization_slug,
            )
        )
    ).all()
    organization = _one_or_conflict(organizations, label="organization")
    if organization is not None and (
        not isinstance(organization, Organization)
        or organization.id != request.organization_id
        or organization.slug != request.organization_slug
        or organization.name != request.organization_name
    ):
        raise IdentityBootstrapError("organization identity does not match existing state")

    expected_identity_key = oidc_identity_key(request.oidc_issuer, request.oidc_subject)
    expected_issuer_sha256 = oidc_issuer_sha256(request.oidc_issuer)
    _assert_database_issuer_binding(
        session,
        expected_issuer_sha256=expected_issuer_sha256,
    )
    users = session.scalars(
        select(User).where(
            or_(
                User.id == request.user_id,
                User.oidc_sub == expected_identity_key,
                User.email == request.email,
            )
        )
    ).all()
    user = _one_or_conflict(users, label="user")
    if user is not None and (
        not isinstance(user, User)
        or user.id != request.user_id
        or user.oidc_sub != expected_identity_key
        or user.oidc_issuer_sha256 != expected_issuer_sha256
        or user.email != request.email
        or user.name != request.user_name
    ):
        raise IdentityBootstrapError("user identity does not match existing state")

    membership = session.scalar(
        select(Membership).where(
            Membership.organization_id == request.organization_id,
            Membership.user_id == request.user_id,
        )
    )
    audit_event_id, audit_payload = _audit_identity(request)
    audit_event = session.get(AuditEvent, audit_event_id)

    state_present = (
        organization is not None,
        user is not None,
        membership is not None,
        audit_event is not None,
    )
    if not any(state_present):
        session.add_all(
            [
                Organization(
                    id=request.organization_id,
                    name=request.organization_name,
                    slug=request.organization_slug,
                ),
                User(
                    id=request.user_id,
                    oidc_sub=expected_identity_key,
                    oidc_issuer_sha256=expected_issuer_sha256,
                    email=request.email,
                    name=request.user_name,
                ),
                Membership(
                    organization_id=request.organization_id,
                    user_id=request.user_id,
                    role=request.role,
                ),
                AuditEvent(
                    id=audit_event_id,
                    organization_id=request.organization_id,
                    actor_id=request.user_id,
                    action=BOOTSTRAP_AUDIT_ACTION,
                    entity_type="user",
                    entity_id=request.user_id,
                    payload_json=audit_payload,
                ),
            ]
        )
        session.flush()
        return BootstrapResult(
            status="created",
            organization_id=request.organization_id,
            user_id=request.user_id,
            role=request.role,
            audit_event_id=audit_event_id,
        )
    if not all(state_present):
        raise IdentityBootstrapError("partial bootstrap identity state requires investigation")
    if not isinstance(membership, Membership) or membership.role != request.role:
        raise IdentityBootstrapError("membership role does not match existing state")
    if not isinstance(audit_event, AuditEvent) or (
        audit_event.organization_id != request.organization_id
        or audit_event.actor_id != request.user_id
        or audit_event.action != BOOTSTRAP_AUDIT_ACTION
        or audit_event.entity_type != "user"
        or audit_event.entity_id != request.user_id
        or audit_event.payload_json != audit_payload
    ):
        raise IdentityBootstrapError("bootstrap audit identity does not match existing state")
    return BootstrapResult(
        status="unchanged",
        organization_id=request.organization_id,
        user_id=request.user_id,
        role=request.role,
        audit_event_id=audit_event_id,
    )


def provision_additional_member(
    session: Session,
    raw_request: BootstrapRequest,
) -> BootstrapResult:
    """Provision one distinct designer or reviewer after the initial administrator."""

    request = validate_request(raw_request, allowed_roles=ADDITIONAL_MEMBER_ROLES)
    set_tenant_context(session, request.organization_id)
    organizations = session.scalars(
        select(Organization).where(
            or_(
                Organization.id == request.organization_id,
                Organization.slug == request.organization_slug,
            )
        )
    ).all()
    organization = _one_or_conflict(organizations, label="organization")
    if not isinstance(organization, Organization) or (
        organization.id != request.organization_id
        or organization.slug != request.organization_slug
        or organization.name != request.organization_name
    ):
        raise IdentityBootstrapError("exact bootstrapped organization is required")
    expected_issuer_sha256 = oidc_issuer_sha256(request.oidc_issuer)
    _assert_database_issuer_binding(
        session,
        expected_issuer_sha256=expected_issuer_sha256,
    )
    administrator_id = session.scalar(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.organization_id == request.organization_id,
            Membership.role.in_(INITIAL_ADMIN_ROLES),
            User.oidc_issuer_sha256 == expected_issuer_sha256,
        )
        .limit(1)
    )
    if administrator_id is None:
        raise IdentityBootstrapError(
            "organization has no owner or admin bound to the requested OIDC issuer"
        )

    expected_identity_key = oidc_identity_key(request.oidc_issuer, request.oidc_subject)
    users = session.scalars(
        select(User).where(
            or_(
                User.id == request.user_id,
                User.oidc_sub == expected_identity_key,
                User.email == request.email,
            )
        )
    ).all()
    user = _one_or_conflict(users, label="user")
    if user is not None and (
        not isinstance(user, User)
        or user.id != request.user_id
        or user.oidc_sub != expected_identity_key
        or user.oidc_issuer_sha256 != expected_issuer_sha256
        or user.email != request.email
        or user.name != request.user_name
    ):
        raise IdentityBootstrapError("user identity does not match existing state")
    membership = session.scalar(
        select(Membership).where(
            Membership.organization_id == request.organization_id,
            Membership.user_id == request.user_id,
        )
    )
    audit_event_id, audit_payload = _audit_identity(request, action=MEMBER_AUDIT_ACTION)
    audit_event = session.get(AuditEvent, audit_event_id)
    state_present = (user is not None, membership is not None, audit_event is not None)
    if not any(state_present):
        session.add_all(
            [
                User(
                    id=request.user_id,
                    oidc_sub=expected_identity_key,
                    oidc_issuer_sha256=expected_issuer_sha256,
                    email=request.email,
                    name=request.user_name,
                ),
                Membership(
                    organization_id=request.organization_id,
                    user_id=request.user_id,
                    role=request.role,
                ),
                AuditEvent(
                    id=audit_event_id,
                    organization_id=request.organization_id,
                    actor_id=request.user_id,
                    action=MEMBER_AUDIT_ACTION,
                    entity_type="user",
                    entity_id=request.user_id,
                    payload_json=audit_payload,
                ),
            ]
        )
        session.flush()
        return BootstrapResult(
            status="created",
            organization_id=request.organization_id,
            user_id=request.user_id,
            role=request.role,
            audit_event_id=audit_event_id,
        )
    if not all(state_present):
        raise IdentityBootstrapError("partial member identity state requires investigation")
    if not isinstance(membership, Membership) or membership.role != request.role:
        raise IdentityBootstrapError("membership role does not match existing state")
    if not isinstance(audit_event, AuditEvent) or (
        audit_event.organization_id != request.organization_id
        or audit_event.actor_id != request.user_id
        or audit_event.action != MEMBER_AUDIT_ACTION
        or audit_event.entity_type != "user"
        or audit_event.entity_id != request.user_id
        or audit_event.payload_json != audit_payload
    ):
        raise IdentityBootstrapError("member audit identity does not match existing state")
    return BootstrapResult(
        status="unchanged",
        organization_id=request.organization_id,
        user_id=request.user_id,
        role=request.role,
        audit_event_id=audit_event_id,
    )


def bind_legacy_oidc_identity(
    session: Session,
    raw_request: BootstrapRequest,
) -> BootstrapResult:
    """Explicitly bind one exact legacy raw subject to its configured issuer."""

    request = validate_request(raw_request, allowed_roles=frozenset(Role))
    set_tenant_context(session, request.organization_id)
    organizations = session.scalars(
        select(Organization).where(
            or_(
                Organization.id == request.organization_id,
                Organization.slug == request.organization_slug,
            )
        )
    ).all()
    organization = _one_or_conflict(organizations, label="organization")
    if not isinstance(organization, Organization) or (
        organization.id != request.organization_id
        or organization.slug != request.organization_slug
        or organization.name != request.organization_name
    ):
        raise IdentityBootstrapError("exact legacy organization is required")

    expected_identity_key = oidc_identity_key(request.oidc_issuer, request.oidc_subject)
    expected_issuer_sha256 = oidc_issuer_sha256(request.oidc_issuer)
    _assert_database_issuer_binding(
        session,
        expected_issuer_sha256=expected_issuer_sha256,
    )
    users = session.scalars(
        select(User).where(
            or_(
                User.id == request.user_id,
                User.oidc_sub == request.oidc_subject,
                User.email == request.email,
            )
        )
    ).all()
    user = _one_or_conflict(users, label="user")
    if not isinstance(user, User) or (
        user.id != request.user_id
        or user.email != request.email
        or user.name != request.user_name
    ):
        raise IdentityBootstrapError("exact legacy user identity is required")
    legacy_raw_state = (
        user.oidc_sub == request.oidc_subject and user.oidc_issuer_sha256 is None
    )
    legacy_opaque_state = (
        user.oidc_sub == expected_identity_key and user.oidc_issuer_sha256 is None
    )
    bound_state = (
        user.oidc_sub == expected_identity_key
        and user.oidc_issuer_sha256 == expected_issuer_sha256
    )
    if not legacy_raw_state and not legacy_opaque_state and not bound_state:
        raise IdentityBootstrapError("legacy OIDC binding state is conflicting")
    memberships = _all_memberships_for_user(
        session,
        user_id=request.user_id,
        restore_organization_id=request.organization_id,
    )
    if memberships != ((request.organization_id, request.role),):
        raise IdentityBootstrapError(
            "legacy issuer binding requires exactly one matching tenant membership"
        )

    audit_event_id, audit_payload = _audit_identity(
        request,
        action=LEGACY_BINDING_AUDIT_ACTION,
    )
    audit_event = session.get(AuditEvent, audit_event_id)
    if (legacy_raw_state or legacy_opaque_state) and audit_event is None:
        user.oidc_sub = expected_identity_key
        user.oidc_issuer_sha256 = expected_issuer_sha256
        session.add(
            AuditEvent(
                id=audit_event_id,
                organization_id=request.organization_id,
                actor_id=request.user_id,
                action=LEGACY_BINDING_AUDIT_ACTION,
                entity_type="user",
                entity_id=request.user_id,
                payload_json=audit_payload,
            )
        )
        session.flush()
        return BootstrapResult(
            status="bound",
            organization_id=request.organization_id,
            user_id=request.user_id,
            role=request.role,
            audit_event_id=audit_event_id,
        )
    if not bound_state or not isinstance(audit_event, AuditEvent):
        raise IdentityBootstrapError("partial legacy issuer binding requires investigation")
    if (
        audit_event.organization_id != request.organization_id
        or audit_event.actor_id != request.user_id
        or audit_event.action != LEGACY_BINDING_AUDIT_ACTION
        or audit_event.entity_type != "user"
        or audit_event.entity_id != request.user_id
        or audit_event.payload_json != audit_payload
    ):
        raise IdentityBootstrapError("legacy binding audit does not match existing state")
    return BootstrapResult(
        status="unchanged",
        organization_id=request.organization_id,
        user_id=request.user_id,
        role=request.role,
        audit_event_id=audit_event_id,
    )


def expected_schema_head() -> str:
    config_path = REPOSITORY_ROOT / "services" / "api" / "alembic.ini"
    config = Config(str(config_path))
    config.set_main_option(
        "script_location",
        str(REPOSITORY_ROOT / "services" / "api" / "alembic"),
    )
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise IdentityBootstrapError("repository must have exactly one Alembic head")
    return heads[0]


def _guard_database_connection(session: Session) -> None:
    role = session.execute(
        text(
            "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolinherit, "
            "rolreplication, rolbypassrls, "
            "pg_catalog.current_schemas(false) AS active_schemas "
            "FROM pg_catalog.pg_roles WHERE rolname = current_user"
        )
    ).mappings().one_or_none()
    if role is None or role["rolname"] != "custombuild_migrator":
        raise IdentityBootstrapError("database connection is not the production migrator role")
    if any(
        role[field] is not False
        for field in (
            "rolsuper",
            "rolcreaterole",
            "rolcreatedb",
            "rolinherit",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise IdentityBootstrapError("production migrator database role is over-privileged")
    active_schemas = role["active_schemas"]
    if not isinstance(active_schemas, list | tuple) or tuple(active_schemas) != (
        "pg_catalog",
        "public",
    ):
        raise IdentityBootstrapError("database search path is not pinned to pg_catalog,public")
    required_relations = {
        name: (owner, rls_enabled, rls_forced)
        for name, owner, rls_enabled, rls_forced in session.execute(
            text(
                "SELECT relation.relname, pg_catalog.pg_get_userbyid(relation.relowner), "
                "relation.relrowsecurity, relation.relforcerowsecurity "
                "FROM pg_catalog.pg_class relation "
                "JOIN pg_catalog.pg_namespace namespace "
                "ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname = 'public' "
                "AND relation.relkind IN ('r', 'p') "
                "AND relation.relname IN "
                "('alembic_version', 'organizations', 'users', 'memberships', 'audit_events')"
            )
        ).tuples()
    }
    if required_relations != {
        "alembic_version": ("custombuild_migrator", False, False),
        "organizations": ("custombuild_migrator", False, False),
        "users": ("custombuild_migrator", False, False),
        "memberships": ("custombuild_migrator", True, True),
        "audit_events": ("custombuild_migrator", True, True),
    }:
        raise IdentityBootstrapError(
            "required public identity tables are missing, misowned or lack forced RLS"
        )
    policies = tuple(
        session.execute(
            text(
                "SELECT relation.relname, policy.polname, policy.polpermissive, policy.polcmd, "
                "policy.polroles = ARRAY[0::oid] AS applies_to_public, "
                "pg_catalog.pg_get_expr(policy.polqual, policy.polrelid), "
                "pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid) "
                "FROM pg_catalog.pg_policy policy "
                "JOIN pg_catalog.pg_class relation ON relation.oid = policy.polrelid "
                "JOIN pg_catalog.pg_namespace namespace "
                "ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname = 'public' "
                "AND relation.relname IN ('memberships', 'audit_events') "
                "ORDER BY relation.relname, policy.polname"
            )
        ).tuples()
    )
    if len(policies) != 2:
        raise IdentityBootstrapError("identity tables do not have the exact tenant RLS policies")
    expected_expression = (
        "((organization_id)::text=current_setting"
        "('app.current_organization_id'::text,true))"
    )
    for (
        table_name,
        policy_name,
        permissive,
        command,
        applies_to_public,
        using,
        with_check,
    ) in policies:
        if (
            table_name not in {"memberships", "audit_events"}
            or policy_name != "tenant_isolation"
            or permissive is not True
            or command != "*"
            or applies_to_public is not True
            or not isinstance(using, str)
            or not isinstance(with_check, str)
            or re.sub(r"\s+", "", using) != expected_expression
            or re.sub(r"\s+", "", with_check) != expected_expression
        ):
            raise IdentityBootstrapError(
                "identity tables do not have the exact tenant RLS policies"
            )
    membership_count = session.execute(
        text(
            "SELECT count(*) FROM pg_catalog.pg_auth_members membership "
            "JOIN pg_catalog.pg_roles member_role ON member_role.oid = membership.member "
            "JOIN pg_catalog.pg_roles granted_role ON granted_role.oid = membership.roleid "
            "WHERE member_role.rolname = current_user OR granted_role.rolname = current_user"
        )
    ).scalar_one()
    if membership_count != 0:
        raise IdentityBootstrapError("production migrator database role has role memberships")
    session.execute(
        text("SELECT pg_catalog.pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": BOOTSTRAP_LOCK_ID},
    )
    current_revisions = tuple(
        session.execute(
            text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
        ).scalars()
    )
    if current_revisions != (expected_schema_head(),):
        raise IdentityBootstrapError("database schema is not at the repository Alembic head")


def _production_database_url(environment: Mapping[str, str]) -> str:
    if environment.get("APP_ENV") != "production":
        raise IdentityBootstrapError("APP_ENV must be explicitly set to production")
    if environment.get("AUTH_MODE") != "oidc":
        raise IdentityBootstrapError("AUTH_MODE must be explicitly set to oidc")
    database_url = environment.get("DATABASE_URL", "")
    try:
        validate_production_database_url(
            database_url,
            expected_username="custombuild_migrator",
            setting_name="DATABASE_URL",
        )
    except ValueError as exc:
        raise IdentityBootstrapError(str(exc)) from exc
    # Avoid allowing a syntactically valid non-psycopg URL to select an
    # unexpected driver in the privileged one-shot container.
    if make_url(database_url).drivername != "postgresql+psycopg":
        raise IdentityBootstrapError("DATABASE_URL must use postgresql+psycopg")
    return database_url


def _request_from_payload(
    payload: object,
    environment: Mapping[str, str],
) -> BootstrapRequest:
    if not isinstance(payload, dict) or set(payload) != REQUEST_KEYS:
        raise IdentityBootstrapError("request file does not have the exact required fields")
    if payload.get("schema_version") != BOOTSTRAP_REQUEST_SCHEMA_VERSION:
        raise IdentityBootstrapError("request file schema version is unsupported")
    if any(not isinstance(payload.get(key), str) for key in REQUEST_KEYS):
        raise IdentityBootstrapError("every request file field must be a string")
    configured_issuer = environment.get("OIDC_ISSUER", "")
    if not configured_issuer:
        raise IdentityBootstrapError("OIDC_ISSUER must be explicitly configured")
    requested_issuer = canonical_oidc_issuer(payload["oidc_issuer"])
    if canonical_oidc_issuer(configured_issuer) != requested_issuer:
        raise IdentityBootstrapError("requested OIDC issuer does not match OIDC_ISSUER")
    try:
        role = Role(payload["role"])
    except ValueError as exc:
        raise IdentityBootstrapError(
            "request file role is not a canonical application role"
        ) from exc
    return BootstrapRequest(
        organization_id=payload["organization_id"],
        organization_slug=payload["organization_slug"],
        organization_name=payload["organization_name"],
        user_id=payload["user_id"],
        oidc_issuer=requested_issuer,
        oidc_subject=payload["oidc_subject"],
        email=payload["email"],
        user_name=payload["user_name"],
        role=role,
        operator_reference=payload["operator_reference"],
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise IdentityBootstrapError("request file contains duplicate JSON keys")
        result[key] = value
    return result


def _load_request_file(path_value: str) -> object:
    path = Path(path_value)
    if not path.is_absolute():
        raise IdentityBootstrapError("request file path must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise IdentityBootstrapError("request file cannot be opened securely") from exc
    encoded = b""
    try:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if not stat.S_ISREG(metadata.st_mode):
            raise IdentityBootstrapError("request file must be a regular file")
        if metadata.st_uid != os.geteuid() or mode not in {0o400, 0o600}:
            raise IdentityBootstrapError(
                "request file must be owned by the process user with mode 0400 or 0600"
            )
        if metadata.st_size < 1 or metadata.st_size > MAX_REQUEST_FILE_BYTES:
            raise IdentityBootstrapError("request file size is outside the safe range")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            encoded = source.read(MAX_REQUEST_FILE_BYTES + 1)
    except OSError as exc:
        raise IdentityBootstrapError("request file cannot be read securely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(encoded) > MAX_REQUEST_FILE_BYTES:
        raise IdentityBootstrapError("request file size is outside the safe range")
    try:
        return json.loads(encoded.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityBootstrapError("request file must contain strict UTF-8 JSON") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request-file",
        required=True,
        help="absolute path to a process-owned mode-0400/0600 JSON request",
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument(
        "--confirm-initial-admin",
        action="store_true",
        help="confirm the privileged production identity write",
    )
    operation.add_argument(
        "--confirm-additional-member",
        action="store_true",
        help="confirm a designer/reviewer write to an existing organization",
    )
    operation.add_argument(
        "--confirm-legacy-issuer-binding",
        action="store_true",
        help="confirm migration of one exact legacy or unmarked OIDC identity",
    )
    return parser


def _engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 10,
            "options": (
                "-c statement_timeout=30000 -c lock_timeout=10000 "
                "-c search_path=pg_catalog,public"
            ),
        },
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    runtime_environment = os.environ if environment is None else environment
    engine: Engine | None = None
    try:
        database_url = _production_database_url(runtime_environment)
        request = _request_from_payload(
            _load_request_file(arguments.request_file),
            runtime_environment,
        )
        engine = _engine(database_url)
        factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        with factory.begin() as session:
            _guard_database_connection(session)
            if arguments.confirm_initial_admin:
                result = bootstrap_identity(session, request)
            elif arguments.confirm_additional_member:
                result = provision_additional_member(session, request)
            else:
                result = bind_legacy_oidc_identity(session, request)
    except IdentityBootstrapError as exc:
        print(f"production identity bootstrap refused: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        # SQLAlchemy exceptions can include statements and identity values.  Do
        # not expose them from this privileged operator command.
        print("production identity bootstrap failed: database operation failed", file=sys.stderr)
        return 3
    finally:
        if engine is not None:
            engine.dispose()
    print(
        json.dumps(
            {
                "status": result.status,
                "organization_id": result.organization_id,
                "user_id": result.user_id,
                "role": result.role.value,
                "audit_event_id": result.audit_event_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
