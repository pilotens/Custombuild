from __future__ import annotations

from types import SimpleNamespace
from typing import get_args, get_type_hints

import app.api as api_module
import pytest
from app.auth import (
    ROLE_CAPABILITIES,
    Capability,
    Principal,
    capabilities_for_role,
    require_capability,
)
from app.models import Role
from fastapi import HTTPException, params


def _principal(role: Role) -> Principal:
    return Principal(
        user_id=f"user-{role.value}",
        organization_id="11111111-1111-4111-8111-111111111111",
        role=role,
        subject=f"test:{role.value}",
        email=f"{role.value}@example.test",
        name=role.value.title(),
    )


EXPECTED_NON_ADMIN_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.viewer: frozenset({Capability.READ}),
    Role.operator: frozenset(
        {
            Capability.READ,
            Capability.EXECUTABLE_CAM_DOWNLOAD,
            Capability.JOINT_RETENTION_EVIDENCE_DOWNLOAD,
            Capability.WORKSHOP_CHALLENGE,
            Capability.WORKSHOP_EVIDENCE,
            Capability.WORKSHOP_ATTEST,
        }
    ),
    Role.designer: frozenset(
        {
            Capability.READ,
            Capability.DESIGN,
            Capability.GENERATE,
        }
    ),
    Role.production: frozenset(
        {
            Capability.READ,
            Capability.EXECUTABLE_CAM_DOWNLOAD,
            Capability.JOINT_RETENTION_EVIDENCE_DOWNLOAD,
            Capability.PRODUCTION_RELEASE,
            Capability.WORKSHOP_PREPARE,
            Capability.WORKSHOP_CHALLENGE,
            Capability.WORKSHOP_EVIDENCE,
            Capability.WORKSHOP_ATTEST,
        }
    ),
    Role.reviewer: frozenset(
        {
            Capability.READ,
            Capability.EXECUTABLE_CAM_DOWNLOAD,
            Capability.REVIEW,
            Capability.JOINT_RETENTION_EVIDENCE_DOWNLOAD,
            Capability.WORKSHOP_VERIFY,
        }
    ),
}


def test_every_role_has_a_closed_explicit_capability_set() -> None:
    assert set(ROLE_CAPABILITIES) == set(Role)
    for role, expected in EXPECTED_NON_ADMIN_CAPABILITIES.items():
        assert capabilities_for_role(role) == expected

    assert capabilities_for_role(Role.admin) == frozenset(Capability)
    assert capabilities_for_role(Role.owner) == frozenset(Capability)


@pytest.mark.parametrize("role", (Role.reviewer, Role.production, Role.operator, Role.viewer))
@pytest.mark.parametrize("capability", (Capability.DESIGN, Capability.GENERATE))
def test_non_design_roles_cannot_design_or_generate(
    role: Role,
    capability: Capability,
) -> None:
    with pytest.raises(HTTPException) as caught:
        require_capability(capability)(_principal(role))

    assert caught.value.status_code == 403
    assert caught.value.detail == f"Capability {capability.value} required"


@pytest.mark.parametrize("role", (Role.designer, Role.production, Role.operator, Role.viewer))
def test_only_review_roles_can_review(role: Role) -> None:
    with pytest.raises(HTTPException, match="Capability review required"):
        require_capability(Capability.REVIEW)(_principal(role))


def test_release_endpoint_uses_the_exact_production_release_capability() -> None:
    annotation = get_type_hints(
        api_module.release_version,
        include_extras=True,
    )["principal"]
    dependencies = [item for item in get_args(annotation) if isinstance(item, params.Depends)]
    assert len(dependencies) == 1
    dependency = dependencies[0].dependency
    assert dependency is not None

    for role in (Role.production, Role.admin, Role.owner):
        principal = _principal(role)
        # The capability dependency returns the authenticated principal unchanged,
        # preserving the exact actor used by release and audit persistence.
        assert dependency(principal) is principal

    for role in (Role.viewer, Role.designer, Role.operator, Role.reviewer):
        with pytest.raises(
            HTTPException,
            match="Capability production_release required",
        ) as caught:
            dependency(_principal(role))
        assert caught.value.status_code == 403


@pytest.mark.parametrize(
    ("role", "allowed"),
    (
        (Role.viewer, False),
        (Role.designer, False),
        (Role.reviewer, False),
        (Role.operator, True),
        (Role.production, True),
        (Role.admin, True),
        (Role.owner, True),
    ),
)
def test_workshop_attestation_is_granted_without_role_inheritance(
    role: Role,
    allowed: bool,
) -> None:
    dependency = require_capability(Capability.WORKSHOP_ATTEST)
    if allowed:
        assert dependency(_principal(role)).role is role
    else:
        with pytest.raises(HTTPException):
            dependency(_principal(role))


def test_workshop_checker_and_revocation_are_separate() -> None:
    reviewer = _principal(Role.reviewer)
    assert require_capability(Capability.WORKSHOP_VERIFY)(reviewer) is reviewer
    with pytest.raises(HTTPException):
        require_capability(Capability.WORKSHOP_ATTEST)(reviewer)
    with pytest.raises(HTTPException):
        require_capability(Capability.WORKSHOP_REVOKE)(reviewer)

    for role in (Role.admin, Role.owner):
        principal = _principal(role)
        assert require_capability(Capability.WORKSHOP_REVOKE)(principal) is principal


@pytest.mark.parametrize(
    ("role", "allowed"),
    (
        (Role.viewer, False),
        (Role.designer, False),
        (Role.reviewer, True),
        (Role.operator, True),
        (Role.production, True),
        (Role.admin, True),
        (Role.owner, True),
    ),
)
def test_joint_retention_evidence_download_has_an_explicit_closed_role_set(
    role: Role,
    allowed: bool,
) -> None:
    dependency = require_capability(Capability.JOINT_RETENTION_EVIDENCE_DOWNLOAD)
    if allowed:
        assert dependency(_principal(role)) is not None
    else:
        with pytest.raises(
            HTTPException,
            match="Capability joint_retention_evidence_download required",
        ):
            dependency(_principal(role))


@pytest.mark.parametrize(
    ("role", "allowed"),
    (
        (Role.viewer, False),
        (Role.designer, False),
        (Role.operator, True),
        (Role.reviewer, True),
        (Role.production, True),
        (Role.admin, True),
        (Role.owner, True),
    ),
)
def test_cutting_cam_artifact_read_has_an_explicit_closed_role_set(
    role: Role,
    allowed: bool,
) -> None:
    dependency = require_capability(Capability.EXECUTABLE_CAM_DOWNLOAD)
    if allowed:
        assert dependency(_principal(role)) is not None
    else:
        with pytest.raises(
            HTTPException,
            match="Capability executable_cam_download required",
        ):
            dependency(_principal(role))


@pytest.mark.parametrize(
    "kind",
    (
        "cam_candidate_bundle",
        "cutting_toolpaths",
        "machine_program_index",
        "cutting_program_validation_report",
        "cutting_backplot",
        "production_machine_profile",
        "machine_program_001",
        "machine_program_999",
    ),
)
def test_every_executable_cam_artifact_kind_uses_the_capability_guard(kind: str) -> None:
    assert api_module._is_cam_candidate_artifact_kind(kind) is True
    with pytest.raises(
        HTTPException,
        match="Capability executable_cam_download required",
    ):
        api_module._require_cam_candidate_artifact_read(
            _principal(Role.viewer),
            kind,
        )


@pytest.mark.parametrize(
    ("role", "allowed"),
    (
        (Role.viewer, False),
        (Role.designer, False),
        (Role.operator, False),
        (Role.reviewer, True),
        (Role.production, False),
        (Role.admin, True),
        (Role.owner, True),
    ),
)
def test_current_job_cam_review_surface_has_a_closed_role_set(
    role: Role,
    allowed: bool,
) -> None:
    principal = _principal(role)
    assert api_module._can_read_current_cam_review_artifacts(principal) is allowed
    if allowed:
        api_module._require_current_cam_review_artifact_read(
            principal,
            "machine_program_001",
        )
    else:
        with pytest.raises(HTTPException):
            api_module._require_current_cam_review_artifact_read(
                principal,
                "machine_program_001",
            )


def test_design_artifacts_remain_outside_the_executable_cam_capability() -> None:
    viewer = _principal(Role.viewer)
    for kind in ("manifest", "production_bundle", "operations", "setup_sheet_001"):
        assert api_module._is_cam_candidate_artifact_kind(kind) is False
        api_module._require_cam_candidate_artifact_read(viewer, kind)


@pytest.mark.parametrize(
    ("role", "allowed"),
    (
        (Role.viewer, False),
        (Role.designer, False),
        (Role.reviewer, True),
        (Role.operator, True),
        (Role.production, True),
        (Role.admin, True),
        (Role.owner, True),
    ),
)
def test_retention_bound_bundle_uses_the_exact_evidence_download_capability(
    monkeypatch: pytest.MonkeyPatch,
    role: Role,
    allowed: bool,
) -> None:
    monkeypatch.setattr(
        api_module.BookcaseDesignSpec,
        "model_validate",
        staticmethod(lambda _value: SimpleNamespace(joint_retention=object())),
    )
    version = SimpleNamespace(spec_json={})

    if allowed:
        api_module._require_retention_bound_bundle_download_capability(
            _principal(role),
            version,  # type: ignore[arg-type]
            "production_bundle",
        )
    else:
        with pytest.raises(
            HTTPException,
            match="Capability joint_retention_evidence_download required",
        ):
            api_module._require_retention_bound_bundle_download_capability(
                _principal(role),
                version,  # type: ignore[arg-type]
                "production_bundle",
            )


def test_bundle_capability_gate_does_not_expand_to_unbound_or_non_bundle_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = SimpleNamespace(spec_json={})
    monkeypatch.setattr(
        api_module.BookcaseDesignSpec,
        "model_validate",
        staticmethod(lambda _value: SimpleNamespace(joint_retention=None)),
    )
    api_module._require_retention_bound_bundle_download_capability(
        _principal(Role.viewer),
        version,  # type: ignore[arg-type]
        "production_bundle",
    )

    def must_not_parse(_value: object) -> None:
        raise AssertionError("non-bundle authorization unexpectedly parsed the DesignSpec")

    monkeypatch.setattr(
        api_module.BookcaseDesignSpec,
        "model_validate",
        staticmethod(must_not_parse),
    )
    api_module._require_retention_bound_bundle_download_capability(
        _principal(Role.viewer),
        version,  # type: ignore[arg-type]
        "operations",
    )
