# Joint-retention certifier handoff

This runbook is the clean-install path for external, signed
`load_bearing_carcass_dado` retention evidence. It joins one server-derived
certification request to an independent certifier's report, exact installation
instruction and operator-owned Ed25519 trust registry.

The workflow does **not** create a trusted key, certify a product, approve a CNC
program or authorize physical cutting. The signing key stays under the external
certifier's control. Custombuild only verifies the resulting statement and
derives a revision-bound software contract.

## Published contracts

| Document | Purpose |
| --- | --- |
| `packages/contracts/joint-retention-certification-request.v2.schema.json` | Strict shape of the server-derived request sent to the certifier |
| `packages/contracts/joint-retention-signing-payload.v2.schema.json` | Exact unsigned payload covered by the Ed25519 signature |
| `packages/contracts/joint-retention-signed-evidence.v2.schema.json` | Strict signed envelope and catalogue-entry shape |
| `packages/contracts/joint-retention-trust-registry.v1.schema.json` | Strict operator allow-list and revocation state |
| `scripts/joint_retention_certifier.py` | Prepare, validate, explicitly sign and verify canonical evidence |

All four schemas use JSON Schema Draft 2020-12 and reject unknown top-level and
nested fields. The CLI additionally rejects duplicate JSON keys, floating-point
numbers, unsafe identifiers, non-canonical signing bytes and request-binding
drift.

## Separation of duties

Keep these responsibilities separate in a real pilot:

1. The **designer** freezes the design and obtains its server-generated
   certification request.
2. The **independent certifier** tests the exact application and owns the signing
   key, report, instruction and catalogue claim.
3. The **platform operator** onboards only the certifier's public key and owns
   revocation state. The operator never receives the private key.
4. A **reviewer** uploads the signed statement. Upload is evidence intake, not
   approval.
5. The **designer** selects that immutable evidence for a new revision. A
   separate reviewer performs the normal design approval.

Do not combine these steps into a shared account or store a certifier private key
in this repository, a browser, an API/worker container, CI variables, logs or the
Custombuild trust registry.

## 1. Obtain the exact request

In the product, open **Serververifierad evidens** and choose **Hämta
certifieringsbegäran (.json)**. This is the preferred path because the browser
writes the exact server response without reconstructing any field.

For an API integration, save the current server-authoritative bookcase spec as
`bookcase-spec.json`, then extract the request directly from the authenticated
preview response:

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $CUSTOMBUILD_DESIGNER_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @bookcase-spec.json \
  "$CUSTOMBUILD_API/v1/designs/preview?project_id=$PROJECT_ID" \
  | jq -e '.retention_certification_request' \
  > certification-request.json
```

Preserve that object as UTF-8 JSON. It must have:

- `schema_version` equal to
  `custombuild.joint-retention-certification-request.v2`;
- `eligible_for_current_binding` equal to `true`;
- the expected source design hash, DADO geometry fingerprint, engine/template
  versions, materials with measured model thicknesses, canonical shear and
  withdrawal loads, and minimum safety factor;
- an explicit list of excluded applications.

Never hand-edit these values. Generate a fresh request after any design,
material, thickness, load, rule, template or engine change. Evidence for the
carcass DADOs does not cover an excluded surface-mounted back. Such a back stays
blocked until its own authenticated application is implemented.

## 2. Onboard the certifier public key

The certifier creates and protects an Ed25519 key under its approved key ceremony
or HSM procedure. Custombuild deliberately has no key-generation command. The
certifier supplies the platform operator with these independently verified
metadata:

- stable `issuer_id` and unique `key_id`;
- the **32 raw public-key bytes**, standard-base64 encoded;
- UTC `not_before` and `not_after` timestamps;
- documented key custodian and out-of-band revocation contact.

Create an operator-owned registry JSON object. The value below is only a shape
example: its public-key placeholder is intentionally invalid and cannot be
trusted or deployed.

```json
{
  "schema_version": "custombuild.joint-retention-trust-registry.v1",
  "issuers": [
    {
      "issuer_id": "independent-lab",
      "key_id": "retention-2026-01",
      "role": "joint_retention_certifier",
      "public_key_base64": "BASE64_OF_32_RAW_PUBLIC_KEY_BYTES",
      "not_before": "2026-09-01T00:00:00Z",
      "not_after": "2027-09-01T00:00:00Z",
      "revoked_at": null
    }
  ],
  "revoked_statement_sha256": [],
  "revoked_system_versions": []
}
```

Every public key must also be globally unique across the registry: never add the
same key material under another issuer or key ID. Sort `issuers` by
`(issuer_id, key_id)` and both revocation lists lexicographically. Validate the
completed file:

```bash
uv run python scripts/joint_retention_certifier.py validate-registry \
  --registry operator-registry.json
```

Review the returned registry SHA-256 out of band. The secret-manager value alone
is not production trust: migration `0018_joint_retention_registry_state` adds a
global database high-water mark that must be advanced by an explicit operator
transaction. API and worker can only assert an exact activated registry; neither
runtime can activate or automatically adopt one.

Stop API, scheduler and workers before the first activation, migrate the database
to the exact application head, and mount the already validated registry as a
process-owned mode-`0400` or `0600` regular file in the one-shot migrator
container. The change-ticket reference is non-secret and only its SHA-256 is
stored:

```bash
test "$(stat -c '%a:%u' "$JOINT_RETENTION_REGISTRY_FILE")" \
  = "600:$(id -u)"

docker compose --env-file artifacts/deploy-images.env \
  -f compose.yml -f compose.external-production.yml -f compose.registry.yml \
  run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  --volume "$JOINT_RETENTION_REGISTRY_FILE:/tmp/joint-retention-registry.json:ro" \
  migrate python -m scripts.activate_joint_retention_registry \
  --registry-file /tmp/joint-retention-registry.json \
  --operator-reference "$CHANGE_TICKET" \
  --confirm-activation
```

Inject `MIGRATION_DATABASE_URL` through the normal deployment secret environment;
never put its password on the command line. The CLI requires
`APP_ENV=production`, the exact `custombuild_migrator` role, the current Alembic
head, pinned search path and reviewed function/table privileges. It refuses
symlinks, relative paths, permission drift, duplicate JSON keys and schema drift.
The first successful run prints `activated` with epoch `1`; an exact replay is an
idempotent `unchanged` result. It never prints the database password, registry
contents or operator reference.

Only after activation, store those exact registry bytes in the deployment secret
manager as `JOINT_RETENTION_TRUST_REGISTRY_JSON` identically for API and worker,
deploy both roles from the `0018`-aware release and require production readiness.
A missing state, an empty/malformed registry, SQLite, or any canonical-byte/digest
mismatch fails closed. Non-production deliberately skips the persistence check
and therefore must never be cited as registry-activation evidence.

## 3. Prepare the unsigned statement

The certifier creates one strict catalogue-entry JSON file. It must describe a
mechanical DADO retention system with `machining_scope` set to
`no_additional_cnc`, sorted unique material/version identities, a thickness range
covering every requested thickness, and exactly two load cases ordered `shear`
then `withdrawal`. Each rated load and the safety factor must meet or exceed the
server request; verified capacities must meet or exceed each signed rated load
times that signed factor. Stronger certified values are accepted, while any
weaker request binding is rejected.

The report and installation instruction are separate immutable files. Their
exact bytes are embedded and checksum-bound; converting, re-saving or changing
metadata later creates different evidence.

```bash
uv run python scripts/joint_retention_certifier.py prepare \
  --request certification-request.json \
  --registry operator-registry.json \
  --catalogue-entry retention-catalogue-entry.json \
  --evidence-id certification-2026-001 \
  --issuer-id independent-lab \
  --key-id retention-2026-01 \
  --issued-at 2026-09-03T12:00:00Z \
  --expires-at 2027-03-03T12:00:00Z \
  --test-report independent-test-report.pdf \
  --test-report-id report-2026-001 \
  --test-report-version 1.0.0 \
  --installation-instruction installation-instruction.pdf \
  --installation-instruction-id instruction-2026-001 \
  --installation-instruction-version 1.0.0 \
  --output retention-unsigned.json
```

The output is a new mode-0600 file. The command refuses to overwrite an existing
file. Validate it again before the key ceremony:

```bash
uv run python scripts/joint_retention_certifier.py validate-payload \
  --request certification-request.json \
  --registry operator-registry.json \
  --payload retention-unsigned.json
```

## 4. Sign only after certifier review

The signature covers the exact unsigned UTF-8 bytes: JSON object keys sorted,
compact `,` and `:` separators, Unicode preserved and non-finite numbers
forbidden. The final evidence uses the same canonical serialization with
`signature_base64` added. Do not use a generic JSON formatter between signing
and upload.

The signing command accepts private-key material only from an explicitly named,
owner-owned, non-symlink PEM file with mode 0600 or stricter. An encrypted PEM
may use a similarly protected `--private-key-password-file`. The command derives
the public key and refuses to sign unless it matches the selected registry
issuer/key. It never prints key bytes.

```bash
uv run python scripts/joint_retention_certifier.py sign \
  --request certification-request.json \
  --registry operator-registry.json \
  --payload retention-unsigned.json \
  --private-key-file /secure/certifier/retention-ed25519.pem \
  --confirm-signing \
  --output retention-signed.json
```

Run the same resolver used by the API before handoff:

```bash
uv run python scripts/joint_retention_certifier.py verify \
  --request certification-request.json \
  --registry operator-registry.json \
  --evidence retention-signed.json
```

For a reproducible audit at a historical UTC instant, add `--at` with an ISO-8601
timestamp. Do not use a historical time to decide whether an upload or release
is currently valid.

## 5. Upload, bind and approve

The reviewer uploads `retention-signed.json` to the same project and design hash
using:

- evidence type `joint_retention`;
- rule ID `CB-JOINT-001`;
- catalogue ID/version exactly equal to the signed `system_id`/`system_version`;
- expiry exactly equal to the signed `expires_at` value.

The server reads the immutable stored bytes, verifies their checksum, signature,
issuer role and validity, statement/system/key revocation, exact
geometry/compiler/material/thickness/load applicability, document checksums and
upload metadata. The designer then binds the stored evidence ID when creating a
new revision. API and worker re-resolve it at validation, generation and release
boundaries, so expiry or later revocation fails closed.

Upload, a valid signature and a design approval are three distinct events. None
authorizes physical cutting.

The product UI performs the upload and evidence selection directly. An API
integration can perform the same reviewer-only upload without parsing or
rewriting the signed file:

```bash
SYSTEM_ID=$(jq -er '.catalogue_entry.system_id' retention-signed.json)
SYSTEM_VERSION=$(jq -er '.catalogue_entry.system_version' retention-signed.json)
EXPIRES_AT=$(jq -er '.expires_at' retention-signed.json)
DESIGN_HASH=$(jq -er '.source_design_hash' certification-request.json)

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $CUSTOMBUILD_REVIEWER_TOKEN" \
  -F 'document=@retention-signed.json;type=application/json' \
  -F evidence_type=joint_retention \
  -F rule_id=CB-JOINT-001 \
  -F "catalog_id=$SYSTEM_ID" \
  -F "catalog_version=$SYSTEM_VERSION" \
  -F "design_hash=$DESIGN_HASH" \
  -F "expires_at=$EXPIRES_AT" \
  "$CUSTOMBUILD_API/v1/projects/$PROJECT_ID/evidence" \
  > uploaded-retention.json

EVIDENCE_ID=$(jq -er '.id' uploaded-retention.json)
```

The designer then creates a new revision from the unmodified spec, exact
workshop production context and returned evidence ID. First request the bound
preview, then construct `version-request.json` without string placeholders.
`production-context.json` must be the exact validated context captured by the
product and `CURRENT_REVISION` must come from the current project response:

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $CUSTOMBUILD_DESIGNER_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @bookcase-spec.json \
  "$CUSTOMBUILD_API/v1/designs/preview?project_id=$PROJECT_ID&joint_retention_evidence_id=$EVIDENCE_ID" \
  > bound-preview.json

BOUND_HASH=$(jq -er '.design_hash' bound-preview.json)
jq -n \
  --slurpfile spec bookcase-spec.json \
  --slurpfile context production-context.json \
  --arg design_hash "$BOUND_HASH" \
  --arg evidence_id "$EVIDENCE_ID" \
  --argjson current_revision "$CURRENT_REVISION" \
  '{template_id:"shelving",spec:$spec[0],production_context:$context[0],expected_design_hash:$design_hash,expected_current_revision:$current_revision,joint_retention_evidence_id:$evidence_id}' \
  > version-request.json
```

Post it with the designer token, validate that revision, and have a distinct
reviewer approve it:

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $CUSTOMBUILD_DESIGNER_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @version-request.json \
  "$CUSTOMBUILD_API/v1/projects/$PROJECT_ID/versions" \
  > bound-version.json

REVISION=$(jq -er '.revision' bound-version.json)
curl --fail-with-body --silent --show-error -X POST \
  -H "Authorization: Bearer $CUSTOMBUILD_DESIGNER_TOKEN" \
  "$CUSTOMBUILD_API/v1/projects/$PROJECT_ID/versions/$REVISION/validate"

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $CUSTOMBUILD_REVIEWER_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"approval_type":"design","reason":"Independent design and retention review complete","warning_overrides":[]}' \
  "$CUSTOMBUILD_API/v1/projects/$PROJECT_ID/versions/$REVISION/approve"
```

Do not reuse the designer token for the reviewer calls in a four-eyes production
deployment. Server responses remain authoritative if a shown example ever
differs from the current OpenAPI contract.

## Revocation runbook

Apply the narrowest sufficient revocation and preserve previous registry bytes
as audit evidence:

- add the lowercase SHA-256 of the **entire canonical signed evidence file** to
  `revoked_statement_sha256` for one bad statement;
- add `system_id@system_version` to `revoked_system_versions` for a withdrawn
  retention catalogue version;
- set the issuer's `revoked_at` timestamp for a compromised or withdrawn key.

Sort the changed lists, validate the new registry and obtain the operator change
approval. First block new generation/release work, then run the activation CLI
against the new exact file. Activation holds an exclusive transaction lock while
runtime assertions use the matching shared lock. Existing issuer identities and
validity/public-key fields cannot change; `revoked_at` may move only from `null`
to a timestamp or from an existing timestamp to an earlier instant. It can never
be cleared or moved later. Statement and system revocation sets may only grow;
new issuer keys may be appended only with previously unseen public-key material.
Key rotation therefore adds a new `(issuer_id, key_id)` with a new key and revokes
the old key without deleting or aliasing it.

After activation, update API and worker with the same exact bytes and roll both
roles. A still-running `0018` process with the old registry immediately fails its
next lifecycle assertion; any older pre-`0018` process is prohibited during this
rollout. Confirm readiness, then confirm that a new `verify` or lifecycle
operation rejects the affected evidence. Never delete or rewrite revoked state to
make previously signed evidence pass.

Readiness additionally requires at least one certifier key that is valid and not
yet revoked at the current instant. It only proves that a synchronized trust
policy can authenticate evidence. The certifier, laboratory work and signed
evidence for each concrete design remain external requirements; readiness alone
never authorizes machining or claims that such evidence exists.

## Sharp-pilot readiness checklist

- [ ] Independent certifier role, competence, scope and key custody are approved.
- [ ] Public key fingerprint and registry metadata were verified out of band.
- [ ] API and worker have the same reviewed registry content.
- [ ] The exact registry SHA-256 is activated at the database high-water epoch.
- [ ] Certification request is current, eligible and preserved with its source design.
- [ ] Physical tests used the exact joint geometry, materials, thickness range and loads.
- [ ] Report and installation instruction identify specimens, hardware, method and results.
- [ ] Toolkit `verify` passes at the current time with the deployment registry.
- [ ] Reviewer uploaded the exact signed bytes; designer bound them to a new revision.
- [ ] Independent design review passed after binding; generation revalidation passed.
- [ ] Revocation contact and emergency registry-deployment procedure were exercised.
- [ ] Separate machine/tool/material/coupon, air-cut, reference-part and prototype gates pass.

Even when every item above passes, the machine-neutral package and reference
LinuxCNC output remain validation/review artifacts with
`physical_cutting_authorized: false`. A named workshop must independently
validate the actual machine, tool, stock batch, fixture, program and first
article before any sharp CNC run.
