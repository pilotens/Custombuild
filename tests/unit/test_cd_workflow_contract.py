import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(".github/workflows/cd.yml")
SHA_PIN = re.compile(r"^[^@\s]+@[a-f0-9]{40}$")

EXPECTED_ACTIONS = {
    "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6",
    "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
    "actions/download-artifact@634f93cb2916e3fdff6788551b99b062d0335ce0",
    "actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38",
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610",
    "anchore/scan-action@e49c028b8f5d4ac63b87309b024ea6faceb6bac3",
    "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9",
    "docker/build-push-action@d08e5c354a6adb9ed34480a06d141179aa583294",
    "docker/login-action@b45d80f862d83dbcd57f89517bcf500b2ab88fb2",
    "docker/setup-buildx-action@d7f5e7f509e45cec5c76c4d5afdd7de93d0b3df5",
    "pnpm/action-setup@0977fd99725f1db4007ccb2928dbb4e90d06cc86",
    "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6",
}


def workflow_source() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(workflow_source())
    assert isinstance(loaded, dict)
    loaded["on"] = loaded.pop(True)
    return loaded


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    value = job.get("steps")
    assert isinstance(value, list)
    assert all(isinstance(item, dict) for item in value)
    return value


def test_cd_runs_read_only_pr_evidence_but_privileged_jobs_are_main_push_only() -> None:
    loaded = workflow()

    assert loaded["on"] == {
        "push": {"branches": ["main"]},
        "pull_request": {"branches": ["main"]},
    }
    assert loaded["permissions"] == {}
    assert not re.search(r"^\s*workflow_run\s*:", workflow_source(), re.MULTILINE)
    assert "pull_request_target" not in loaded["on"]
    assert "workflow_dispatch" not in loaded["on"]

    jobs = loaded["jobs"]
    assert set(jobs) == {
        "quality-evidence",
        "build-images",
        "test-release-bundle",
        "publish-images",
        "deployment-descriptor",
    }
    for job in jobs.values():
        assert job["runs-on"] == "ubuntu-24.04"
    for name in ("quality-evidence", "build-images", "test-release-bundle"):
        condition = jobs[name]["if"]
        assert "github.event_name == 'pull_request'" in condition
        assert "github.base_ref == 'main'" in condition
        assert "github.event_name == 'push'" in condition
        assert "github.ref == 'refs/heads/main'" in condition
        assert "github.repository == 'pilotens/Custombuild'" in condition
    for name in ("publish-images", "deployment-descriptor"):
        condition = jobs[name]["if"]
        assert "github.event_name == 'push'" in condition
        assert "github.ref == 'refs/heads/main'" in condition
        assert "github.repository == 'pilotens/Custombuild'" in condition
        assert "pull_request" not in condition


def test_cd_jobs_have_exact_least_privilege_and_dependency_boundaries() -> None:
    jobs = workflow()["jobs"]

    read_only = {"contents": "read"}
    assert jobs["quality-evidence"]["permissions"] == read_only
    assert jobs["build-images"]["permissions"] == read_only
    assert jobs["test-release-bundle"]["permissions"] == read_only
    for name in ("quality-evidence", "build-images", "test-release-bundle"):
        assert "secrets." not in str(jobs[name])
    assert jobs["deployment-descriptor"]["permissions"] == read_only
    assert jobs["publish-images"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert jobs["test-release-bundle"]["needs"] == [
        "quality-evidence",
        "build-images",
    ]
    assert jobs["publish-images"]["needs"] == "test-release-bundle"
    assert jobs["deployment-descriptor"]["needs"] == "publish-images"


def test_every_action_is_immutable_and_the_reviewed_actions_are_exact() -> None:
    uses = {
        str(step["uses"])
        for job in workflow()["jobs"].values()
        for step in steps(job)
        if "uses" in step
    }

    assert uses == EXPECTED_ACTIONS
    assert all(SHA_PIN.fullmatch(action) for action in uses)
    checkouts = [
        step
        for job in workflow()["jobs"].values()
        for step in steps(job)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 5
    assert all(
        step["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False}
        for step in checkouts
    )
    scan_actions = [
        step
        for job in workflow()["jobs"].values()
        for step in steps(job)
        if str(step.get("uses", "")).startswith("anchore/scan-action@")
    ]
    assert len(scan_actions) == 4
    assert all(step["with"]["grype-version"] == "v0.110.0" for step in scan_actions)
    assert "e1165082ffb1fe366ebaf02d8526e7c4989ea9d2" not in workflow_source()
    assert "anchore/grype/main/install.sh" not in workflow_source()


def test_images_are_built_once_archived_tested_then_pushed_without_rebuild() -> None:
    loaded = workflow()
    jobs = loaded["jobs"]
    source = workflow_source()

    build = jobs["build-images"]
    assert build["strategy"]["matrix"]["include"] == [
        {
            "component": "api",
            "dockerfile": "services/api/Dockerfile",
            "lock_argument": "DEPENDENCY_LOCK_SHA256",
            "lock_file": "uv.lock",
        },
        {
            "component": "worker",
            "dockerfile": "services/worker/Dockerfile",
            "lock_argument": "DEPENDENCY_LOCK_SHA256",
            "lock_file": "uv.lock",
        },
        {
            "component": "web",
            "dockerfile": "apps/web/Dockerfile",
            "lock_argument": "FRONTEND_LOCK_SHA256",
            "lock_file": "pnpm-lock.yaml",
        },
        {
            "component": "seaweedfs",
            "dockerfile": "infra/seaweedfs/Dockerfile",
        },
    ]
    build_steps = steps(build)
    build_actions = [
        step
        for step in build_steps
        if step.get("uses", "").startswith("docker/build-push-action@")
    ]
    assert len(build_actions) == 1
    assert build_actions[0]["with"]["load"] is True
    assert build_actions[0]["with"]["push"] is False
    assert build_actions[0]["with"]["platforms"] == "linux/amd64"
    assert build_actions[0]["with"]["build-args"] == (
        "${{ steps.identity.outputs.build_args }}"
    )
    assert "if [ '${{ matrix.component }}' != seaweedfs ]; then" in source

    built = source.index("- name: Build candidate exactly once")
    output_root = source.index("mkdir -p artifacts/release-images")
    sbom = source.index("- name: Generate exact image SPDX SBOM")
    scanned = source.index("- name: Block all image High and Critical vulnerabilities")
    archived = source.index("- name: Save the exact tested image object")
    runtime = source.index("- name: Start only the archived image objects")
    live = source.index("- name: Same-SHA live design-review")
    pushed = source.index("- name: Push the exact tested object without rebuilding")
    assert output_root < built < sbom < scanned < archived < runtime < live < pushed
    assert "docker compose --file compose.yml up --no-build" in source
    assert 'sha256sum "image-${component}.tar" > "image-${component}.tar.sha256"' in source
    assert source.count(
        '(cd artifacts/release-images && sha256sum --check "image-${component}.tar.sha256")'
    ) == 2
    assert 'test "$registry_config_digest" = "$EXPECTED_CONFIG_DIGEST"' in source
    assert "EXPECTED_DIGEST" not in source
    assert 'test "$registry_digest" =' not in source
    assert "custombuild.release-image-archive.v2" in source
    assert "image_config_digest" in source
    assert "local_scan_manifest_digest" in source
    assert source.count(
        "test \"$(jq -er '.local_scan_manifest_digest' \"$metadata\")\" ="
    ) == 2
    assert "registry:redis:7.2.15-alpine@sha256:" in source
    assert "resolve_registry_platform()" in source
    assert 'platform.architecture == "amd64"' in source
    assert 'test "$platform_digest" = "$(scan_value "$component"' in source

    publish_runs = "\n".join(
        str(step.get("run", "")) for step in steps(jobs["publish-images"])
    )
    assert "docker image push" in publish_runs
    assert "docker build " not in publish_runs
    assert "docker buildx build" not in publish_runs
    resolved = publish_runs.index('registry_digest="$(docker buildx imagetools inspect')
    exact = publish_runs.index('exact_reference="${SUBJECT_NAME}@${registry_digest}"')
    inspected = publish_runs.index(
        'docker buildx imagetools inspect "$exact_reference" --raw'
    )
    config_checked = publish_runs.index(
        'test "$registry_config_digest" = "$EXPECTED_CONFIG_DIGEST"'
    )
    assert resolved < exact < inspected < config_checked
    assert 'imagetools inspect "$tagged_reference" --raw' not in publish_runs


def test_exact_loaded_web_image_proves_fail_closed_production_runtime() -> None:
    job_steps = steps(workflow()["jobs"]["test-release-bundle"])
    named_steps = {step.get("name"): step for step in job_steps}
    smoke = named_steps["Prove exact loaded web image production runtime boundary"]
    run = str(smoke["run"])
    step_names = [step.get("name") for step in job_steps]

    assert step_names.index("Verify and load the exact archived images") < step_names.index(
        "Prove exact loaded web image production runtime boundary"
    ) < step_names.index("Start only the archived image objects")
    assert 'image="custombuild-web:${GITHUB_SHA}"' in run
    assert run.count('"$image" > /dev/null') == 2
    assert "--env APP_ENV=production" in run
    assert '--env "APP_ENV=${app_env}"' in run
    assert "custombuild-web:ci" not in run

    positive, negative = run.split(
        "for invalid_case in app-env api-url demo-token oidc-issuer "
        "oidc-client-id oidc-redirect-uri",
        maxsplit=1,
    )
    for expected in (
        "--env CUSTOMBUILD_WEB_API_URL=https://api.cd.invalid",
        "--env CUSTOMBUILD_WEB_DEMO_TOKEN=",
        "--env CUSTOMBUILD_WEB_OIDC_ISSUER=https://identity.cd.invalid/",
        "--env CUSTOMBUILD_WEB_OIDC_CLIENT_ID=custombuild-web",
        "--env CUSTOMBUILD_WEB_OIDC_REDIRECT_URI=https://app.cd.invalid/",
        "csp_header=",
        "grep --fixed-strings 'https://api.cd.invalid' <<< \"$csp_header\"",
        "grep --fixed-strings 'https://identity.cd.invalid' <<< \"$csp_header\"",
        "'Logga in'",
        "'Lokalt konstruktionsläge.'",
        "'demo-nordic-owner'",
        'if [ "$positive_health" != healthy ]',
    ):
        assert expected in positive
    for expected in (
        "app-env) app_env=",
        "api-url) api_url=",
        "demo-token) demo_token=demo-nordic-owner",
        "oidc-issuer) oidc_issuer=",
        "oidc-client-id) oidc_client_id=",
        "oidc-redirect-uri) oidc_redirect_uri=",
        '--env "CUSTOMBUILD_WEB_API_URL=${api_url}"',
        '--env "CUSTOMBUILD_WEB_DEMO_TOKEN=${demo_token}"',
        '--env "CUSTOMBUILD_WEB_OIDC_ISSUER=${oidc_issuer}"',
        '--env "CUSTOMBUILD_WEB_OIDC_CLIENT_ID=${oidc_client_id}"',
        '--env "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI=${oidc_redirect_uri}"',
        'case "$negative_status" in',
        "2??|000)",
        'if [ "$negative_health" != unhealthy ]',
    ):
        assert expected in negative
    assert run.count("--format '{{.Image}}'") == 2
    assert run.count('= "$expected_image_id"') == 2


def test_same_sha_browser_wcag_live_restore_and_release_evidence_are_required() -> None:
    source = workflow_source()

    required = (
        "Same-SHA WCAG 2.2 AA production-build acceptance",
        "Same-SHA Chromium Firefox and WebKit acceptance",
        "Same-SHA browser acceptance against exact images",
        "scripts/live_acceptance.py",
        "scripts/compose_backup.py",
        "scripts/restore_drill.py",
        "scripts/release_evidence_gate.py",
        "static-release-readiness.json",
        "runtime-evidence.json",
        "release-readiness.json",
        "release-live-browser-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}",
        "test \"$(jq -er '.software_release_ready' \"$report\")\" = true",
        "test \"$(jq -er '.commercial_release_ready' \"$report\")\" = false",
        "test \"$(jq -er '.physical_machine_release_ready' \"$report\")\" = false",
    )
    for value in required:
        assert value in source


def test_artifact_common_roots_reconstruct_the_paths_consumed_downstream() -> None:
    source = workflow_source()

    assert "path: artifacts/release-images\n          merge-multiple: true" in source
    assert (
        "name: release-evidence-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}\n"
        "          path: artifacts"
    ) in source
    assert 'evidence="artifacts/release-evidence/release-readiness.json"' in source
    assert "path: artifacts/published\n          merge-multiple: true" in source
    assert 'metadata="artifacts/published/image-${component}.json"' in source


def test_publication_uses_exact_keyless_identity_and_github_attestation() -> None:
    source = workflow_source()

    for value in (
        "cosign-release: v3.0.6",
        "cosign sign --yes \"$EXACT_REFERENCE\"",
        "https://github.com/${GITHUB_REPOSITORY}/.github/workflows/cd.yml@refs/heads/main",
        "https://token.actions.githubusercontent.com",
        "subject-name: ${{ steps.published.outputs.subject_name }}",
        "subject-digest: ${{ steps.published.outputs.digest }}",
        "push-to-registry: true",
        "create-storage-record: false",
        "gh attestation verify \"oci://${EXACT_REFERENCE}\"",
        "--bundle-from-oci",
        "--signer-workflow \"$GITHUB_REPOSITORY/.github/workflows/cd.yml\"",
        "--cert-oidc-issuer https://token.actions.githubusercontent.com",
        "--source-ref refs/heads/main",
        "--source-digest \"$GITHUB_SHA\"",
        "--deny-self-hosted-runners",
    ):
        assert value in source


def test_descriptor_is_canonical_digest_only_and_stops_before_deployment() -> None:
    source = workflow_source()
    descriptor_job = workflow()["jobs"]["deployment-descriptor"]
    descriptor_runs = "\n".join(
        str(step.get("run", "")) for step in steps(descriptor_job)
    )

    assert "scripts/deploy_descriptor.py render" in descriptor_runs
    assert "scripts/deploy_descriptor.py verify" in descriptor_runs
    assert "scripts/deploy_descriptor.py publication" in workflow_source()
    assert descriptor_runs.count("--publication-directory artifacts/published") == 2
    assert "custombuild.published-image.v1" not in workflow_source()
    assert "compose.registry.yml config --images" in descriptor_runs
    assert "grep -c '@sha256:'" in descriptor_runs
    assert "Upload the verified promotion input without deploying" in source
    forbidden = (
        "environment:",
        "kubectl ",
        "helm ",
        "ssh ",
        "docker compose up",
    )
    for value in forbidden:
        assert value not in descriptor_runs
