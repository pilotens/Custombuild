from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(".github/workflows/cd-attestation-recovery.yml")


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_recovery_is_direct_main_push_only() -> None:
    text = workflow_text()
    assert "push:" in text
    assert "branches: [main]" in text
    assert "workflow_run:" not in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "ref: ${{ github.sha }}" in text
    assert "actions/workflows/cd.yml/runs?event=push&head_sha=${GITHUB_SHA}" in text


def test_recovery_requires_original_acceptance_signing_and_attestation_success() -> None:
    text = workflow_text()
    for required in (
        'test-release-bundle" and .conclusion == "success"',
        "Push the exact tested object without rebuilding",
        "Sign the exact digest with GitHub OIDC",
        "Create the GitHub artifact attestation",
        "Verify exact signer and attestation policy",
    ):
        assert required in text
    assert 'and (.name | startswith("publish-images (") | not)' in text


def test_recovery_never_builds_or_pushes_an_image() -> None:
    text = workflow_text()
    forbidden = (
        "docker build ",
        "docker image build",
        "docker buildx build",
        "docker image push",
        "docker push",
        "build-push-action",
        "packages: write",
    )
    for token in forbidden:
        assert token not in text
    assert "packages: read" in text


def test_recovery_reverifies_exact_source_run_and_retains_diagnostics() -> None:
    text = workflow_text()
    assert "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6" in text
    assert "cosign-release: v3.0.6" in text
    assert "cosign verify" in text
    assert "--bundle-from-oci" in text
    assert '--source-digest "$SOURCE_SHA"' in text
    assert "--source-ref refs/heads/main" in text
    assert "--deny-self-hosted-runners" in text
    assert '--workflow-run-id "$SOURCE_RUN_ID"' in text
    assert '--workflow-run-attempt "$SOURCE_RUN_ATTEMPT"' in text
    assert "github-attestation-${component}.stderr" in text
    assert "physical_machine_release_ready" in text
    assert "= false" in text


def test_recovery_only_emits_digest_pinned_no_build_promotion_input() -> None:
    text = workflow_text()
    assert "scripts/deploy_descriptor.py render" in text
    assert "scripts/deploy_descriptor.py verify" in text
    assert "grep -c '@sha256:'" in text
    assert "compose.registry.yml config --images" in text
    assert "docker compose" in text
    assert " up " not in text
