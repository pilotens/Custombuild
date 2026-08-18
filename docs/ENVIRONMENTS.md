# Production-candidate and test environments

The repository root is the canonical local production candidate.  The sibling
`DIY-test` working copy is the integration playground.  They are separate Git
working copies and must never share Compose identity, host ports, networks or
persistent volumes.

| Surface | Compose project | Web | API | Object storage | Purpose |
| --- | --- | ---: | ---: | ---: | --- |
| Production candidate | `custombuild-prod` | 3000 | 8000 | 9000 | Stable local acceptance candidate |
| Test | `custombuild-test` | 3100 | 8100 | 9200 | New changes and destructive test data |

The top-level `prod/` directory is an older self-contained release snapshot. It
is not a third runtime and must not be used as the source for current local
containers. Until its history has been archived, it remains read-only reference
material. New implementation belongs in the repository root and is promoted to
the sibling test copy through reviewed commits.

Before starting both local surfaces, run:

```bash
uv run python scripts/check_environment_isolation.py compose.yml --peer ../DIY-test/compose.yml
```

The check resolves the effective Compose configuration and fails if project
names, published ports or persistent volume names overlap. The root `make check`
also verifies its own environment identity.

The 2026-08-10 migration copied the former `custombuild_*` local volumes to the
project-scoped `custombuild-prod_*` volumes while both stacks were stopped. The
old volumes were deliberately retained as a rollback snapshot; current Compose
does not mount them.

Neither local surface is an Internet production deployment. `prod` here means a
stable production *candidate*. Internet production still requires the HTTPS,
OIDC, secret-management, backup, rate-limiting and observability controls in
`OPERATIONS.md` and `SECURITY.md`.
