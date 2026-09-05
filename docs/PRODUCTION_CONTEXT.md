# Production context and reproducibility

Every generation job freezes a canonical `ProductionEngineContext`. The API
resolves the selected machine profile and validation postprocessor before it
queues work, stores the complete context on the job, and includes it in the
generation-context SHA-256 and idempotency key. A worker independently resolves
the current context and requires byte-canonical equality before it builds a
design or writes an object. Drift is a blocking failure; an existing job or
artifact is never silently reused.

## Covered implementation identity

The context includes explicit versions for:

- application, domain engine, template, rules and joint-support catalogue;
- domain-to-manufacturing adapter, DFM, nesting and semantic operations;
- operations, manifest, artifact and application-document schemas;
- artifact exporters, production pipeline and deterministic ZIP builder;
- CAD adapter and kernel contract plus pinned CadQuery and OpenCascade
  distributions;
- CAM validation and backplot;
- the selected machine profile and its full canonical fingerprint;
- the tool library and its full canonical fingerprint;
- the selected postprocessor, G-code parser and safety validator;
- the version-locked PDF renderer dependency.

Version constants live beside the implementations they identify. Production-
affecting behavior changes must bump the owning constant. Catalogue content is
also hashed in full, so changing a travel limit, WCS, keep-out zone, tool
diameter, measured diameter, runout, cutting length, RPM or feed changes the
context even if a catalogue maintainer forgets to bump its label.

## Frozen outputs

The same context and generation hash are embedded in `manifest.json` and covered
by the manifest integrity hash. `operations.json` freezes the exact tools selected
by the emitted operations, including tool version, nominal and measured diameter,
runout, cutting length, spindle RPM, feed and plunge. `cam/tool-list.csv` is
generated only from that snapshot. CAM validation rejects fingerprint mismatch,
unknown or unused snapshot tools, and any setup tool list that differs from its
operations.

The worker verifies context equality and the full generation hash before artifact
construction, then verifies that the bundle manifest contains the exact frozen
values before any object-store write. Artifact listing, download, CAM approval
and release also re-resolve the context and return a conflict for stale jobs.

## Migration and deployment rule

The package contract introduced with this release is
`custombuild.production-manifest.v5` with
`custombuild.supplier-handoff.v3`. The current reader deliberately rejects
manifest v4 and supplier-handoff v2 instead of interpreting old bytes under
the stronger v5/v3 rules. Historical bytes are never rewritten or relabelled,
but the current release-archive endpoints intentionally reject v4 packages
because their ownership check uses the current v5 reader. Preserve and retrieve
v4 packages through the archived v4 runtime or an independently controlled
byte archive, and verify them with the exact archived v4 verifier that created
them. Regenerating a design creates a new revision and a new v5 archive rather
than upgrading an existing ZIP in place.

This break is required because v5 adds the canonical package guide and three
published JSON Schemas, while handoff v3 binds the operations document and its
schema by path, version and SHA-256. Those required files and fields were not
part of the v4/v2 contract and must never be inferred for a historical archive.

Migration `0002_generation_engine_context` adds the non-null JSON snapshot to
generation jobs. Existing pre-context jobs are intentionally non-reproducible:
their empty snapshot fails the current-context guard and they must be regenerated.

Deploy application code, worker code and migrations as one coordinated release.
Do not let old and new workers consume the same queue across an implementation
version change. The current topology replaces the former `celery` default queue with
explicit `generation` and `maintenance` queues. For that cutover, pause API and beat,
drain the legacy queue completely with the old image, stop every old worker, migrate,
then start the new generation worker, singleton maintenance worker and singleton beat
as one release. Never overlap the old and new topology or abandon legacy messages:
their outbox rows may already be marked dispatched.
