"""Closed, canonical software provenance for executable CAM candidates.

``SOURCE_MANIFEST_SHA256`` is the build's code-root identity.  It is not a
container-image digest and must not be presented as one.  Candidate packages
also bind the dependency lock and every versioned implementation that turns
reviewed operations into controller programs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from custombuild_cam import (
    CUTTING_BACKPLOT_VERSION,
    CUTTING_PROGRAM_VERIFIER_VERSION,
    PRODUCTION_TOOLPATH_ENGINE_VERSION,
    PRODUCTION_TOOLPATH_SCHEMA_VERSION,
)
from custombuild_postprocessors import (
    LINUXCNC_PRODUCTION_POSTPROCESSOR_ID,
    LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION,
    PRODUCTION_GCODE_PARSER_VERSION,
    PRODUCTION_GCODE_SAFETY_VALIDATOR_VERSION,
)

from .model import canonical_json_bytes, sha256_hex

PRODUCER_BUILD_IDENTITY_SCHEMA_VERSION = "custombuild.producer-build-identity.v1"
CAM_SOFTWARE_PROVENANCE_SCHEMA_VERSION = "custombuild.cam-software-provenance.v1"
CAM_CANDIDATE_MANIFEST_SCHEMA_VERSION = "custombuild.cam-candidate-manifest.v2"
CAM_CANDIDATE_PACKAGE_BUILDER_VERSION = "deterministic-cam-candidate-package-1.1.0"
SOURCE_MANIFEST_CODE_ROOT_KIND = "SOURCE_MANIFEST_SHA256"
CURRENT_CAM_IMPLEMENTATION_SUPPORT_ID = "custombuild.cam-implementation-stack.v1"
CAM_CANDIDATE_VERIFICATION_DISPATCH_V1 = "custombuild.cam-candidate-verifier.v1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PRODUCTION_VCS_REF_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_BUILD_IDENTITY_KEYS = frozenset(
    {
        "schema_version",
        "app_version",
        "vcs_ref",
        "source_manifest_sha256",
        "dependency_lock_sha256",
    }
)
_PROVENANCE_KEYS = frozenset({"schema_version", "code_root", "producer_build", "implementations"})
_CODE_ROOT_KEYS = frozenset({"kind", "sha256"})
_IMPLEMENTATION_KEYS = frozenset(
    {
        "toolpath_schema_version",
        "toolpath_engine_version",
        "cutting_verifier_version",
        "cutting_backplot_version",
        "postprocessor_id",
        "postprocessor_version",
        "gcode_parser_version",
        "gcode_safety_validator_version",
        "candidate_manifest_schema_version",
        "candidate_package_builder_version",
    }
)


class CAMSoftwareProvenanceError(ValueError):
    """A producer identity or CAM implementation binding is invalid."""


@dataclass(frozen=True, slots=True)
class ProducerBuildIdentity:
    schema_version: str
    app_version: str
    vcs_ref: str
    source_manifest_sha256: str
    dependency_lock_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "vcs_ref": self.vcs_ref,
            "source_manifest_sha256": self.source_manifest_sha256,
            "dependency_lock_sha256": self.dependency_lock_sha256,
        }


@dataclass(frozen=True, slots=True)
class CAMImplementationIdentity:
    """One explicitly supported, dispatchable interpretation of a CAM package."""

    support_id: str
    verification_dispatch: str
    toolpath_schema_version: str
    toolpath_engine_version: str
    cutting_verifier_version: str
    cutting_backplot_version: str
    postprocessor_id: str
    postprocessor_version: str
    gcode_parser_version: str
    gcode_safety_validator_version: str
    candidate_manifest_schema_version: str
    candidate_package_builder_version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "toolpath_schema_version": self.toolpath_schema_version,
            "toolpath_engine_version": self.toolpath_engine_version,
            "cutting_verifier_version": self.cutting_verifier_version,
            "cutting_backplot_version": self.cutting_backplot_version,
            "postprocessor_id": self.postprocessor_id,
            "postprocessor_version": self.postprocessor_version,
            "gcode_parser_version": self.gcode_parser_version,
            "gcode_safety_validator_version": self.gcode_safety_validator_version,
            "candidate_manifest_schema_version": self.candidate_manifest_schema_version,
            "candidate_package_builder_version": self.candidate_package_builder_version,
        }

    @property
    def dispatch_key(self) -> tuple[str, str, str]:
        """Return the exact registry key for executable verification code.

        The support and dispatch identifiers are human-reviewable routing
        labels.  The digest additionally binds every implementation field, so
        changing any parser, verifier, postprocessor, schema, or builder
        version necessarily selects a different callable.
        """

        identity = {
            "support_id": self.support_id,
            "verification_dispatch": self.verification_dispatch,
            "implementations": self.as_dict(),
        }
        return (
            self.support_id,
            self.verification_dispatch,
            sha256_hex(canonical_json_bytes(identity)),
        )


# Historical support is deliberately an explicit source-controlled allowlist.
# A past identity must remain here together with a real verification dispatch;
# accepting an arbitrary self-declared version would only rename unverified code.
_SUPPORTED_CAM_IMPLEMENTATION_IDENTITIES: Mapping[str, CAMImplementationIdentity] = (
    MappingProxyType(
        {
            CURRENT_CAM_IMPLEMENTATION_SUPPORT_ID: CAMImplementationIdentity(
                support_id=CURRENT_CAM_IMPLEMENTATION_SUPPORT_ID,
                verification_dispatch=CAM_CANDIDATE_VERIFICATION_DISPATCH_V1,
                toolpath_schema_version="custombuild.toolpaths.v1",
                toolpath_engine_version="production-toolpaths-1.1.0",
                cutting_verifier_version="cutting-program-verifier-1.1.0",
                cutting_backplot_version="cutting-backplot-1.1.0",
                postprocessor_id="linuxcnc-3axis-production",
                postprocessor_version="1.1.0",
                gcode_parser_version="linuxcnc-production-parser-1.3.0",
                gcode_safety_validator_version="linuxcnc-production-safety-1.3.0",
                candidate_manifest_schema_version="custombuild.cam-candidate-manifest.v2",
                candidate_package_builder_version="deterministic-cam-candidate-package-1.1.0",
            )
        }
    )
)


def parse_producer_build_identity(
    value: object,
    *,
    allow_test_only: bool = False,
) -> ProducerBuildIdentity:
    """Parse one exact externally supplied producer-build identity document."""

    if type(allow_test_only) is not bool:
        raise CAMSoftwareProvenanceError("allow_test_only must be an explicit boolean")
    if not isinstance(value, Mapping) or frozenset(value) != _BUILD_IDENTITY_KEYS:
        raise CAMSoftwareProvenanceError(
            "producer build identity must have the exact closed v1 structure"
        )
    schema_version = _required_string(value.get("schema_version"), "schema_version")
    app_version = _required_string(value.get("app_version"), "app_version")
    vcs_ref = _required_string(value.get("vcs_ref"), "vcs_ref")
    source_manifest_sha256 = _required_sha256(
        value.get("source_manifest_sha256"), "source_manifest_sha256"
    )
    dependency_lock_sha256 = _required_sha256(
        value.get("dependency_lock_sha256"), "dependency_lock_sha256"
    )
    if schema_version != PRODUCER_BUILD_IDENTITY_SCHEMA_VERSION:
        raise CAMSoftwareProvenanceError("producer build identity schema is unsupported")
    if not allow_test_only and _PRODUCTION_VCS_REF_RE.fullmatch(vcs_ref) is None:
        raise CAMSoftwareProvenanceError(
            "production producer vcs_ref must be a 40- or 64-character lowercase digest"
        )
    return ProducerBuildIdentity(
        schema_version=schema_version,
        app_version=app_version,
        vcs_ref=vcs_ref,
        source_manifest_sha256=source_manifest_sha256,
        dependency_lock_sha256=dependency_lock_sha256,
    )


def producer_build_identity_from_engine_context(
    context: object,
    *,
    allow_test_only: bool = False,
) -> ProducerBuildIdentity:
    """Project the four validated build facts from a production engine context."""

    if not isinstance(context, Mapping):
        raise CAMSoftwareProvenanceError("production engine context must be an object")
    value = {
        "schema_version": PRODUCER_BUILD_IDENTITY_SCHEMA_VERSION,
        "app_version": context.get("app_version"),
        "vcs_ref": context.get("vcs_ref"),
        "source_manifest_sha256": context.get("source_manifest_sha256"),
        "dependency_lock_sha256": context.get("dependency_lock_sha256"),
    }
    return parse_producer_build_identity(value, allow_test_only=allow_test_only)


def test_only_producer_build_identity() -> ProducerBuildIdentity:
    """Return a conspicuous deterministic identity for explicit TEST_ONLY fixtures."""

    return ProducerBuildIdentity(
        schema_version=PRODUCER_BUILD_IDENTITY_SCHEMA_VERSION,
        app_version="0.0.0-test-only",
        vcs_ref="TEST_ONLY_UNATTESTED_BUILD",
        source_manifest_sha256=sha256_hex(b"TEST_ONLY_UNATTESTED_SOURCE_MANIFEST"),
        dependency_lock_sha256=sha256_hex(b"TEST_ONLY_UNATTESTED_DEPENDENCY_LOCK"),
    )


def current_cam_implementation_versions() -> dict[str, str]:
    """Return the exact versions whose code generated and verified a candidate."""

    runtime_versions = {
        "toolpath_schema_version": PRODUCTION_TOOLPATH_SCHEMA_VERSION,
        "toolpath_engine_version": PRODUCTION_TOOLPATH_ENGINE_VERSION,
        "cutting_verifier_version": CUTTING_PROGRAM_VERIFIER_VERSION,
        "cutting_backplot_version": CUTTING_BACKPLOT_VERSION,
        "postprocessor_id": LINUXCNC_PRODUCTION_POSTPROCESSOR_ID,
        "postprocessor_version": LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION,
        "gcode_parser_version": PRODUCTION_GCODE_PARSER_VERSION,
        "gcode_safety_validator_version": PRODUCTION_GCODE_SAFETY_VALIDATOR_VERSION,
        "candidate_manifest_schema_version": CAM_CANDIDATE_MANIFEST_SCHEMA_VERSION,
        "candidate_package_builder_version": CAM_CANDIDATE_PACKAGE_BUILDER_VERSION,
    }
    identity = _SUPPORTED_CAM_IMPLEMENTATION_IDENTITIES.get(CURRENT_CAM_IMPLEMENTATION_SUPPORT_ID)
    if identity is None or identity.as_dict() != runtime_versions:
        raise CAMSoftwareProvenanceError(
            "current CAM implementation constants have no exact verification dispatch"
        )
    return runtime_versions


def current_cam_implementation_identity() -> CAMImplementationIdentity:
    """Return the registry entry for the code currently executing."""

    current_versions = current_cam_implementation_versions()
    identity = _SUPPORTED_CAM_IMPLEMENTATION_IDENTITIES[CURRENT_CAM_IMPLEMENTATION_SUPPORT_ID]
    if identity.as_dict() != current_versions:  # pragma: no cover - guarded above.
        raise CAMSoftwareProvenanceError("current CAM implementation registry is inconsistent")
    return identity


def supported_cam_implementation_identities() -> tuple[CAMImplementationIdentity, ...]:
    """Return the immutable, explicitly dispatchable implementation allowlist."""

    identities = tuple(
        _SUPPORTED_CAM_IMPLEMENTATION_IDENTITIES[key]
        for key in sorted(_SUPPORTED_CAM_IMPLEMENTATION_IDENTITIES)
    )
    fingerprints = {canonical_json_bytes(identity.as_dict()) for identity in identities}
    if len(fingerprints) != len(identities):
        raise CAMSoftwareProvenanceError("CAM implementation registry contains aliases")
    return identities


def parse_supported_cam_implementation_identity(
    value: object,
) -> CAMImplementationIdentity:
    """Resolve an exact frozen implementation object through the support registry."""

    if not isinstance(value, Mapping) or frozenset(value) != _IMPLEMENTATION_KEYS:
        raise CAMSoftwareProvenanceError("CAM implementation identity is not closed")
    candidate: dict[str, str] = {}
    for key in sorted(_IMPLEMENTATION_KEYS):
        item = value.get(key)
        if not isinstance(item, str) or not item or item != item.strip() or len(item) > 200:
            raise CAMSoftwareProvenanceError("CAM implementation identity is invalid")
        candidate[key] = item
    matches = tuple(
        identity
        for identity in supported_cam_implementation_identities()
        if identity.as_dict() == candidate
    )
    if len(matches) != 1:
        raise CAMSoftwareProvenanceError("CAM implementation versions are unsupported or stale")
    return matches[0]


def build_cam_software_provenance(
    producer_build: ProducerBuildIdentity | Mapping[str, Any],
    *,
    allow_test_only: bool = False,
) -> dict[str, Any]:
    """Build the canonical provenance object embedded in a candidate manifest."""

    identity = (
        producer_build
        if isinstance(producer_build, ProducerBuildIdentity)
        else parse_producer_build_identity(producer_build, allow_test_only=allow_test_only)
    )
    if isinstance(identity, ProducerBuildIdentity) and not allow_test_only:
        # Dataclass construction is public, so production callers still receive
        # the exact same closed validation as JSON/mapping callers.
        identity = parse_producer_build_identity(identity.as_dict())
    value: dict[str, Any] = {
        "schema_version": CAM_SOFTWARE_PROVENANCE_SCHEMA_VERSION,
        "code_root": {
            "kind": SOURCE_MANIFEST_CODE_ROOT_KIND,
            "sha256": identity.source_manifest_sha256,
        },
        "producer_build": identity.as_dict(),
        "implementations": current_cam_implementation_versions(),
    }
    validate_cam_software_provenance(value, allow_test_only=allow_test_only)
    return value


def validate_cam_software_provenance(
    value: object,
    *,
    expected_producer_build: ProducerBuildIdentity | Mapping[str, Any] | None = None,
    allow_test_only: bool = False,
    require_current_implementations: bool = True,
) -> ProducerBuildIdentity:
    """Validate a closed provenance object against this exact implementation."""

    if type(require_current_implementations) is not bool:
        raise CAMSoftwareProvenanceError(
            "require_current_implementations must be an explicit boolean"
        )
    if not isinstance(value, Mapping) or frozenset(value) != _PROVENANCE_KEYS:
        raise CAMSoftwareProvenanceError(
            "CAM software provenance must have the exact closed v1 structure"
        )
    if value.get("schema_version") != CAM_SOFTWARE_PROVENANCE_SCHEMA_VERSION:
        raise CAMSoftwareProvenanceError("CAM software provenance schema is unsupported")
    code_root = value.get("code_root")
    implementations = value.get("implementations")
    if not isinstance(code_root, Mapping) or frozenset(code_root) != _CODE_ROOT_KEYS:
        raise CAMSoftwareProvenanceError("CAM code-root binding is invalid")
    code_root_sha256 = code_root.get("sha256")
    if (
        code_root.get("kind") != SOURCE_MANIFEST_CODE_ROOT_KIND
        or not isinstance(code_root_sha256, str)
        or _SHA256_RE.fullmatch(code_root_sha256) is None
    ):
        raise CAMSoftwareProvenanceError("CAM code-root binding is invalid")
    implementation_identity = parse_supported_cam_implementation_identity(implementations)
    if (
        require_current_implementations
        and implementation_identity.support_id != current_cam_implementation_identity().support_id
    ):
        raise CAMSoftwareProvenanceError("CAM implementation versions are not current")
    identity = parse_producer_build_identity(
        value.get("producer_build"), allow_test_only=allow_test_only
    )
    if code_root_sha256 != identity.source_manifest_sha256:
        raise CAMSoftwareProvenanceError(
            "CAM code root differs from producer SOURCE_MANIFEST_SHA256"
        )
    if expected_producer_build is not None:
        expected = (
            expected_producer_build
            if isinstance(expected_producer_build, ProducerBuildIdentity)
            else parse_producer_build_identity(
                expected_producer_build, allow_test_only=allow_test_only
            )
        )
        if canonical_json_bytes(identity.as_dict()) != canonical_json_bytes(expected.as_dict()):
            raise CAMSoftwareProvenanceError(
                "CAM producer build identity differs from the independently bound build"
            )
    return identity


def cam_software_provenance_sha256(
    value: object,
    *,
    allow_test_only: bool = False,
    require_current_implementations: bool = True,
) -> str:
    """Validate then fingerprint one canonical provenance object."""

    validate_cam_software_provenance(
        value,
        allow_test_only=allow_test_only,
        require_current_implementations=require_current_implementations,
    )
    return sha256_hex(canonical_json_bytes(value))


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 200:
        raise CAMSoftwareProvenanceError(f"producer build {label} is invalid")
    return value


def _required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CAMSoftwareProvenanceError(
            f"producer build {label} must be 64 lowercase hexadecimal characters"
        )
    return value


__all__ = [
    "CAM_CANDIDATE_MANIFEST_SCHEMA_VERSION",
    "CAM_CANDIDATE_PACKAGE_BUILDER_VERSION",
    "CAM_CANDIDATE_VERIFICATION_DISPATCH_V1",
    "CAM_SOFTWARE_PROVENANCE_SCHEMA_VERSION",
    "CURRENT_CAM_IMPLEMENTATION_SUPPORT_ID",
    "PRODUCER_BUILD_IDENTITY_SCHEMA_VERSION",
    "SOURCE_MANIFEST_CODE_ROOT_KIND",
    "CAMSoftwareProvenanceError",
    "CAMImplementationIdentity",
    "ProducerBuildIdentity",
    "build_cam_software_provenance",
    "cam_software_provenance_sha256",
    "current_cam_implementation_versions",
    "current_cam_implementation_identity",
    "parse_producer_build_identity",
    "parse_supported_cam_implementation_identity",
    "producer_build_identity_from_engine_context",
    "test_only_producer_build_identity",
    "supported_cam_implementation_identities",
    "validate_cam_software_provenance",
]
