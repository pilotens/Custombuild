# Supply-chain remediation checkpoint — 2026-09-25

This is a development checkpoint, not release approval. The High/Critical gate,
empty `.grype.yaml` ignore list, and empty vulnerability-exception ledger remain
unchanged. All final application images must still be built and scanned by Linux CI.

## Updated immutable inputs

- Postgres remains PostgreSQL 18.6; its Wolfi revision moves from `r0` to `r3`.
- Redis moves from `7.2.15-alpine` to `7.2.16-alpine`, retaining Redis 7.2.
- The BusyBox helper, application glibc base and Python build assembler use newly
  resolved immutable digests. Compose, backup tooling, deployment descriptors,
  release-evidence validation and all CI/CD scan inputs use matching references.
- Application runtime packages update glibc to `2.44-r6`, zlib to
  `1.3.2.1_rc20260601-r0`, and Python to the distribution's
  `3.13.15_git20260918-r2` security snapshot. The Python interpreter remains 3.13.15;
  Node remains 24.19.0. These are distribution snapshots, not a Python minor upgrade.
- Exact transitive package counts and closure hashes remain enforced. Actual APK
  installation in isolated Linux chroots produced 19 web, 24 API and 49 worker
  packages. Python, SSL/zlib imports and Node version checks ran successfully there.
  This does not test the complete application virtualenv, CAD imports or containers.

The source package recipes are [Wolfi zlib](https://github.com/wolfi-dev/os/blob/main/zlib.yaml)
and [Wolfi Python 3.13](https://github.com/wolfi-dev/os/blob/main/python-3.13.yaml).
The exact approved candidate versions and digests are in the Dockerfiles; mutable
upstream recipe links alone are not release evidence.

## Scan evidence and remaining blockers

Direct registry scans used Grype **0.110.0**, `linux/amd64`, `--fail-on high`, the
repository configuration, and database built **2026-09-25T06:31:49Z**. The database
archive SHA-256 was
`04c01bc3418d0d53f64d041c2f28070a5ce1f814ee891b3f5d90102f09232dfe`.

| Candidate | High/Critical findings | Status |
| --- | ---: | --- |
| Postgres `f275a6af…` | 0 | Candidate image scan passed |
| BusyBox `b2953ab1…` | 0 | Candidate image scan passed |
| glibc base `6acf5a19…` | 0 | Base-only scan passed |
| Redis `29e8589c…` | 1 | **Release blocked** |

Redis still reports **CVE-2026-85091** on Alpine `zlib 1.3.2-r0`, with no fixed
version reported in that scan. The refreshed image removes the previously observed
OpenSSL findings, but is not vulnerability-clean. Do not suppress this finding or
promote the release while the gate is red.

The application image scans remain unverified. A Windows scan of the worker's
Linux chroot could not recognize its distribution through the WSL filesystem and
reported generic NVD matches, including CVE-2026-19499 and CVE-2026-82049. It is not
equivalent to scanning the final Linux image and does not establish those findings
as fixed or false positives. CI must resolve their status against the real SBOM.

Docker/Compose execution was unavailable on the review machine. Required follow-up:
build the final API/worker/web images, run their strict scans and runtime import
probes, run live Compose/backup/restore acceptance, and obtain qualified human review.
No physical machining authorization follows from this checkpoint.
