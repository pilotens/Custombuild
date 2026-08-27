"""Render and verify the immutable input to release promotion.

The descriptor deliberately contains no deployment secrets or mutable image tags.
It binds the exact release evidence, source identity, registry manifest digests,
Compose service roles and signer policy that a later privileged workflow must verify.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "custombuild.deploy-descriptor.v1"
RELEASE_EVIDENCE_SCHEMA_VERSION = "custombuild.release-readiness-evidence.v1"
REGISTRY_OVERLAY_SCHEMA_VERSION = "custombuild.compose-registry.v1"
PROMOTION_WORKFLOW_PATH = ".github/workflows/cd.yml"
PROMOTION_SOURCE_REF = "refs/heads/main"
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"

APPLICATION_COMPONENTS = ("api", "worker", "web", "seaweedfs")
PINNED_RUNTIME_IMAGES = {
    "postgres": (
        "cgr.dev/chainguard/postgres@"
        "sha256:3af67abef0353ec61f054acf649abb5eaaae9742a9c1c9125e073c7833736060"
    ),
    "redis": (
        "docker.io/library/redis@"
        "sha256:05a97a479bc73de66f087dc05b569010772880f778cc8671fa6b8aadee32e5c6"
    ),
    "volume-init": (
        "cgr.dev/chainguard/busybox@"
        "sha256:928939fc7f20750dea03366627d83bfa497df565fcf6b55fdddb004ecd8426d6"
    ),
}
ALL_COMPONENTS = (*APPLICATION_COMPONENTS, *PINNED_RUNTIME_IMAGES)
ROLE_COMPONENTS = {
    "api": "api",
    "migrate": "api",
    "worker": "worker",
    "scheduler": "worker",
    "web": "web",
    "object-storage": "seaweedfs",
    "postgres": "postgres",
    "redis": "redis",
    "object-storage-init": "volume-init",
}

EXPECTED_REGISTRY_OVERLAY = (
    "# Generated image references must come from a verified deploy descriptor.\n"
    "# Always invoke this overlay with `docker compose ... up --no-build`.\n"
    "x-custombuild-registry-contract:\n"
    f"  schema-version: {REGISTRY_OVERLAY_SCHEMA_VERSION}\n"
    f"  descriptor-schema-version: {SCHEMA_VERSION}\n"
    "  required-up-flag: --no-build\n"
    "\n"
    "services:\n"
    "  postgres:\n"
    f"    image: {PINNED_RUNTIME_IMAGES['postgres']}\n"
    "  redis:\n"
    f"    image: {PINNED_RUNTIME_IMAGES['redis']}\n"
    "  object-storage-init:\n"
    f"    image: {PINNED_RUNTIME_IMAGES['volume-init']}\n"
    "  object-storage:\n"
    "    image: ${CUSTOMBUILD_DEPLOY_SEAWEEDFS_IMAGE:?Set from a verified deploy descriptor}\n"
    "  migrate:\n"
    "    image: ${CUSTOMBUILD_DEPLOY_API_IMAGE:?Set from a verified deploy descriptor}\n"
    "  api:\n"
    "    image: ${CUSTOMBUILD_DEPLOY_API_IMAGE:?Set from a verified deploy descriptor}\n"
    "  worker:\n"
    "    image: ${CUSTOMBUILD_DEPLOY_WORKER_IMAGE:?Set from a verified deploy descriptor}\n"
    "  scheduler:\n"
    "    image: ${CUSTOMBUILD_DEPLOY_WORKER_IMAGE:?Set from a verified deploy descriptor}\n"
    "  web:\n"
    "    image: ${CUSTOMBUILD_DEPLOY_WEB_IMAGE:?Set from a verified deploy descriptor}\n"
)

ROOT_KEYS = {
    "schema_version",
    "repository",
    "git_revision",
    "source_manifest_sha256",
    "release_evidence",
    "release_workflow",
    "images",
    "roles",
    "signing_policy",
}
RELEASE_EVIDENCE_KEYS = {"schema_version", "sha256"}
RELEASE_WORKFLOW_KEYS = {"run_id", "run_attempt", "workflow_path", "source_ref"}
SIGNING_POLICY_KEYS = {"cosign", "github_attestations"}
COSIGN_POLICY_KEYS = {"certificate_identity", "certificate_oidc_issuer"}
GITHUB_ATTESTATION_POLICY_KEYS = {
    "repository",
    "signer_workflow",
    "source_ref",
    "source_digest",
    "deny_self_hosted_runners",
}

SHA256 = re.compile(r"^[a-f0-9]{64}$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
REVISION = re.compile(r"^[a-f0-9]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
IMAGE_NAME = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)
MAX_DESCRIPTOR_BYTES = 128 * 1024
MAX_RELEASE_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_WORKFLOW_INTEGER = 9_223_372_036_854_775_807


class DescriptorError(RuntimeError):
    """A descriptor or its bound evidence is not safe to promote."""


def canonical_json_bytes(value: object) -> bytes:
    """Return the sole accepted descriptor representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _bounded_canonical_descriptor_bytes(value: object) -> bytes:
    try:
        canonical = canonical_json_bytes(value)
    except (RecursionError, TypeError, ValueError) as exc:
        raise DescriptorError("deploy descriptor cannot be represented canonically") from exc
    if len(canonical) > MAX_DESCRIPTOR_BYTES:
        raise DescriptorError("deploy descriptor exceeds its size limit")
    return canonical


def _reject_constant(value: str) -> object:
    raise DescriptorError(f"JSON contains the forbidden numeric constant {value}")


def _object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DescriptorError("JSON contains a duplicate object key")
        result[key] = value
    return result


def _parse_json(data: bytes, *, label: str, maximum_size: int) -> dict[str, Any]:
    if not data or len(data) > maximum_size:
        raise DescriptorError(f"{label} is empty or exceeds its size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DescriptorError(f"{label} is not UTF-8") from exc
    if text.startswith("\ufeff"):
        raise DescriptorError(f"{label} must not contain a byte-order mark")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_from_pairs,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise DescriptorError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise DescriptorError(f"{label} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _exact_object(value: object, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise DescriptorError(f"{label} does not have the exact required fields")
    return {str(key): item for key, item in value.items()}


def _required_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DescriptorError(f"{label} must be a non-empty canonical string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DescriptorError(f"{label} contains a control character")
    return value


def _sha256(value: object, *, label: str) -> str:
    candidate = _required_string(value, label=label)
    if not SHA256.fullmatch(candidate):
        raise DescriptorError(f"{label} must be a lowercase SHA-256")
    return candidate


def _revision(value: object, *, label: str = "git_revision") -> str:
    candidate = _required_string(value, label=label)
    if not REVISION.fullmatch(candidate):
        raise DescriptorError(f"{label} must be a full lowercase Git commit")
    return candidate


def _positive_integer(value: object, *, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > MAX_WORKFLOW_INTEGER
    ):
        raise DescriptorError(f"{label} must be a bounded positive integer")
    return value


def _repository(value: object) -> str:
    candidate = _required_string(value, label="repository")
    if not REPOSITORY.fullmatch(candidate) or ".." in candidate:
        raise DescriptorError("repository must be an exact owner/name identity")
    return candidate


def _image_reference(value: object, *, label: str) -> tuple[str, str]:
    reference = _required_string(value, label=label)
    if reference.count("@") != 1:
        raise DescriptorError(f"{label} must be an exact digest-only OCI reference")
    name, digest = reference.split("@", 1)
    if not IMAGE_NAME.fullmatch(name) or name != name.lower() or not DIGEST.fullmatch(digest):
        raise DescriptorError(f"{label} must be a lowercase name@sha256 OCI reference")
    return name, digest


def application_image_name(repository: str, component: str) -> str:
    if component not in APPLICATION_COMPONENTS:
        raise DescriptorError(f"unknown application component {component!r}")
    return f"ghcr.io/{repository.lower()}-{component}"


def _release_evidence(
    data: bytes,
    *,
    git_revision: str,
    source_manifest_sha256: str,
    image_digests: dict[str, str],
) -> dict[str, Any]:
    evidence = _parse_json(
        data,
        label="release evidence",
        maximum_size=MAX_RELEASE_EVIDENCE_BYTES,
    )
    if evidence.get("schema_version") != RELEASE_EVIDENCE_SCHEMA_VERSION:
        raise DescriptorError("release evidence has another schema version")
    if evidence.get("git_revision") != git_revision:
        raise DescriptorError("release evidence belongs to another Git revision")
    if evidence.get("source_manifest_sha256") != source_manifest_sha256:
        raise DescriptorError("release evidence belongs to another source manifest")
    if evidence.get("software_release_ready") is not True:
        raise DescriptorError("release evidence did not pass the software gate")
    if evidence.get("commercial_release_ready") is not False:
        raise DescriptorError("release evidence misstates commercial release authority")
    if evidence.get("physical_machine_release_ready") is not False:
        raise DescriptorError("release evidence misstates physical machine authority")
    static_report = evidence.get("static_report")
    if (
        not isinstance(static_report, dict)
        or static_report.get("static_controls_ready") is not True
    ):
        raise DescriptorError("release evidence did not preserve passed static controls")
    manifest_digests = evidence.get("runtime_manifest_digests")
    if not isinstance(manifest_digests, dict) or set(manifest_digests) != set(ALL_COMPONENTS):
        raise DescriptorError("release evidence has another runtime image set")
    for component, digest in image_digests.items():
        if manifest_digests.get(component) != digest:
            raise DescriptorError(
                f"release evidence has another manifest digest for {component}"
            )
    return evidence


def _expected_signing_policy(repository: str, git_revision: str) -> dict[str, object]:
    signer_workflow = f"{repository}/{PROMOTION_WORKFLOW_PATH}"
    return {
        "cosign": {
            "certificate_identity": (
                f"https://github.com/{signer_workflow}@{PROMOTION_SOURCE_REF}"
            ),
            "certificate_oidc_issuer": GITHUB_OIDC_ISSUER,
        },
        "github_attestations": {
            "repository": repository,
            "signer_workflow": signer_workflow,
            "source_ref": PROMOTION_SOURCE_REF,
            "source_digest": git_revision,
            "deny_self_hosted_runners": True,
        },
    }


def _validate_descriptor_object(
    descriptor: dict[str, Any],
    *,
    release_evidence_bytes: bytes,
    expected_repository: str,
    expected_git_revision: str,
    expected_source_manifest_sha256: str,
    expected_workflow_run_id: int,
    expected_workflow_run_attempt: int,
) -> None:
    root = _exact_object(descriptor, ROOT_KEYS, label="descriptor")
    if root["schema_version"] != SCHEMA_VERSION:
        raise DescriptorError("descriptor has another schema version")
    repository = _repository(root["repository"])
    git_revision = _revision(root["git_revision"])
    source_manifest_sha256 = _sha256(
        root["source_manifest_sha256"],
        label="source_manifest_sha256",
    )
    if repository != _repository(expected_repository):
        raise DescriptorError("descriptor belongs to another repository")
    if git_revision != _revision(expected_git_revision, label="expected_git_revision"):
        raise DescriptorError("descriptor belongs to another Git revision")
    if source_manifest_sha256 != _sha256(
        expected_source_manifest_sha256,
        label="expected_source_manifest_sha256",
    ):
        raise DescriptorError("descriptor belongs to another source manifest")

    release_evidence = _exact_object(
        root["release_evidence"],
        RELEASE_EVIDENCE_KEYS,
        label="release_evidence",
    )
    if release_evidence["schema_version"] != RELEASE_EVIDENCE_SCHEMA_VERSION:
        raise DescriptorError("descriptor names another release evidence schema")
    if release_evidence["sha256"] != hashlib.sha256(release_evidence_bytes).hexdigest():
        raise DescriptorError("descriptor release evidence digest does not match")

    release_workflow = _exact_object(
        root["release_workflow"],
        RELEASE_WORKFLOW_KEYS,
        label="release_workflow",
    )
    run_id = _positive_integer(release_workflow["run_id"], label="release_workflow.run_id")
    run_attempt = _positive_integer(
        release_workflow["run_attempt"],
        label="release_workflow.run_attempt",
    )
    if run_id != _positive_integer(expected_workflow_run_id, label="expected_workflow_run_id"):
        raise DescriptorError("descriptor belongs to another workflow run")
    if run_attempt != _positive_integer(
        expected_workflow_run_attempt,
        label="expected_workflow_run_attempt",
    ):
        raise DescriptorError("descriptor belongs to another workflow run attempt")
    if release_workflow["workflow_path"] != PROMOTION_WORKFLOW_PATH:
        raise DescriptorError("descriptor names another promotion workflow")
    if release_workflow["source_ref"] != PROMOTION_SOURCE_REF:
        raise DescriptorError("descriptor names another promotion source ref")

    images = _exact_object(root["images"], set(ALL_COMPONENTS), label="images")
    image_digests: dict[str, str] = {}
    for component in APPLICATION_COMPONENTS:
        name, digest = _image_reference(images[component], label=f"images.{component}")
        if name != application_image_name(repository, component):
            raise DescriptorError(f"images.{component} has another registry repository")
        image_digests[component] = digest
    for component, expected_reference in PINNED_RUNTIME_IMAGES.items():
        name, digest = _image_reference(images[component], label=f"images.{component}")
        expected_name, expected_digest = _image_reference(
            expected_reference,
            label=f"pinned runtime {component}",
        )
        if name != expected_name or digest != expected_digest:
            raise DescriptorError(f"images.{component} is not the reviewed runtime digest")
        image_digests[component] = digest
    if len(set(image_digests.values())) != len(ALL_COMPONENTS):
        raise DescriptorError("descriptor substitutes one image digest for another component")

    roles = _exact_object(root["roles"], set(ROLE_COMPONENTS), label="roles")
    if roles != ROLE_COMPONENTS:
        raise DescriptorError("descriptor service roles do not match the reviewed mapping")

    policy = _exact_object(
        root["signing_policy"],
        SIGNING_POLICY_KEYS,
        label="signing_policy",
    )
    _exact_object(policy["cosign"], COSIGN_POLICY_KEYS, label="signing_policy.cosign")
    _exact_object(
        policy["github_attestations"],
        GITHUB_ATTESTATION_POLICY_KEYS,
        label="signing_policy.github_attestations",
    )
    if policy != _expected_signing_policy(repository, git_revision):
        raise DescriptorError("descriptor weakens or changes the exact signing policy")

    _release_evidence(
        release_evidence_bytes,
        git_revision=git_revision,
        source_manifest_sha256=source_manifest_sha256,
        image_digests=image_digests,
    )


def render_descriptor(
    *,
    repository: str,
    git_revision: str,
    source_manifest_sha256: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    application_images: dict[str, str],
    release_evidence_bytes: bytes,
) -> dict[str, Any]:
    """Build a descriptor and immediately validate every cross-source binding."""

    repository = _repository(repository)
    git_revision = _revision(git_revision)
    source_manifest_sha256 = _sha256(
        source_manifest_sha256,
        label="source_manifest_sha256",
    )
    _positive_integer(workflow_run_id, label="workflow_run_id")
    _positive_integer(workflow_run_attempt, label="workflow_run_attempt")
    if set(application_images) != set(APPLICATION_COMPONENTS):
        raise DescriptorError("render requires the exact application image set")
    images = {**application_images, **PINNED_RUNTIME_IMAGES}
    descriptor: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "git_revision": git_revision,
        "source_manifest_sha256": source_manifest_sha256,
        "release_evidence": {
            "schema_version": RELEASE_EVIDENCE_SCHEMA_VERSION,
            "sha256": hashlib.sha256(release_evidence_bytes).hexdigest(),
        },
        "release_workflow": {
            "run_id": workflow_run_id,
            "run_attempt": workflow_run_attempt,
            "workflow_path": PROMOTION_WORKFLOW_PATH,
            "source_ref": PROMOTION_SOURCE_REF,
        },
        "images": images,
        "roles": dict(ROLE_COMPONENTS),
        "signing_policy": _expected_signing_policy(repository, git_revision),
    }
    _validate_descriptor_object(
        descriptor,
        release_evidence_bytes=release_evidence_bytes,
        expected_repository=repository,
        expected_git_revision=git_revision,
        expected_source_manifest_sha256=source_manifest_sha256,
        expected_workflow_run_id=workflow_run_id,
        expected_workflow_run_attempt=workflow_run_attempt,
    )
    _bounded_canonical_descriptor_bytes(descriptor)
    return descriptor


def verify_descriptor_bytes(
    data: bytes,
    *,
    release_evidence_bytes: bytes,
    expected_repository: str,
    expected_git_revision: str,
    expected_source_manifest_sha256: str,
    expected_workflow_run_id: int,
    expected_workflow_run_attempt: int,
) -> dict[str, Any]:
    """Strictly parse canonical bytes and verify them against trusted expectations."""

    descriptor = _parse_json(
        data,
        label="deploy descriptor",
        maximum_size=MAX_DESCRIPTOR_BYTES,
    )
    if data != _bounded_canonical_descriptor_bytes(descriptor):
        raise DescriptorError("deploy descriptor is not canonical JSON")
    _validate_descriptor_object(
        descriptor,
        release_evidence_bytes=release_evidence_bytes,
        expected_repository=expected_repository,
        expected_git_revision=expected_git_revision,
        expected_source_manifest_sha256=expected_source_manifest_sha256,
        expected_workflow_run_id=expected_workflow_run_id,
        expected_workflow_run_attempt=expected_workflow_run_attempt,
    )
    return descriptor


def compose_environment_bytes(descriptor: dict[str, Any]) -> bytes:
    """Render the non-secret application image inputs consumed by the overlay."""

    repository = _repository(descriptor.get("repository"))
    images = _exact_object(descriptor.get("images"), set(ALL_COMPONENTS), label="images")
    roles = _exact_object(descriptor.get("roles"), set(ROLE_COMPONENTS), label="roles")
    if roles != ROLE_COMPONENTS:
        raise DescriptorError("descriptor service roles do not match the reviewed mapping")
    lines: list[str] = []
    for component in APPLICATION_COMPONENTS:
        reference = images.get(component)
        name, _digest = _image_reference(reference, label=f"images.{component}")
        if name != application_image_name(repository, component):
            raise DescriptorError(f"images.{component} has another registry repository")
        variable = f"CUSTOMBUILD_DEPLOY_{component.upper()}_IMAGE"
        lines.append(f"{variable}={reference}")
    return ("\n".join(lines) + "\n").encode("ascii")


def registry_overlay_issues(path: Path) -> list[str]:
    """Return static contract drift without resolving deployment environment values."""

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ["compose.registry.yml is missing or unreadable"]
    if source != EXPECTED_REGISTRY_OVERLAY:
        return [
            "compose.registry.yml does not exactly bind descriptor images and --no-build"
        ]
    return []


def _read_bounded(path: Path, *, label: str, maximum_size: int) -> bytes:
    try:
        size = path.stat().st_size
        if size < 1 or size > maximum_size:
            raise DescriptorError(f"{label} is empty or exceeds its size limit")
        return path.read_bytes()
    except OSError as exc:
        raise DescriptorError(f"{label} is missing or unreadable") from exc


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _common_expectation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--git-revision", required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--workflow-run-attempt", type=int, required=True)
    parser.add_argument("--release-evidence", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render", help="render canonical descriptor bytes")
    _common_expectation_arguments(render)
    for component in APPLICATION_COMPONENTS:
        render.add_argument(f"--{component}-image", required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--sha256-output", type=Path)
    render.add_argument("--compose-env-output", type=Path)

    verify = subparsers.add_parser("verify", help="verify canonical descriptor bytes")
    _common_expectation_arguments(verify)
    verify.add_argument("descriptor", type=Path)
    verify.add_argument("--compose-env-output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        release_evidence_bytes = _read_bounded(
            args.release_evidence,
            label="release evidence",
            maximum_size=MAX_RELEASE_EVIDENCE_BYTES,
        )
        if args.command == "render":
            application_images = {
                component: str(getattr(args, f"{component}_image"))
                for component in APPLICATION_COMPONENTS
            }
            descriptor = render_descriptor(
                repository=args.repository,
                git_revision=args.git_revision,
                source_manifest_sha256=args.source_manifest_sha256,
                workflow_run_id=args.workflow_run_id,
                workflow_run_attempt=args.workflow_run_attempt,
                application_images=application_images,
                release_evidence_bytes=release_evidence_bytes,
            )
            canonical = canonical_json_bytes(descriptor)
            _write(args.output, canonical)
            digest = hashlib.sha256(canonical).hexdigest()
            if args.sha256_output is not None:
                _write(
                    args.sha256_output,
                    f"{digest}  {args.output.name}\n".encode("ascii"),
                )
        else:
            canonical = _read_bounded(
                args.descriptor,
                label="deploy descriptor",
                maximum_size=MAX_DESCRIPTOR_BYTES,
            )
            descriptor = verify_descriptor_bytes(
                canonical,
                release_evidence_bytes=release_evidence_bytes,
                expected_repository=args.repository,
                expected_git_revision=args.git_revision,
                expected_source_manifest_sha256=args.source_manifest_sha256,
                expected_workflow_run_id=args.workflow_run_id,
                expected_workflow_run_attempt=args.workflow_run_attempt,
            )
            digest = hashlib.sha256(canonical).hexdigest()
        if args.compose_env_output is not None:
            _write(args.compose_env_output, compose_environment_bytes(descriptor))
    except DescriptorError as exc:
        print(f"deploy descriptor rejected: {exc}", file=sys.stderr)
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
