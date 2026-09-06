"""Explicitly activate a monotonic production joint-retention trust registry.

This one-shot operator command uses the short-lived migrator database identity.
API and worker runtimes have no activation privilege and never auto-pin policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from app.config_guards import validate_production_database_url
from app.joint_retention_registry import (
    MAX_TRUST_REGISTRY_BYTES,
    JointRetentionRegistryError,
    joint_retention_registry_binding,
    parse_joint_retention_registry_json,
    validate_monotonic_registry_transition,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

REGISTRY_ACTIVATION_LOCK_ID = 4_340_449_326_452_121_818
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class RegistryActivationError(RuntimeError):
    """The requested production registry activation is unsafe or ambiguous."""


@dataclass(frozen=True, slots=True)
class RegistryActivationResult:
    status: str
    transition_epoch: int
    registry_sha256: str


def _operator_reference(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > 160
        or CONTROL_CHARACTERS.search(value) is not None
        or (value.startswith("__REPLACE_") and value.endswith("__"))
    ):
        raise RegistryActivationError(
            "operator reference must be trimmed, non-placeholder text of at most 160 characters"
        )
    return value


def _load_registry_file(path_value: str) -> Mapping[str, object]:
    path = Path(path_value)
    if not path.is_absolute():
        raise RegistryActivationError("registry file path must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RegistryActivationError("registry file cannot be opened securely") from exc
    encoded = b""
    try:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if not stat.S_ISREG(metadata.st_mode):
            raise RegistryActivationError("registry file must be a regular file")
        if metadata.st_uid != os.geteuid() or mode not in {0o400, 0o600}:
            raise RegistryActivationError(
                "registry file must be process-owned with mode 0400 or 0600"
            )
        if metadata.st_size < 1 or metadata.st_size > MAX_TRUST_REGISTRY_BYTES:
            raise RegistryActivationError("registry file size is outside the safe range")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            encoded = source.read(MAX_TRUST_REGISTRY_BYTES + 1)
            after_read = os.fstat(source.fileno())
            before_identity = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_uid,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            after_identity = (
                after_read.st_dev,
                after_read.st_ino,
                after_read.st_uid,
                stat.S_IMODE(after_read.st_mode),
                after_read.st_size,
                after_read.st_mtime_ns,
                after_read.st_ctime_ns,
            )
            if after_identity != before_identity:
                raise RegistryActivationError(
                    "registry file changed while it was being read"
                )
    except OSError as exc:
        raise RegistryActivationError("registry file cannot be read securely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(encoded) > MAX_TRUST_REGISTRY_BYTES:
        raise RegistryActivationError("registry file size is outside the safe range")
    try:
        decoded = encoded.decode("utf-8")
        return parse_joint_retention_registry_json(decoded)
    except (UnicodeDecodeError, JointRetentionRegistryError) as exc:
        raise RegistryActivationError("registry file is not a valid trust registry") from exc


def expected_schema_head() -> str:
    config_path = REPOSITORY_ROOT / "services" / "api" / "alembic.ini"
    config = Config(str(config_path))
    config.set_main_option(
        "script_location",
        str(REPOSITORY_ROOT / "services" / "api" / "alembic"),
    )
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise RegistryActivationError("repository must have exactly one Alembic head")
    return heads[0]


def _production_database_url(environment: Mapping[str, str]) -> str:
    if environment.get("APP_ENV") != "production":
        raise RegistryActivationError("APP_ENV must be explicitly set to production")
    database_url = environment.get("DATABASE_URL", "")
    try:
        validate_production_database_url(
            database_url,
            expected_username="custombuild_migrator",
            setting_name="DATABASE_URL",
        )
    except ValueError as exc:
        raise RegistryActivationError(str(exc)) from exc
    if make_url(database_url).drivername != "postgresql+psycopg":
        raise RegistryActivationError("DATABASE_URL must use postgresql+psycopg")
    return database_url


def _guard_database_connection(session: Session) -> None:
    role = (
        session.execute(
            text(
                "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolinherit, "
                "rolreplication, rolbypassrls, "
                "pg_catalog.current_schemas(false) AS active_schemas "
                "FROM pg_catalog.pg_roles WHERE rolname = current_user"
            )
        )
        .mappings()
        .one_or_none()
    )
    if role is None or role["rolname"] != "custombuild_migrator":
        raise RegistryActivationError("database connection is not the production migrator role")
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
        raise RegistryActivationError("production migrator database role is over-privileged")
    active_schemas = role["active_schemas"]
    if not isinstance(active_schemas, list | tuple) or tuple(active_schemas) != (
        "pg_catalog",
        "public",
    ):
        raise RegistryActivationError("database search path is not pinned to pg_catalog,public")
    relations = {
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
                "('alembic_version', 'joint_retention_registry_state')"
            )
        ).tuples()
    }
    if relations != {
        "alembic_version": ("custombuild_migrator", False, False),
        "joint_retention_registry_state": ("custombuild_migrator", False, False),
    }:
        raise RegistryActivationError(
            "required registry relations are missing, misowned or unexpectedly RLS-enabled"
        )
    membership_count = session.scalar(
        text(
            "SELECT count(*) FROM pg_catalog.pg_auth_members membership "
            "JOIN pg_catalog.pg_roles member_role ON member_role.oid = membership.member "
            "JOIN pg_catalog.pg_roles granted_role ON granted_role.oid = membership.roleid "
            "WHERE member_role.rolname = current_user OR granted_role.rolname = current_user"
        )
    )
    if membership_count != 0:
        raise RegistryActivationError("production migrator database role has role memberships")
    functions = session.execute(
        text(
            "SELECT procedure.prosecdef, procedure.proconfig, owner.rolname, "
            "language.lanname "
            "FROM pg_catalog.pg_proc procedure "
            "JOIN pg_catalog.pg_roles owner ON owner.oid = procedure.proowner "
            "JOIN pg_catalog.pg_language language ON language.oid = procedure.prolang "
            "WHERE procedure.oid = ANY(CAST(:functions AS regprocedure[]))"
        ),
        {
            "functions": [
                "public.custombuild_joint_retention_install_registry(jsonb,text,text,text)",
                "public.custombuild_joint_retention_assert_registry(text,text)",
            ]
        },
    ).all()
    if len(functions) != 2 or any(
        tuple(row)
        != (
            True,
            ["search_path=pg_catalog, public"],
            "custombuild_migrator",
            "plpgsql",
        )
        for row in functions
    ):
        raise RegistryActivationError(
            "joint-retention registry functions are missing or have unsafe ownership"
        )
    unexpected_acl = tuple(
        session.execute(
            text(
                "WITH unexpected_acl AS ("
                "SELECT 'table'::pg_catalog.text AS object_kind, "
                "'public.joint_retention_registry_state'::pg_catalog.text "
                "AS object_identity, "
                "CASE WHEN acl.grantee = 0 THEN 'PUBLIC' "
                "ELSE grantee.rolname END AS grantee, acl.privilege_type "
                "FROM pg_catalog.pg_class relation "
                "JOIN pg_catalog.pg_namespace namespace "
                "ON namespace.oid = relation.relnamespace "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                "relation.relacl, pg_catalog.acldefault('r', relation.relowner))) acl "
                "LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid = acl.grantee "
                "WHERE namespace.nspname = 'public' "
                "AND relation.relname = 'joint_retention_registry_state' "
                "AND acl.grantee <> relation.relowner "
                "UNION ALL "
                "SELECT 'column'::pg_catalog.text, "
                "'public.joint_retention_registry_state.'::pg_catalog.text "
                "|| attribute.attname, "
                "CASE WHEN acl.grantee = 0 THEN 'PUBLIC' "
                "ELSE grantee.rolname END, acl.privilege_type "
                "FROM pg_catalog.pg_attribute attribute "
                "JOIN pg_catalog.pg_class relation ON relation.oid = attribute.attrelid "
                "JOIN pg_catalog.pg_namespace namespace "
                "ON namespace.oid = relation.relnamespace "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) acl "
                "LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid = acl.grantee "
                "WHERE namespace.nspname = 'public' "
                "AND relation.relname = 'joint_retention_registry_state' "
                "AND attribute.attnum > 0 AND NOT attribute.attisdropped "
                "AND attribute.attacl IS NOT NULL "
                "AND acl.grantee <> relation.relowner "
                "UNION ALL "
                "SELECT 'function'::pg_catalog.text, "
                "procedure.oid::pg_catalog.regprocedure::pg_catalog.text, "
                "CASE WHEN acl.grantee = 0 THEN 'PUBLIC' "
                "ELSE grantee.rolname END, acl.privilege_type "
                "FROM pg_catalog.pg_proc procedure "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                "procedure.proacl, pg_catalog.acldefault('f', procedure.proowner))) acl "
                "LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid = acl.grantee "
                "WHERE procedure.oid = ANY(CAST(:functions AS pg_catalog.regprocedure[])) "
                "AND NOT ((acl.grantee = procedure.proowner "
                "AND acl.privilege_type = 'EXECUTE' "
                "AND acl.is_grantable IS FALSE) OR ("
                "procedure.oid = CAST(:assertion AS pg_catalog.regprocedure) "
                "AND acl.grantee IN (SELECT allowed.oid FROM pg_catalog.pg_roles allowed "
                "WHERE allowed.rolname IN ('custombuild_api', 'custombuild_worker')) "
                "AND acl.privilege_type = 'EXECUTE' "
                "AND acl.is_grantable IS FALSE)) "
                ") SELECT object_kind, object_identity, grantee, privilege_type "
                "FROM unexpected_acl ORDER BY object_kind, object_identity, grantee, "
                "privilege_type"
            ),
            {
                "functions": [
                    "public.custombuild_joint_retention_install_registry(jsonb,text,text,text)",
                    "public.custombuild_joint_retention_assert_registry(text,text)",
                ],
                "assertion": (
                    "public.custombuild_joint_retention_assert_registry(text,text)"
                ),
            },
        ).tuples()
    )
    if unexpected_acl:
        raise RegistryActivationError(
            "joint-retention registry objects have unexpected direct ACL grantees"
        )
    privileges = session.execute(
        text(
            "SELECT "
            "pg_catalog.has_function_privilege('custombuild_api', "
            "'public.custombuild_joint_retention_install_registry(jsonb,text,text,text)', "
            "'EXECUTE'), "
            "pg_catalog.has_function_privilege('custombuild_worker', "
            "'public.custombuild_joint_retention_install_registry(jsonb,text,text,text)', "
            "'EXECUTE'), "
            "pg_catalog.has_function_privilege('custombuild_storage_attestor', "
            "'public.custombuild_joint_retention_install_registry(jsonb,text,text,text)', "
            "'EXECUTE'), "
            "pg_catalog.has_function_privilege('custombuild_api', "
            "'public.custombuild_joint_retention_assert_registry(text,text)', 'EXECUTE'), "
            "pg_catalog.has_function_privilege('custombuild_worker', "
            "'public.custombuild_joint_retention_assert_registry(text,text)', 'EXECUTE'), "
            "pg_catalog.has_function_privilege('custombuild_storage_attestor', "
            "'public.custombuild_joint_retention_assert_registry(text,text)', 'EXECUTE'), "
            "pg_catalog.has_function_privilege('custombuild_migrator', "
            "'public.custombuild_joint_retention_install_registry(jsonb,text,text,text)', "
            "'EXECUTE'), "
            "pg_catalog.has_function_privilege('custombuild_migrator', "
            "'public.custombuild_joint_retention_assert_registry(text,text)', 'EXECUTE'), "
            "pg_catalog.has_table_privilege('custombuild_api', "
            "'public.joint_retention_registry_state', 'SELECT,INSERT,UPDATE,DELETE'), "
            "pg_catalog.has_table_privilege('custombuild_worker', "
            "'public.joint_retention_registry_state', 'SELECT,INSERT,UPDATE,DELETE'), "
            "pg_catalog.has_table_privilege('custombuild_storage_attestor', "
            "'public.joint_retention_registry_state', 'SELECT,INSERT,UPDATE,DELETE')"
        )
    ).one()
    if tuple(privileges) != (
        False,
        False,
        False,
        True,
        True,
        False,
        True,
        True,
        False,
        False,
        False,
    ):
        raise RegistryActivationError("joint-retention registry privileges are not exact")
    current_revisions = tuple(
        session.execute(
            text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
        ).scalars()
    )
    if current_revisions != (expected_schema_head(),):
        raise RegistryActivationError("database schema is not at the repository Alembic head")
    session.execute(
        text("SELECT pg_catalog.pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": REGISTRY_ACTIVATION_LOCK_ID},
    )


def activate_registry(
    session: Session,
    registry: Mapping[str, object],
    *,
    operator_reference: str,
) -> RegistryActivationResult:
    reference = _operator_reference(operator_reference)
    binding = joint_retention_registry_binding(registry)
    # Keep the helper safe when invoked directly rather than through ``main``:
    # the global policy lock must precede the singleton row lock everywhere.
    session.execute(
        text("SELECT pg_catalog.pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": REGISTRY_ACTIVATION_LOCK_ID},
    )
    previous_canonical = session.scalar(
        text(
            "SELECT registry_canonical_json FROM public.joint_retention_registry_state "
            "WHERE id = 1 FOR UPDATE"
        )
    )
    if previous_canonical is not None:
        if not isinstance(previous_canonical, str):
            raise RegistryActivationError("activated registry state is malformed")
        try:
            previous = parse_joint_retention_registry_json(previous_canonical)
            validate_monotonic_registry_transition(previous, registry)
        except JointRetentionRegistryError as exc:
            raise RegistryActivationError(str(exc)) from exc
    result = (
        session.execute(
            text(
                "SELECT activated_epoch, changed FROM "
                "public.custombuild_joint_retention_install_registry("
                "CAST(:registry_json AS pg_catalog.jsonb), :canonical_json, "
                ":registry_sha256, :operator_reference_sha256)"
            ),
            {
                "registry_json": binding.canonical_json,
                "canonical_json": binding.canonical_json,
                "registry_sha256": binding.sha256,
                "operator_reference_sha256": hashlib.sha256(reference.encode("utf-8")).hexdigest(),
            },
        )
        .mappings()
        .one()
    )
    epoch = result["activated_epoch"]
    changed = result["changed"]
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise RegistryActivationError("database returned an invalid registry epoch")
    if not isinstance(changed, bool):
        raise RegistryActivationError("database returned an invalid activation status")
    asserted_epoch = session.scalar(
        text(
            "SELECT public.custombuild_joint_retention_assert_registry("
            ":canonical_json, :registry_sha256)"
        ),
        {
            "canonical_json": binding.canonical_json,
            "registry_sha256": binding.sha256,
        },
    )
    if asserted_epoch != epoch:
        raise RegistryActivationError("post-activation registry assertion failed")
    return RegistryActivationResult(
        status="activated" if changed else "unchanged",
        transition_epoch=epoch,
        registry_sha256=binding.sha256,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry-file",
        required=True,
        help="absolute path to a process-owned mode-0400/0600 registry JSON file",
    )
    parser.add_argument(
        "--operator-reference",
        required=True,
        help="non-secret deployment/change approval reference (only its SHA-256 is stored)",
    )
    parser.add_argument(
        "--confirm-activation",
        required=True,
        action="store_true",
        help="confirm the monotonic production trust-policy activation",
    )
    return parser


def _engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 10,
            "options": (
                "-c statement_timeout=30000 -c lock_timeout=10000 -c search_path=pg_catalog,public"
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
        registry = _load_registry_file(arguments.registry_file)
        engine = _engine(database_url)
        factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        with factory.begin() as session:
            _guard_database_connection(session)
            result = activate_registry(
                session,
                registry,
                operator_reference=arguments.operator_reference,
            )
    except (RegistryActivationError, JointRetentionRegistryError) as exc:
        print(f"joint-retention registry activation refused: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        # Database exceptions can include the connection URL or registry
        # statement.  Never echo them from this privileged one-shot command.
        print(
            "joint-retention registry activation failed: database operation failed",
            file=sys.stderr,
        )
        return 3
    finally:
        if engine is not None:
            engine.dispose()
    print(
        json.dumps(
            {
                "status": result.status,
                "transition_epoch": result.transition_epoch,
                "registry_sha256": result.registry_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
