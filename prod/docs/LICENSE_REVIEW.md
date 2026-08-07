# Dependency licence review gate

This file records the engineering inventory made for the MVP. It is not legal
advice and it is not a commercial release approval. A release owner must review
the exact source, container images, distribution model, hosted-service model,
notices and source-offer obligations with qualified counsel before deployment.

## Application source licence

This repository currently has no top-level `LICENSE` file. Repository access
must not be interpreted as a grant to copy, redistribute or commercially deploy
the Custombuild application source. The repository owner must choose and add an
application licence before any third-party distribution; dependency licences do
not license the application itself.

## Pinned runtime services

| Component | Pinned image | Upstream licence | Engineering decision |
| --- | --- | --- | --- |
| PostgreSQL | `postgres:17.5-alpine` | PostgreSQL License | Permissive; retain copyright and licence notices. |
| Redis | `redis:7.2.5-alpine` | BSD-3-Clause | Redis states that 7.2.x and earlier remain BSD-3-Clause. Do not upgrade this image across the 7.4 or 8.x licence boundaries without a new review. |
| MinIO | `minio/minio:RELEASE.2025-04-22T22-12-26Z` | AGPL-3.0 | **Commercial release gate.** Decide whether the deployment and distribution model complies with AGPL-3.0, obtain an appropriate commercial licence, or replace MinIO with another S3-compatible service. |

Authoritative upstream references:

- [Redis licence history](https://redis.io/legal/licenses/)
- [MinIO AGPL-3.0 announcement](https://www.min.io/blog/from-open-source-to-free-and-open-source-minio-is-now-fully-licensed-under-gnu-agplv3)
- [PostgreSQL licence](https://www.postgresql.org/about/licence/)

The application talks to object storage through the S3 API, so the local MinIO
service can be replaced without changing production-domain contracts.

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

## Release checklist

- [ ] Generate and archive CycloneDX or SPDX SBOMs for Python, Node and all images.
- [ ] Scan image layers as well as package-manager dependency graphs.
- [ ] Resolve every `UNKNOWN`, custom, copyleft or source-available licence.
- [ ] Decide the MinIO deployment/licensing strategy.
- [ ] Verify that Redis remains on an approved licence/version or approve a replacement.
- [ ] Assemble `NOTICE` and licence text bundles for distributed artifacts/images.
- [ ] Record source-offer, attribution and modification obligations where applicable.
- [ ] Review manufacturer/tool/machine datasets and generated-content rights.
- [ ] Obtain a dated written approval from the commercial release owner and counsel.

Dependency, image, data-source or deployment-model changes invalidate this review.
