import copy
import hashlib
import json
from pathlib import Path

import pytest

import scripts.deploy_descriptor as deploy_descriptor
from scripts.deploy_descriptor import (
    APPLICATION_COMPONENTS,
    EXPECTED_REGISTRY_OVERLAY,
    PINNED_RUNTIME_IMAGES,
    ROLE_COMPONENTS,
    DescriptorError,
    canonical_json_bytes,
    compose_environment_bytes,
    registry_overlay_issues,
    render_descriptor,
    verify_descriptor_bytes,
)

REPOSITORY = "pilotens/Custombuild"
REVISION = "a" * 40
SOURCE_MANIFEST = "b" * 64
RUN_ID = 123456
RUN_ATTEMPT = 2
APPLICATION_DIGESTS = {
    "api": f"sha256:{'1' * 64}",
    "worker": f"sha256:{'2' * 64}",
    "web": f"sha256:{'3' * 64}",
    "seaweedfs": f"sha256:{'4' * 64}",
}


def application_images() -> dict[str, str]:
    return {
        component: (
            f"ghcr.io/{REPOSITORY.lower()}-{component}@{APPLICATION_DIGESTS[component]}"
        )
        for component in APPLICATION_COMPONENTS
    }


def evidence_object() -> dict[str, object]:
    runtime_digests = dict(APPLICATION_DIGESTS)
    runtime_digests.update(
        {
            component: reference.split("@", 1)[1]
            for component, reference in PINNED_RUNTIME_IMAGES.items()
        }
    )
    return {
        "schema_version": "custombuild.release-readiness-evidence.v1",
        "git_revision": REVISION,
        "source_manifest_sha256": SOURCE_MANIFEST,
        "software_release_ready": True,
        "commercial_release_ready": False,
        "physical_machine_release_ready": False,
        "static_report": {
            "schema_version": "custombuild.release-readiness-static.v2",
            "static_controls_ready": True,
        },
        "runtime_manifest_digests": runtime_digests,
        "unrelated_final_evidence_is_preserved": {"restore": "passed"},
    }


def evidence_bytes(value: dict[str, object] | None = None) -> bytes:
    return (json.dumps(value or evidence_object(), sort_keys=True, indent=2) + "\n").encode()


def descriptor_for(evidence: bytes | None = None) -> dict[str, object]:
    return render_descriptor(
        repository=REPOSITORY,
        git_revision=REVISION,
        source_manifest_sha256=SOURCE_MANIFEST,
        workflow_run_id=RUN_ID,
        workflow_run_attempt=RUN_ATTEMPT,
        application_images=application_images(),
        release_evidence_bytes=evidence or evidence_bytes(),
    )


def verify(data: bytes, evidence: bytes | None = None) -> dict[str, object]:
    return verify_descriptor_bytes(
        data,
        release_evidence_bytes=evidence or evidence_bytes(),
        expected_repository=REPOSITORY,
        expected_git_revision=REVISION,
        expected_source_manifest_sha256=SOURCE_MANIFEST,
        expected_workflow_run_id=RUN_ID,
        expected_workflow_run_attempt=RUN_ATTEMPT,
    )


def set_path(value: dict[str, object], path: tuple[str, ...], replacement: object) -> None:
    owner: dict[str, object] = value
    for key in path[:-1]:
        child = owner[key]
        assert isinstance(child, dict)
        owner = child
    owner[path[-1]] = replacement


def test_descriptor_is_deterministic_canonical_and_cross_bound() -> None:
    evidence = evidence_bytes()
    descriptor = descriptor_for(evidence)
    canonical = canonical_json_bytes(descriptor)

    assert not canonical.endswith(b"\n")
    assert verify(canonical, evidence) == descriptor
    assert descriptor["roles"] == ROLE_COMPONENTS
    assert descriptor["release_evidence"] == {
        "schema_version": "custombuild.release-readiness-evidence.v1",
        "sha256": hashlib.sha256(evidence).hexdigest(),
    }
    assert descriptor["images"] == {**application_images(), **PINNED_RUNTIME_IMAGES}
    assert descriptor_for(evidence) == descriptor


def test_descriptor_is_independent_of_application_mapping_insertion_order() -> None:
    reversed_images = dict(reversed(application_images().items()))

    rendered = render_descriptor(
        repository=REPOSITORY,
        git_revision=REVISION,
        source_manifest_sha256=SOURCE_MANIFEST,
        workflow_run_id=RUN_ID,
        workflow_run_attempt=RUN_ATTEMPT,
        application_images=reversed_images,
        release_evidence_bytes=evidence_bytes(),
    )

    assert canonical_json_bytes(rendered) == canonical_json_bytes(descriptor_for())


def test_signing_policy_is_exact_and_commit_bound() -> None:
    policy = descriptor_for()["signing_policy"]

    assert policy == {
        "cosign": {
            "certificate_identity": (
                "https://github.com/pilotens/Custombuild/.github/workflows/"
                "cd.yml@refs/heads/main"
            ),
            "certificate_oidc_issuer": "https://token.actions.githubusercontent.com",
        },
        "github_attestations": {
            "repository": REPOSITORY,
            "signer_workflow": "pilotens/Custombuild/.github/workflows/cd.yml",
            "source_ref": "refs/heads/main",
            "source_digest": REVISION,
            "deny_self_hosted_runners": True,
        },
    }


def test_compose_environment_contains_only_verified_application_references() -> None:
    descriptor = descriptor_for()

    assert compose_environment_bytes(descriptor) == (
        f"CUSTOMBUILD_DEPLOY_API_IMAGE={application_images()['api']}\n"
        f"CUSTOMBUILD_DEPLOY_WORKER_IMAGE={application_images()['worker']}\n"
        f"CUSTOMBUILD_DEPLOY_WEB_IMAGE={application_images()['web']}\n"
        f"CUSTOMBUILD_DEPLOY_SEAWEEDFS_IMAGE={application_images()['seaweedfs']}\n"
    ).encode("ascii")


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        (("roles", "scheduler"), "api"),
        (("images", "api"), application_images()["worker"]),
    ),
)
def test_compose_environment_rejects_unverified_role_or_repository(
    path: tuple[str, ...],
    replacement: str,
) -> None:
    descriptor = copy.deepcopy(descriptor_for())
    set_path(descriptor, path, replacement)

    with pytest.raises(DescriptorError):
        compose_environment_bytes(descriptor)


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        (("schema_version",), "custombuild.deploy-descriptor.v2"),
        (("repository",), "another/Repository"),
        (("git_revision",), "c" * 40),
        (("source_manifest_sha256",), "c" * 64),
        (("release_evidence", "schema_version"), "other.evidence.v1"),
        (("release_evidence", "sha256"), "c" * 64),
        (("release_workflow", "run_id"), 123457),
        (("release_workflow", "run_id"), True),
        (("release_workflow", "run_id"), 9_223_372_036_854_775_808),
        (("release_workflow", "run_attempt"), 0),
        (("release_workflow", "workflow_path"), ".github/workflows/ci.yml"),
        (("release_workflow", "source_ref"), "refs/pull/7/merge"),
        (("images", "api"), "ghcr.io/pilotens/custombuild-api:latest"),
        (
            ("images", "api"),
            f"ghcr.io/pilotens/custombuild-worker@{APPLICATION_DIGESTS['api']}",
        ),
        (
            ("images", "api"),
            f"ghcr.io/pilotens/custombuild-api@SHA256:{'1' * 64}",
        ),
        (("images", "worker"), application_images()["api"]),
        (
            ("images", "postgres"),
            f"cgr.dev/chainguard/postgres@sha256:{'9' * 64}",
        ),
        (("roles", "scheduler"), "api"),
        (
            ("signing_policy", "cosign", "certificate_identity"),
            "https://github.com/pilotens/Custombuild/*@refs/heads/main",
        ),
        (
            ("signing_policy", "cosign", "certificate_oidc_issuer"),
            "https://example.invalid",
        ),
        (
            ("signing_policy", "github_attestations", "repository"),
            "another/Repository",
        ),
        (
            ("signing_policy", "github_attestations", "signer_workflow"),
            "pilotens/Custombuild/.github/workflows/ci.yml",
        ),
        (
            ("signing_policy", "github_attestations", "source_ref"),
            "refs/heads/feature",
        ),
        (
            ("signing_policy", "github_attestations", "source_digest"),
            "c" * 40,
        ),
        (
            ("signing_policy", "github_attestations", "deny_self_hosted_runners"),
            False,
        ),
    ),
)
def test_descriptor_mutations_fail_closed(
    path: tuple[str, ...],
    replacement: object,
) -> None:
    descriptor = copy.deepcopy(descriptor_for())
    set_path(descriptor, path, replacement)

    with pytest.raises(DescriptorError):
        verify(canonical_json_bytes(descriptor))


@pytest.mark.parametrize(
    ("path", "key"),
    (
        ((), "roles"),
        (("images",), "api"),
        (("roles",), "migrate"),
        (("signing_policy",), "cosign"),
        (("signing_policy", "github_attestations"), "source_digest"),
    ),
)
def test_descriptor_rejects_missing_fields(path: tuple[str, ...], key: str) -> None:
    descriptor = copy.deepcopy(descriptor_for())
    owner = descriptor
    for item in path:
        child = owner[item]
        assert isinstance(child, dict)
        owner = child
    del owner[key]

    with pytest.raises(DescriptorError):
        verify(canonical_json_bytes(descriptor))


@pytest.mark.parametrize(
    "path",
    ((), ("images",), ("roles",), ("signing_policy", "cosign")),
)
def test_descriptor_rejects_unknown_fields(path: tuple[str, ...]) -> None:
    descriptor = copy.deepcopy(descriptor_for())
    owner = descriptor
    for item in path:
        child = owner[item]
        assert isinstance(child, dict)
        owner = child
    owner["unexpected"] = "value"

    with pytest.raises(DescriptorError):
        verify(canonical_json_bytes(descriptor))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: b"\xef\xbb\xbf" + value,
        lambda value: value + b"\n",
        lambda value: b" " + value,
        lambda value: value.replace(b'"run_id":123456', b'"run_id":NaN'),
        lambda _value: b"\xff",
        lambda _value: b"",
    ),
)
def test_descriptor_rejects_noncanonical_or_invalid_bytes(mutation: object) -> None:
    canonical = canonical_json_bytes(descriptor_for())
    assert callable(mutation)

    with pytest.raises(DescriptorError):
        verify(mutation(canonical))


def test_descriptor_rejects_duplicate_json_keys() -> None:
    canonical = canonical_json_bytes(descriptor_for())
    duplicate = canonical.replace(
        b'"repository":"pilotens/Custombuild"',
        b'"repository":"pilotens/Custombuild","repository":"pilotens/Custombuild"',
    )
    assert duplicate != canonical

    with pytest.raises(DescriptorError, match="duplicate.*key"):
        verify(duplicate)


def test_descriptor_rejects_oversized_input() -> None:
    with pytest.raises(DescriptorError, match="size limit"):
        verify(b"{" + b" " * deploy_descriptor.MAX_DESCRIPTOR_BYTES + b"}")


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        (("schema_version",), "other.evidence.v1"),
        (("git_revision",), "c" * 40),
        (("source_manifest_sha256",), "c" * 64),
        (("software_release_ready",), False),
        (("commercial_release_ready",), True),
        (("physical_machine_release_ready",), True),
        (("static_report", "static_controls_ready"), False),
        (("runtime_manifest_digests", "api"), f"sha256:{'9' * 64}"),
    ),
)
def test_render_rejects_cross_source_or_unsafe_release_evidence(
    path: tuple[str, ...],
    replacement: object,
) -> None:
    evidence = evidence_object()
    set_path(evidence, path, replacement)

    with pytest.raises(DescriptorError):
        descriptor_for(evidence_bytes(evidence))


@pytest.mark.parametrize("component", ("api", "postgres"))
def test_render_requires_complete_release_evidence_image_set(component: str) -> None:
    evidence = evidence_object()
    manifests = evidence["runtime_manifest_digests"]
    assert isinstance(manifests, dict)
    del manifests[component]

    with pytest.raises(DescriptorError, match="runtime image set"):
        descriptor_for(evidence_bytes(evidence))


def test_changed_release_evidence_bytes_invalidate_an_existing_descriptor() -> None:
    original = evidence_bytes()
    canonical = canonical_json_bytes(descriptor_for(original))
    changed = original.replace(b'"restore": "passed"', b'"restore": "changed"')

    with pytest.raises(DescriptorError, match="digest does not match"):
        verify(canonical, changed)


def test_cli_renders_verifies_and_emits_only_digest_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release_evidence = tmp_path / "release-evidence.json"
    descriptor = tmp_path / "deploy-descriptor.json"
    descriptor_sha = tmp_path / "deploy-descriptor.sha256"
    environment = tmp_path / "deploy-images.env"
    release_evidence.write_bytes(evidence_bytes())
    image_arguments = [
        argument
        for component, reference in application_images().items()
        for argument in (f"--{component}-image", reference)
    ]
    common = [
        "--repository",
        REPOSITORY,
        "--git-revision",
        REVISION,
        "--source-manifest-sha256",
        SOURCE_MANIFEST,
        "--workflow-run-id",
        str(RUN_ID),
        "--workflow-run-attempt",
        str(RUN_ATTEMPT),
        "--release-evidence",
        str(release_evidence),
    ]
    monkeypatch.setattr(
        "sys.argv",
        [
            "deploy_descriptor.py",
            "render",
            *common,
            *image_arguments,
            "--output",
            str(descriptor),
            "--sha256-output",
            str(descriptor_sha),
        ],
    )

    assert deploy_descriptor.main() == 0
    digest = capsys.readouterr().out.strip()
    assert digest == hashlib.sha256(descriptor.read_bytes()).hexdigest()
    assert descriptor_sha.read_text(encoding="ascii") == (
        f"{digest}  {descriptor.name}\n"
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "deploy_descriptor.py",
            "verify",
            *common,
            str(descriptor),
            "--compose-env-output",
            str(environment),
        ],
    )

    assert deploy_descriptor.main() == 0
    assert capsys.readouterr().out.strip() == digest
    assert environment.read_bytes() == compose_environment_bytes(descriptor_for())


def test_cli_rejection_does_not_emit_compose_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release_evidence = tmp_path / "release-evidence.json"
    descriptor = tmp_path / "deploy-descriptor.json"
    environment = tmp_path / "deploy-images.env"
    release_evidence.write_bytes(evidence_bytes())
    descriptor.write_bytes(canonical_json_bytes(descriptor_for()) + b"\n")
    monkeypatch.setattr(
        "sys.argv",
        [
            "deploy_descriptor.py",
            "verify",
            "--repository",
            REPOSITORY,
            "--git-revision",
            REVISION,
            "--source-manifest-sha256",
            SOURCE_MANIFEST,
            "--workflow-run-id",
            str(RUN_ID),
            "--workflow-run-attempt",
            str(RUN_ATTEMPT),
            "--release-evidence",
            str(release_evidence),
            str(descriptor),
            "--compose-env-output",
            str(environment),
        ],
    )

    assert deploy_descriptor.main() == 1
    assert not environment.exists()
    assert "deploy descriptor rejected" in capsys.readouterr().err


def test_checked_in_registry_overlay_is_exact() -> None:
    assert registry_overlay_issues(Path("compose.registry.yml")) == []
    assert Path("compose.registry.yml").read_text(encoding="utf-8") == (
        EXPECTED_REGISTRY_OVERLAY
    )


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        ("required-up-flag: --no-build", "required-up-flag: --build"),
        ("sha256:3af67", "sha256:9af67"),
        ("CUSTOMBUILD_DEPLOY_API_IMAGE", "CUSTOMBUILD_DEPLOY_WEB_IMAGE"),
        ("services:\n", "services:\n  unexpected:\n    build: .\n"),
    ),
)
def test_registry_overlay_mutations_fail_closed(
    tmp_path: Path,
    original: str,
    replacement: str,
) -> None:
    overlay = tmp_path / "compose.registry.yml"
    overlay.write_text(EXPECTED_REGISTRY_OVERLAY.replace(original, replacement), encoding="utf-8")

    assert registry_overlay_issues(overlay)


def test_registry_overlay_must_exist() -> None:
    assert registry_overlay_issues(Path("does-not-exist.yml")) == [
        "compose.registry.yml is missing or unreadable"
    ]
