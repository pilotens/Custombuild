from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from app.joint_retention_registry import (
    JointRetentionRegistryError,
    assert_joint_retention_registry_activated,
    joint_retention_registry_binding,
    parse_joint_retention_registry_json,
    validate_monotonic_registry_transition,
)
from sqlalchemy.orm import Session

from scripts import activate_joint_retention_registry as activation

MIGRATION = Path("services/api/alembic/versions/0018_joint_retention_registry_state.py")


def _issuer(
    *,
    issuer_id: str = "independent-lab",
    key_id: str = "ed25519-2026-01",
    public_key: bytes = bytes(range(32)),
) -> dict[str, object]:
    return {
        "issuer_id": issuer_id,
        "key_id": key_id,
        "role": "joint_retention_certifier",
        "public_key_base64": base64.b64encode(public_key).decode("ascii"),
        "not_before": "2026-01-01T00:00:00Z",
        "not_after": "2028-01-01T00:00:00Z",
        "revoked_at": None,
    }


def _registry() -> dict[str, Any]:
    return {
        "schema_version": "custombuild.joint-retention-trust-registry.v1",
        "issuers": [_issuer()],
        "revoked_statement_sha256": [],
        "revoked_system_versions": [],
    }


def test_registry_parser_rejects_duplicate_keys_and_non_objects() -> None:
    with pytest.raises(JointRetentionRegistryError, match="duplicate"):
        parse_joint_retention_registry_json('{"schema_version":"a","schema_version":"b"}')
    with pytest.raises(JointRetentionRegistryError, match="JSON object"):
        parse_joint_retention_registry_json("[]")


def test_registry_binding_uses_exact_canonical_bytes_and_validates_public_key() -> None:
    registry = _registry()
    binding = joint_retention_registry_binding(registry)
    expected = json.dumps(
        registry,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

    assert binding.canonical_json == expected
    assert binding.sha256 == hashlib.sha256(expected.encode()).hexdigest()

    registry["issuers"][0]["public_key_base64"] = "A" * 44
    with pytest.raises(JointRetentionRegistryError, match="invalid"):
        joint_retention_registry_binding(registry)


def test_monotonic_transition_is_idempotent_and_accepts_only_additive_trust_change() -> None:
    previous = _registry()
    assert validate_monotonic_registry_transition(previous, copy.deepcopy(previous)) is False

    candidate = copy.deepcopy(previous)
    candidate["issuers"][0]["revoked_at"] = "2026-09-03T12:00:00Z"
    additional = _issuer(key_id="ed25519-2027-01", public_key=b"\x01" * 32)
    candidate["issuers"].append(additional)
    candidate["revoked_statement_sha256"] = ["a" * 64]
    candidate["revoked_system_versions"] = ["mechanical-dado-lock@1.0.0"]

    assert validate_monotonic_registry_transition(previous, candidate) is True


@pytest.mark.parametrize(
    "additional",
    (
        _issuer(key_id="ed25519-2027-01"),
        _issuer(issuer_id="second-lab", key_id="ed25519-2026-01"),
    ),
)
def test_registry_rejects_duplicate_public_key_material_across_identities(
    additional: dict[str, object],
) -> None:
    candidate = _registry()
    candidate["issuers"].append(additional)
    candidate["issuers"].sort(key=lambda item: (item["issuer_id"], item["key_id"]))

    with pytest.raises(JointRetentionRegistryError, match="invalid"):
        joint_retention_registry_binding(candidate)


@pytest.mark.parametrize("revoked", (False, True))
def test_monotonic_transition_rejects_activated_key_material_alias(
    revoked: bool,
) -> None:
    previous = _registry()
    if revoked:
        previous["issuers"][0]["revoked_at"] = "2026-09-03T12:00:00Z"
    candidate = copy.deepcopy(previous)
    candidate["issuers"].append(_issuer(key_id="ed25519-2027-01"))

    with pytest.raises(JointRetentionRegistryError, match="invalid"):
        validate_monotonic_registry_transition(previous, candidate)


def test_monotonic_transition_rejects_issuer_removal_and_mutation() -> None:
    previous = _registry()
    removed = copy.deepcopy(previous)
    removed["issuers"] = []
    with pytest.raises(JointRetentionRegistryError, match="remove"):
        validate_monotonic_registry_transition(previous, removed)

    mutated = copy.deepcopy(previous)
    mutated["issuers"][0]["not_after"] = "2029-01-01T00:00:00Z"
    with pytest.raises(JointRetentionRegistryError, match="mutate"):
        validate_monotonic_registry_transition(previous, mutated)


def test_monotonic_transition_rejects_every_revocation_rollback() -> None:
    previous = _registry()
    previous["issuers"][0]["revoked_at"] = "2026-09-03T12:00:00Z"
    previous["revoked_statement_sha256"] = ["a" * 64]
    previous["revoked_system_versions"] = ["mechanical-dado-lock@1.0.0"]

    issuer_unrevoked = copy.deepcopy(previous)
    issuer_unrevoked["issuers"][0]["revoked_at"] = None
    with pytest.raises(JointRetentionRegistryError, match="revocation"):
        validate_monotonic_registry_transition(previous, issuer_unrevoked)

    issuer_rewritten = copy.deepcopy(previous)
    issuer_rewritten["issuers"][0]["revoked_at"] = "2026-09-04T12:00:00Z"
    with pytest.raises(JointRetentionRegistryError, match="revocation"):
        validate_monotonic_registry_transition(previous, issuer_rewritten)

    statement_unrevoked = copy.deepcopy(previous)
    statement_unrevoked["revoked_statement_sha256"] = []
    with pytest.raises(JointRetentionRegistryError, match="revoked_statement"):
        validate_monotonic_registry_transition(previous, statement_unrevoked)

    system_unrevoked = copy.deepcopy(previous)
    system_unrevoked["revoked_system_versions"] = []
    with pytest.raises(JointRetentionRegistryError, match="revoked_system"):
        validate_monotonic_registry_transition(previous, system_unrevoked)


@pytest.mark.parametrize(
    "candidate_revoked_at",
    (
        "2026-09-02T12:00:00Z",
        "2026-09-03T14:00:00+02:00",
    ),
)
def test_monotonic_transition_allows_earlier_or_offset_equivalent_revocation(
    candidate_revoked_at: str,
) -> None:
    previous = _registry()
    previous["issuers"][0]["revoked_at"] = "2026-09-03T12:00:00Z"
    candidate = copy.deepcopy(previous)
    candidate["issuers"][0]["revoked_at"] = candidate_revoked_at

    assert validate_monotonic_registry_transition(previous, candidate) is True


class _RuntimeExecutor:
    def __init__(self, dialect: str, epoch: object = 7) -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self.epoch = epoch
        self.calls: list[tuple[str, object | None]] = []

    def scalar(self, statement: object, parameters: object | None = None) -> object:
        self.calls.append((str(statement), parameters))
        return self.epoch


def test_runtime_assertion_skips_without_production_proof_and_rejects_sqlite() -> None:
    executor = _RuntimeExecutor("sqlite")

    assert (
        assert_joint_retention_registry_activated(
            executor,  # type: ignore[arg-type]
            _registry(),
            production=False,
        )
        is None
    )
    assert executor.calls == []
    with pytest.raises(JointRetentionRegistryError, match="requires PostgreSQL"):
        assert_joint_retention_registry_activated(
            executor,  # type: ignore[arg-type]
            _registry(),
            production=True,
        )


def test_runtime_assertion_binds_both_canonical_bytes_and_digest() -> None:
    executor = _RuntimeExecutor("postgresql")
    binding = joint_retention_registry_binding(_registry())

    epoch = assert_joint_retention_registry_activated(
        executor,  # type: ignore[arg-type]
        _registry(),
        production=True,
    )

    assert epoch == 7
    assert executor.calls == [
        (
            "SELECT public.custombuild_joint_retention_assert_registry("
            ":canonical_json, :registry_sha256)",
            {
                "canonical_json": binding.canonical_json,
                "registry_sha256": binding.sha256,
            },
        )
    ]


class _ActivationResult:
    def __init__(self, *, epoch: int, changed: bool) -> None:
        self.row = {"activated_epoch": epoch, "changed": changed}

    def mappings(self) -> _ActivationResult:
        return self

    def one(self) -> dict[str, object]:
        return self.row


class _ActivationSession:
    def __init__(
        self,
        *,
        previous_canonical: str | None,
        epoch: int,
        changed: bool,
    ) -> None:
        self.previous_canonical = previous_canonical
        self.epoch = epoch
        self.changed = changed
        self.install_parameters: dict[str, object] | None = None
        self.call_order: list[str] = []

    def scalar(self, statement: object, parameters: object | None = None) -> object:
        del parameters
        if "SELECT registry_canonical_json" in str(statement):
            self.call_order.append("row_lock")
            return self.previous_canonical
        if "custombuild_joint_retention_assert_registry" in str(statement):
            self.call_order.append("assert")
            return self.epoch
        raise AssertionError(f"unexpected scalar: {statement}")

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> object:
        if "pg_advisory_xact_lock" in str(statement):
            assert parameters == {"lock_id": activation.REGISTRY_ACTIVATION_LOCK_ID}
            self.call_order.append("policy_lock")
            return object()
        assert "custombuild_joint_retention_install_registry" in str(statement)
        assert parameters is not None
        self.call_order.append("install")
        self.install_parameters = parameters
        return _ActivationResult(epoch=self.epoch, changed=self.changed)


@pytest.mark.parametrize(
    ("previous", "changed", "expected_status"),
    ((None, True, "activated"), ("current", False, "unchanged")),
)
def test_operator_activation_reports_first_install_and_idempotence_without_reference_leakage(
    previous: str | None,
    changed: bool,
    expected_status: str,
) -> None:
    registry = _registry()
    binding = joint_retention_registry_binding(registry)
    session = _ActivationSession(
        previous_canonical=binding.canonical_json if previous == "current" else None,
        epoch=1,
        changed=changed,
    )

    result = activation.activate_registry(
        session,  # type: ignore[arg-type]
        registry,
        operator_reference="CHANGE-2026-0042",
    )

    assert result.status == expected_status
    assert result.transition_epoch == 1
    assert result.registry_sha256 == binding.sha256
    assert session.call_order == ["policy_lock", "row_lock", "install", "assert"]
    assert session.install_parameters is not None
    assert "CHANGE-2026-0042" not in repr(session.install_parameters)
    assert session.install_parameters["operator_reference_sha256"] == hashlib.sha256(
        b"CHANGE-2026-0042"
    ).hexdigest()


def test_operator_registry_file_is_single_read_regular_process_owned_file(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(_registry()), encoding="utf-8")
    registry_path.chmod(0o600)

    assert activation._load_registry_file(str(registry_path)) == _registry()

    registry_path.chmod(0o644)
    with pytest.raises(activation.RegistryActivationError, match="process-owned"):
        activation._load_registry_file(str(registry_path))
    symlink_path = tmp_path / "registry-link.json"
    symlink_path.symlink_to(registry_path)
    with pytest.raises(activation.RegistryActivationError, match="opened securely"):
        activation._load_registry_file(str(symlink_path))


def test_operator_registry_file_detects_in_place_read_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(_registry()), encoding="utf-8")
    registry_path.chmod(0o600)
    real_fstat = activation.os.fstat
    calls = 0

    def changed_fstat(descriptor: int) -> object:
        nonlocal calls
        calls += 1
        metadata = real_fstat(descriptor)
        if calls == 1:
            return metadata
        return SimpleNamespace(
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_uid=metadata.st_uid,
            st_mode=metadata.st_mode,
            st_size=metadata.st_size,
            st_mtime_ns=metadata.st_mtime_ns + 1,
            st_ctime_ns=metadata.st_ctime_ns + 1,
        )

    monkeypatch.setattr(activation.os, "fstat", changed_fstat)

    with pytest.raises(activation.RegistryActivationError, match="changed"):
        activation._load_registry_file(str(registry_path))


def test_operator_requires_explicit_production_migrator_url() -> None:
    good_url = (
        "postgresql+psycopg://custombuild_migrator:unit-test-strong-secret-0001@db/custombuild"
    )
    assert (
        activation._production_database_url({"APP_ENV": "production", "DATABASE_URL": good_url})
        == good_url
    )
    with pytest.raises(activation.RegistryActivationError, match="APP_ENV"):
        activation._production_database_url({"DATABASE_URL": good_url})
    with pytest.raises(activation.RegistryActivationError, match="custombuild_migrator"):
        activation._production_database_url(
            {
                "APP_ENV": "production",
                "DATABASE_URL": (
                    "postgresql+psycopg://custombuild_api:"
                    "unit-test-strong-secret-0001@db/custombuild"
                ),
            }
        )


def _guarded_activation_session(
    *,
    unexpected_acl: tuple[tuple[str, str, str, str], ...] = (),
) -> Session:
    role_result = Mock()
    role_result.mappings.return_value.one_or_none.return_value = {
        "rolname": "custombuild_migrator",
        "rolsuper": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolinherit": False,
        "rolreplication": False,
        "rolbypassrls": False,
        "active_schemas": ["pg_catalog", "public"],
    }
    relations_result = Mock()
    relations_result.tuples.return_value = (
        ("alembic_version", "custombuild_migrator", False, False),
        ("joint_retention_registry_state", "custombuild_migrator", False, False),
    )
    functions_result = Mock()
    functions_result.all.return_value = [
        (True, ["search_path=pg_catalog, public"], "custombuild_migrator", "plpgsql"),
        (True, ["search_path=pg_catalog, public"], "custombuild_migrator", "plpgsql"),
    ]
    privileges_result = Mock()
    privileges_result.one.return_value = (
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
    )
    unexpected_result = Mock()
    unexpected_result.tuples.return_value = unexpected_acl
    revisions_result = Mock()
    revisions_result.scalars.return_value = iter(("0018_joint_retention_registry_state",))
    lock_result = Mock()
    session = Mock(spec=Session)
    session.execute.side_effect = (
        role_result,
        relations_result,
        functions_result,
        unexpected_result,
        privileges_result,
        revisions_result,
        lock_result,
    )
    session.scalar.return_value = 0
    return cast(Session, session)


def test_operator_database_guard_accepts_only_exact_registry_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        activation,
        "expected_schema_head",
        lambda: "0018_joint_retention_registry_state",
    )
    session = _guarded_activation_session()

    activation._guard_database_connection(session)

    calls = cast(Mock, session).execute.call_args_list
    acl_statement = str(calls[3].args[0])
    assert "pg_catalog.aclexplode" in acl_statement
    assert "acl.grantee = 0 THEN 'PUBLIC'" in acl_statement
    assert "custombuild_api', 'custombuild_worker'" in acl_statement
    assert "acl.is_grantable IS FALSE" in acl_statement


@pytest.mark.parametrize(
    "unexpected_acl",
    (
        (("table", "public.joint_retention_registry_state", "rogue_login", "SELECT"),),
        (
            (
                "function",
                "custombuild_joint_retention_assert_registry(text,text)",
                "rogue_group",
                "EXECUTE",
            ),
        ),
        (
            (
                "column",
                "public.joint_retention_registry_state.registry_sha256",
                "rogue_group",
                "UPDATE",
            ),
        ),
        (("table", "public.joint_retention_registry_state", "PUBLIC", "SELECT"),),
    ),
)
def test_operator_database_guard_rejects_unexpected_login_group_or_public_acl(
    monkeypatch: pytest.MonkeyPatch,
    unexpected_acl: tuple[tuple[str, str, str, str], ...],
) -> None:
    monkeypatch.setattr(
        activation,
        "expected_schema_head",
        lambda: "0018_joint_retention_registry_state",
    )

    with pytest.raises(activation.RegistryActivationError, match="unexpected direct ACL"):
        activation._guard_database_connection(
            _guarded_activation_session(unexpected_acl=unexpected_acl)
        )


def test_migration_has_global_locked_function_only_acl_and_downgrade_guard() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision = "0017_oidc_issuer_binding"' in source
    assert "pg_advisory_xact_lock(4340449326452121818)" in source
    assert "pg_advisory_xact_lock_shared(4340449326452121818)" in source
    assert "SECURITY DEFINER" in source
    assert "SET search_path TO pg_catalog, public" in source
    assert "REVOKE ALL PRIVILEGES ON TABLE" in source
    assert "pg_catalog.aclexplode" in source
    assert "pg_catalog.pg_attribute" in source
    assert "attribute.attacl" in source
    assert "'public.joint_retention_registry_state FROM PUBLIC'" in source
    assert "'public.joint_retention_registry_state FROM %I'" in source
    assert "REVOKE ALL ON FUNCTION %s FROM %I" in source
    assert "GRANT EXECUTE ON FUNCTION {INSTALL_FUNCTION} TO custombuild_migrator" in source
    assert '"TO custombuild_migrator, custombuild_api, custombuild_worker"' in source
    assert "revocations cannot be removed" in source
    assert "issuer key cannot be removed" in source
    assert "revocation delayed or cleared" in source
    assert "::pg_catalog.timestamptz" in source
    assert "issuer key material rebound" in source
    assert "issuer key material is duplicated" in source
    assert "!~ '^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$'" in source
    assert "JOINT_RETENTION_REGISTRY_DOWNGRADE_BLOCKED" in source
    assert 'schema="public" if bind.dialect.name == "postgresql" else None' in source
    assert '"INSERT INTO public.joint_retention_registry_state "' in source
    # PostgreSQL accepts unqualified SQL aliases such as BIGINT and BOOLEAN, but
    # their actual pg_catalog type names are int8 and bool.  Qualifying an alias
    # (for example pg_catalog.bigint) makes a fresh PostgreSQL migration fail.
    assert "pg_catalog.int8" in source
    assert "pg_catalog.bool" in source
    assert "pg_catalog.bigint" not in source
    assert "pg_catalog.boolean" not in source
