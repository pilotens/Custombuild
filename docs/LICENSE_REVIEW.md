# Dependency licence review gate

This file records the engineering inventory made for the MVP. It is not legal
advice and it is not a commercial release approval. A release owner must review
the exact source, container images, distribution model, hosted-service model,
notices and source-offer obligations with qualified counsel before deployment.

## Application source licence

The application source is covered by the top-level proprietary `LICENSE` file.
Repository access is not a grant to copy, redistribute, host or commercially
deploy Custombuild. Third-party dependencies and runtime services remain subject
to their own licences and the release gates documented below.

## Pinned runtime services

| Component | Pinned image | Upstream licence | Engineering decision |
| --- | --- | --- | --- |
| PostgreSQL | `postgres:17.10-alpine` | PostgreSQL License | Permissive; retain copyright and licence notices. |
| Redis | `redis:7.2.15-alpine` | BSD-3-Clause | Redis states that 7.2.x and earlier remain BSD-3-Clause. Do not upgrade this image across the 7.4 or 8.x licence boundaries without a new review. |
| SeaweedFS | `chrislusf/seaweedfs:4.41` | Apache-2.0 | Permissive, actively maintained S3-compatible runtime; retain the Apache-2.0 licence and notices. The image is digest-pinned and vulnerability-scanned before release. |

Authoritative upstream references:

- [Redis licence history](https://redis.io/legal/licenses/)
- [SeaweedFS Apache-2.0 licence and S3 quick start](https://github.com/seaweedfs/seaweedfs)
- [PostgreSQL licence](https://www.postgresql.org/about/licence/)

The application talks to object storage through the S3 API. SeaweedFS replaced
the discontinued MinIO runtime without changing production-domain contracts;
the migration preserved and verified every existing object before cutover.

## Application dependency inventory

All direct Python, Node and browser dependencies are exact-version locked in
`uv.lock` and `pnpm-lock.yaml`. The current metadata inventory is predominantly
MIT, BSD, Apache-2.0, ISC and 0BSD. Items requiring explicit notice or a release
owner's attention include:

- CadQuery/OpenCascade packaging and its bundled native dependencies;
- `psycopg` (LGPL-3.0 for the library, with the binary distribution's bundled
  components inventoried separately);
- `@img/sharp-libvips-linux-x64` / libvips (LGPL-3.0-or-later);
- `caniuse-lite` data (CC-BY-4.0);
- CasADi (LGPL-3.0-or-later) pulled into the locked CAD dependency graph;
- `certifi` (MPL-2.0) and its bundled certificate-data notices;
- fonts, icons, generated PDFs and any future manufacturer data, tool libraries,
  machine adapters or uploaded reference material.

No dependency metadata scan can prove licence compliance. Missing or ambiguous
package metadata is a blocking review item, not an implicit approval.

## Reproducible review commands

Run from the repository root after the locked installs:

```bash
uv sync --locked --group dev --group cad
uv run python -c 'import importlib.metadata as m; print("\n".join(sorted("{}=={}: {}".format(d.metadata["Name"], d.version, d.metadata.get("License-Expression") or d.metadata.get("License") or "UNKNOWN") for d in m.distributions() if d.metadata.get("Name"))))'
pnpm install --frozen-lockfile
pnpm licenses list --prod --json
docker compose config --images
```

Archive the resulting inventories, the exact image digests, applicable licence
texts, notices and the counsel/release-owner decision with the release record.
Tag-only container references are convenient for local development; a commercial
deployment must pin reviewed image digests.

## Automated evidence and release checklist

`.github/workflows/supply-chain.yml` builds the three application images and
pulls the three pinned runtime images. It emits a uniquely named SPDX JSON SBOM
for every image and blocks fixed high/critical vulnerabilities. Every external
workflow action is pinned to an immutable commit, and release readiness rejects
floating action tags. The scheduled run also catches newly published
vulnerability data without waiting for a source change. These artifacts are
engineering evidence, not a legal approval.

Application base images are digest-pinned. Every application Dockerfile also
resolves operating-system updates and packages from an explicit dated Debian
snapshot whose metadata remains verified by Debian's archive keyring, and release
readiness rejects a return to a floating APT index. Update
that snapshot date only in a reviewed change that rebuilds all three images and
reruns their SBOM and vulnerability gates. This narrows rebuild drift; it does
not make a source revision itself a deployable immutable artifact. Production
must promote and record the exact built image digest and archive the associated
SBOM and scan evidence.

- [x] Generate SPDX SBOM artifacts for application and pinned runtime images in CI.
- [x] Scan image layers and language packages; block fixed high/critical findings.
- [ ] Archive the successful candidate run's SBOMs and vulnerability reports with the release.
- [ ] Resolve every `UNKNOWN`, custom, copyleft or source-available licence.
- [x] Replace the unmaintained AGPL MinIO runtime with digest-pinned Apache-2.0 SeaweedFS.
- [ ] Verify that Redis remains on an approved licence/version or approve a replacement.
- [ ] Assemble `NOTICE` and licence text bundles for distributed artifacts/images.
- [ ] Record source-offer, attribution and modification obligations where applicable.
- [ ] Review manufacturer/tool/machine datasets and generated-content rights.
- [ ] Obtain a dated written approval from the commercial release owner and counsel.

Dependency, image, data-source or deployment-model changes invalidate this review.
