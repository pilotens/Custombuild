## Change and risk

Describe the behavior changed, affected contracts and migration/rollback implications.

## Evidence

- [ ] Focused regression tests added and run (record commands/results).
- [ ] Same-commit CI, production-candidate and release-evidence checks are green.
- [ ] All runtime images pass the high/critical vulnerability gate without new ignores.
- [ ] OpenAPI/TypeScript contracts and production semantic-root record are current.
- [ ] Tenant isolation, immutable artifacts and physical-cutting blockers are preserved.

## Human review

Name the reviewer and link their actual review; do not pre-fill an approval.
Production-affecting geometry, rules, CAM, trust or release changes require an
independent qualified reviewer. A passing build is not workshop acceptance or
authorization to run a machine.
