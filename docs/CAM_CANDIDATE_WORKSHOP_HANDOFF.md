# Executable CAM candidate: operator and workshop handoff

## Scope and safety boundary

Custombuild can compile the supported shelving design-review package into a
deterministic LinuxCNC CAM-candidate sidecar. The sidecar contains real cutting
motion and is machine-executable, but every manifest, report and program keeps
`physical_cutting_authorized=false` and
`workshop_acceptance_required=true`. Compilation is therefore not permission to
start a spindle or cut stock.

There is deliberately no built-in production profile or production example with
invented machine facts. A named workshop must own, measure and accept the exact
controller, travel, WCS offsets, fixture, spoilboard, tool assembly and material
recipes in its profile.

## Author the production profile

The complete closed syntax contract is
`packages/contracts/production-machine-profile.v1.schema.json` for the finalized
document. An `init` draft deliberately does not satisfy that schema while its
sentinels remain. JSON Schema
covers required keys, primitive types, constants, enums and local ranges. Hash
bindings, canonical bytes, ordering, design-review provenance and geometric CAM
constraints are semantic rules and must also pass the authoring CLI; schema
validation alone is never sufficient.

Start from the exact checksum-verified design-review ZIP that will be compiled:

```bash
uv run python -m scripts.production_machine_profile init \
  --design-review intake/design-review.zip \
  --output work/production-machine-profile.draft.json
```

`init` strictly verifies the whole review archive before it emits anything. The
draft is explicitly `deployable=false`. It copies only immutable facts that are
derivable from that archive: design and source-machine identities, source setup
and tool hashes, source stock geometry, source material identities, source
keep-outs and the exact recipe coverage that must be supplied. Every actual
machine, controller version, WCS, fixture, spoilboard, production material,
material evidence, T/H row, tool assembly, cutting recipe, acceptance record and
controller/VFD attestation remains an explicit
`{"$unresolved":"WORKSHOP_INPUT_REQUIRED"}` value. Required policy constants
and operation-kind defaults are contract values, not claims about a machine.
Even when production v1 permits only zero, the actual G5x rotation, H-row X/Y
offsets and cutter point length remain unresolved until the workshop explicitly
records those zero values.
`IDENTITY_STOCK_XY_TO_WCS_XY`, `STOCK_TOP_Z0` and `raw_allowance_um=0` are
versioned restrictions of the supported 3-axis CAM v1 implementation, not
measured workshop facts. A setup that differs needs a newly profiled and
validated implementation; editing those constants is rejected.

Edit the draft with measured and independently accepted shop facts. The
suffixes are units: `_um` is integer micrometres, `_um_min` micrometres per
minute, `_mdeg` millidegrees, `_ppm` parts per million, `_rpm` revolutions per
minute and `_ms` milliseconds. Every evidence ID/version/hash trio must name one
retained immutable workshop record; its `_sha256` is the lowercase SHA-256 of
that record's exact bytes. The CLI binds the declared digest but cannot establish
the truth of the external record.

The
`source_material_id`/`source_material_version` fields retain the validation
catalogue provenance and may therefore contain a source label such as
`screening-*`. They are not production-stock claims. Each setup's separate
`material_id`/`material_version` and
`material_evidence_id`/`material_evidence_version`/`material_evidence_sha256`
identify the actual workshop-owned stock; recipes bind to that actual identity.
Do not copy the source material identity into those fields unless the workshop's
evidence independently establishes that exact identity as its accepted stock.
Each `requirements.recipe_bindings[].recipe_index` identifies the initial recipe
slot for one physical-sheet/source-tool/operation-kind need. Because `init`
cannot know the workshop mapping yet, consolidate slots that resolve to the same
actual material/tool/kind binding, or retain separate slots when those bindings
differ. `finalize` computes the exact required binding set from the completed
setups and tools and reports `RECIPE_COVERAGE_MISMATCH` if a recipe is missing or
extraneous.

Finalize only after every unresolved value has been replaced:

```bash
uv run python -m scripts.production_machine_profile finalize \
  --design-review intake/design-review.zip \
  --draft work/production-machine-profile.draft.json \
  --output /protected/custombuild/production-cam-profile.json
```

`finalize` rejects changed source requirements, missing/unknown keys and every
remaining unresolved value with stable JSON-pointer diagnostics. It computes the
nested postprocessor config SHA-256 first, then the canonical payload SHA-256,
strictly validates the completed profile against the review operations, and
creates the final compact UTF-8 file exclusively with mode `0600`. It never
overwrites an existing file. Its receipt prints `document_sha256`; use that exact
value as `PRODUCTION_CAM_PROFILE_SHA256`.

The receiving or deployment operator can repeat the complete check without
writing any file:

```bash
uv run python -m scripts.production_machine_profile validate \
  --design-review intake/design-review.zip \
  --profile /protected/custombuild/production-cam-profile.json
```

Success means the profile is structurally and semantically ready to generate a
CAM candidate for that exact review ZIP. The receipt still states
`physical_cutting_authorized=false`; it is not permission to start a spindle.

## Production profile deployment

The authoritative input is one canonical UTF-8 JSON file. Its root has exactly
these keys:

- `schema_version`: `custombuild.production-machine-profile.v1`
- `payload`: the closed production payload described below
- `payload_sha256`: lowercase SHA-256 of the canonical `payload` bytes

Canonical JSON uses sorted object keys, compact separators, exact integers and
no trailing newline. Unknown or duplicate keys, floating-point values and an
incorrect payload digest are rejected. Production accepts only
`profile_class=SERVER_OWNED_PRODUCTION`,
`acceptance.status=WORKSHOP_ACCEPTED` and a non-placeholder acceptance evidence
identity/hash. `TEST_ONLY` requires an explicit test-harness opt-in and is
rejected by normal production readers.

`payload` contains exactly:

| Key | Bound facts |
| --- | --- |
| `profile_class` | Server-owned production or explicitly test-only |
| `acceptance` | Status plus evidence ID, version and SHA-256 |
| `machine` | Source validation profile; actual machine/controller identity and six absolute axis bounds; spindle/feed limits; tool/recipe catalogue versions; postprocessor-profile binding |
| `postprocessor_profile` | Exact native metric XYZ identity-trivkins/joint topology; raw LinuxCNC G5x offsets and zero XY rotation; complete G49/G53 tool-change path; explicit G92.1 reset, homing, disabled-override, spindle-at-speed and full-restart policies; controller/VFD evidence and attestations |
| `setups` | Exact source-setup and source-material provenance; separately evidenced actual workshop material; stock/sheet/side, identity XY transform, WCS with raw signed controller G5x X/Y/Z and zero rotation, safe/fixture clearance, keep-outs, probe method, and conditional spoilboard allowance |
| `tools` | Source-tool hash mapped to a separately identified actual tool/version, controller T/H numbers, expected signed H-row X/Y/Z values plus one atomic tool-table evidence identity/hash, exact flat-bottom geometry with zero drill-point length, effective diameter, cutting length, measured stick-out, holder clearance and collision radius |
| `recipes` | Exact machine/actual-workshop-material/tool/operation binding with RPM, feeds, depth/step-over, entry, tolerance/accuracy budget, overtravel and tabs |

The normative closed-key parser is
`custombuild_manufacturing.production_machine_profile`; its mutation tests cover
every nested object. The LinuxCNC subprofile additionally requires:

- the fixed
  `LINEAR_UNITS_MM_COORDINATES_XYZ_IDENTITY_TRIVKINS_JOINTS_3_JOINT_0_X_JOINT_1_Y_JOINT_2_Z_NO_EXTRA_AXES`
  policy, retained evidence ID/version/SHA-256, and explicit verification of all
  six facts: native `[TRAJ] LINEAR_UNITS=mm`, exactly
  `[TRAJ] COORDINATES=XYZ`, identity `trivkins`, exactly `[KINS] JOINTS=3`,
  the mapping `0:X`, `1:Y`, `2:Z`, and no additional controlled axes. A fourth
  joint, a gantry with duplicate joints, an extra rotary/linear axis,
  non-identity kinematics, or inch-native controller data is outside production
  v1 and is rejected rather than normalized or inferred;
- sorted `supported_wcs` and an exact raw controller `wcs_offsets` row with
  X/Y/Z and zero XY rotation for each code;
- an unconditional, controller-attested `G92.1_CLEAR_AND_DO_NOT_RESTORE`
  policy, so no G52/G92 local offset survives into candidate motion;
- a controller-attested `P0` policy that disables LinuxCNC external X/Y/Z
  axis offsets; the program header, manifest, index and setup instructions bind
  the evidence and the verifier rejects any other policy;
- the complete verified
  `G53_Z_TOOLCHANGE_XY_M6_G53_Z_THEN_ENTRY_XY_AT_GLOBAL_CLEARANCE` path;
- verified G8 radius mode, M6 behavior and axis preservation, G43/H offsets,
  G97 RPM mode, coolant-off M9, clockwise M3 and G4/P seconds semantics; the
  dwell is only a minimum delay and is never accepted as proof that the spindle
  reached speed;
- M49 for disabled feed/spindle overrides throughout the program, M52 P0 for
  disabled adaptive feed and M53 P1 for an available controlled feed hold;
- program-start-only execution: Run From Selected Line is disabled and an abort
  requires the preflight and full program to restart from its beginning. A
  controlled feed-hold pause/resume is distinct from an abort;
- the `M6_PRESERVES_EXACT_BOUND_TOOL_TABLE` policy and controller evidence:
  an M6 remap or automatic probing routine must not change the preflight-bound H
  row before G43 applies it;
- the independent `M6_PRESERVES_EXACT_BOUND_WCS_TABLE` policy and controller
  evidence: an M6 remap or automatic probing routine must not change any bound
  raw G5x X/Y/Z/R row between preflight and the post-M6 WCS selection;
- `NO_FORCE_HOMING=0` with X, Y and Z homed before AUTO; the candidate never
  performs homing and requires a separately evidenced controller interlock;
- real, nonzero spindle RPM feedback within the profile's bounded tolerance
  before feed motion, plus VFD-fault interlocks that inhibit motion and stop the
  spindle, and a separate continuously active interlock that inhibits cutting
  feed whenever actual RPM leaves that tolerance;
- an H-aware absolute tool-change Z at or above every setup's transformed
  safe-Z plane, with the complete transformed cutting envelope inside the six
  machine bounds;
- identical actual-machine identity and absolute travel in the execution and
  postprocessor profiles.

The profile's `machine_wcs_origin` and `machine_wcs_z0_um` values are the raw,
signed LinuxCNC G5x offset values, not measured G53 tool-tip origins. With G52
and G92 cleared, XY rotation fixed to zero, and the exact metric XYZ identity
kinematics contract active, the asserted G53 controlled-point endpoint on each
axis is:

```text
machine_axis_um = programmed_wcs_axis_um + g5x_axis_offset_um + expected_h_axis_offset_um
```

LinuxCNC adds the signed tool-table offset when deriving absolute position;
therefore a negative H Z value lowers the absolute endpoint. Production v1
requires expected H X/Y to be zero and retains the signed expected H Z value
from the exact controller tool-table snapshot. Both the controller `T` number
and the `H` number are restricted to LinuxCNC's positive signed-32-bit range
`1..2147483647`. `G43 Hn` resolves the tool-table row whose `T` value is `n`;
it does not resolve by the row's `P` pocket and does not implicitly use the
currently loaded tool. The selected `T` number and the `H` number may therefore
be different, but both named `T` rows must exist in the accepted table. The
tool number, H row,
all three expected values and the shared tool-table evidence hash must agree in
the profile, program header, program index and setup instructions. G53 blocks run
with G49 in raw machine coordinates. The canonical safety-state block includes
G8, G49, M9, M49, M52 P0, M53 P1 and G92.1. It runs before G53 Z and the G53
tool-change XY traverse, then M6 runs. Because M6 may be remapped, the complete
safety-state block runs again before G53 Z, G53 program-entry XY, G43 H, WCS and
setup safe Z. The exact bound H row and every bound raw G5x X/Y/Z/R row must
survive M6/remap unchanged. The candidate manifest, program index, setup
instructions and program header bind the separate tool-table, WCS-table and
metric-XYZ-identity-kinematics policies, evidence hashes and verified
capabilities. The trailer cancels G43
with G49 before its final G53 Z retract.

Every emitted NGC identity header also carries
`SOURCE_MATERIAL_ID`/`SOURCE_MATERIAL_VERSION`,
`ACTUAL_MATERIAL_ID`/`ACTUAL_MATERIAL_VERSION` and the exact
`MATERIAL_EVIDENCE_ID`/`MATERIAL_EVIDENCE_VERSION`/`MATERIAL_EVIDENCE_SHA256`.
The production parser reconstructs those lines from the bound toolpath setup and
rejects any mismatch; the operator can therefore compare the machine file with
the retained material record without treating source screening material as the
physical stock.

All NGC is ASCII with LF line endings and at most 252 bytes before each LF.
Both profile validation and candidate compilation run the exact LinuxCNC
postprocessor and parser, so an overlong header or command fails before a
candidate can be declared generation-ready. The 252-byte boundary and the
signed `M = P + G5x + H` transform are checked against the real LinuxCNC `rs274`
interpreter in the pinned CI oracle. With `rs274` installed, repeat that check
locally with `make linuxcnc-interpreter-oracle`; 252 bytes must parse and 253
must fail.

The design-review validation profile may describe a source reference cutter as
a brad-point bit. That description is not production geometry evidence and is
never inherited by the mapped shop tool. Production v1 independently requires
the accepted actual tool to be an exact flat-bottom cutter with
`drill_point_length_um=0`; any pointed geometry is rejected. Through drilling
remains blocked even with that mapping.

For external production, set `PRODUCTION_CAM_PROFILE_HOST_PATH` to the protected
regular file and set `PRODUCTION_CAM_PROFILE_SHA256` to the lowercase SHA-256 of
that file's exact bytes. The Compose overlay mounts the same file read-only into
the API and generation worker at
`/run/custombuild-config/production-cam-profile.json`. Inline JSON is forbidden
in production, and Compose does not create a missing host path. Both services
receive the same required deployment pin. They
open the leaf with `O_NOFOLLOW`, require a bounded regular file and compare
device, inode, mode, size, mtime and ctime from `fstat` before and after reading
the same descriptor, then compare the SHA-256 of those exact bytes with the pin
on every read. A missing or mismatched pin and a same-size in-place or torn update
therefore fail closed. Deploy API and worker together when rotating the file and
pin; their job binding includes the exact `document_sha256` and must match before
generation starts.

## Offline compile and verification

The offline path is useful for a controlled workshop intake or simulation:

First obtain the producer build identity from the authenticated deployment/build
record. It is a canonical JSON object with exactly `schema_version`,
`app_version`, `vcs_ref`, `source_manifest_sha256` and
`dependency_lock_sha256`. The schema is
`custombuild.producer-build-identity.v1`. For a source-tree invocation, the
compiler independently recomputes `SOURCE_MANIFEST_SHA256` over the exact local
build inputs and the SHA-256 of `uv.lock`; both must equal the supplied document.
This code-root identity is deliberately named `SOURCE_MANIFEST_SHA256`: it is
not claimed to be a container-image digest or a signature.

```bash
uv run python -m scripts.compile_cam_candidate \
  --design-review artifacts/design-review.zip \
  --production-profile /protected/custombuild/production-cam-profile.json \
  --producer-build-identity /protected/custombuild/producer-build-identity.json \
  --output artifacts/cam-candidate.zip
```

The compiler opens both inputs without following symlinks, enforces size limits,
fully verifies the immutable design-review archive, loads its embedded operations,
generates toolpaths and LinuxCNC programs, runs the independent source-to-removal
verifier, builds the sidecar and strictly reads the completed sidecar again. It
creates the output exclusively and will not overwrite an existing file. Its JSON
receipt records the base/profile/toolpath/candidate hashes, program count and the
unchanged safety boundary. It also records the complete closed
`software_provenance` object and its canonical SHA-256. That object binds the
producer build, source-manifest code root, dependency lock, toolpath engine and
schema, independent verifier/backplot, LinuxCNC postprocessor/parser/safety
validator, and candidate manifest/package-builder versions.
The verifier resolves the complete implementation object through an explicit,
source-controlled support registry and verification-dispatch ID. Current
approval and release require the registry entry for the code presently
executing. Historical intake may use only a frozen entry that remains in that
registry with a real compatible verification dispatch; an arbitrary declared
version is never accepted. Retain the matching verifier release/source root
with an archived candidate. A later incompatible or retired implementation set
fails closed rather than silently reinterpreting old machine code.

`--allow-test-only` exists only for CI and simulation fixtures. A production
operator must never pass it. Only an actually `TEST_ONLY` profile under that
explicit flag may omit the identity document; the resulting manifest embeds a
conspicuous deterministic `TEST_ONLY_UNATTESTED_BUILD` identity that normal
production readers reject.

After transfer, the receiving workshop verifies the candidate independently:

```bash
uv run python -m scripts.verify_cam_candidate \
  --candidate intake/cam-candidate.zip \
  --design-review intake/design-review.zip \
  --expect-candidate-sha256 <immutable-job-or-release-sha256> \
  --expect-producer-source-manifest-sha256 <authenticated-producer-code-root> \
  --expect-verifier-source-manifest-sha256 <authenticated-verifier-code-root>
```

The expected digest must come from the immutable job/release record or another
authenticated out-of-band channel, not from the transferred ZIP or a checksum
file delivered beside it. The producer code root must likewise come from the
authenticated job/deploy receipt, not from the candidate. The verifier code
root must come from the authenticated deployment of the receiving verifier; the
CLI recomputes its local source manifest and rejects a mismatch. The verifier
follows no input symlinks, writes no files, checks the transfer digest before parsing, then strictly checks the base
binding, canonical ZIP envelope, complete inventory, every payload hash,
production-profile receipt, regenerated programs, source-to-removal report and
backplot. Its JSON receipt remains non-authorizing. `--allow-test-only` is only
for CI/simulation and is rejected by the normal production invocation.

The sidecar's authoritative inventory is `manifest.json.artifacts`. It includes:

- the embedded source operations and trusted validation-machine profile;
- production toolpaths and the independent cutting-program report;
- a deterministic SVG backplot;
- the accepted production profile and LinuxCNC subprofile;
- setup instructions and a dense program index;
- ordered `*.production.ngc` programs.

Before any workshop action, verify the candidate against the exact base
design-review ZIP and compare the reported SHA-256 to the immutable job/release
record. Follow the program index's `execution_order`; do not infer order from a
file browser. For each program, verify setup ID, stock/sheet/side; raw signed G5x
X/Y/Z and zero rotation; fixture/keep-outs/spoilboard; actual T/H identity and
the live tool-table row against its expected X/Y/Z values and evidence hash;
zero drill-point length and exact flat-bottom actual-tool identity; immutable
source-material identity; exact actual material/lot and its evidence hash; RPM,
feed and actual-material recipe. A release-contour program is terminal for its
physical sheet, so no later program may target that sheet.

The manifest, program index, setup instructions, toolpath document and cutting
report independently expose and bind the reviewed source material to the
workshop's actual material/lot and evidence ID, version and SHA-256. A/B setups
for one physical sheet must carry the same seven material facts. Repacking any
surface with a substituted source material, lot or evidence hash fails the
canonical candidate rebuild.

## Implemented candidate boundary

The API accepts only the explicit `include_cutting_candidate` opt-in, binds the
canonical server-owned profile receipt into the queued job and the worker rejects
profile drift before generation. The worker passes the exact independently
resolved production engine context, checks it byte-for-byte against the review
bundle, and projects all four producer-build fields into the candidate. It builds and persists the sidecar and
its public evidence; the API rereads the candidate against the exact immutable
design-review ZIP and checks the manifest, dense program inventory, object sizes
and hashes before exposing downloads. The offline compiler and independent
receiving-workshop verifier provide the same fail-closed package boundary without
turning it into a machine-start instruction.

The generated preamble establishes G8 radius mode, G97, coolant off, M49-disabled
feed/spindle overrides, disabled adaptive feed, enabled feed hold, cleared
G52/G92 state and the complete G49/G53/M6 return path before applying the exact
G43 H row. The profile must contain workshop evidence for those controller
semantics, `NO_FORCE_HOMING=0`/all-XYZ-homed gating, program-start-only execution
with Run From Selected Line disabled and full restart after abort, real nonzero
spindle feedback within tolerance and VFD-fault motion/spindle interlocks. G4
supplies only a minimum spin-up dwell. M6/remap must preserve both the exact
preflight-bound H XYZ snapshot until G43 applies it and the exact raw G5x
X/Y/Z/R rows until the post-M6 WCS selection. These commands and attestations
prevent silent assumptions in generated code; they do not observe a real
controller, tool table, WCS table, spindle or fixture at run time.

`ProductionWorkflow` now exposes the candidate as a separate, non-authorizing
opt-in while preserving the design-review boundary. Its strict client contract:

- accepts only the paired result
  `machine_program_mode=EXECUTABLE_CAM_CANDIDATE` and
  `production_machine_program=true` when the nested candidate status, hashes and
  `physical_cutting_authorized=false` also agree;
- sends `include_cutting_candidate` only as an explicit opt-in, false by default;
- allowlists `cam_candidate_bundle`, `cutting_toolpaths`,
  `machine_program_index`, dense `machine_program_###`,
  `cutting_program_validation_report`, `cutting_backplot` and
  `production_machine_profile`, while rejecting gaps, duplicates and unknown
  executable artifacts;
- checks every published artifact's content type, size and hash binding and uses
  the checksum-verified download path for the ZIP, each NGC,
  report and index; render the verified SVG backplot through an object URL in an
  image element, never as unsanitized inline markup;
- labels the result “Körbar CAM-kandidat” and “Ej kapningsgodkänd av Custombuild”;
  do not expose a “start”, “release for cutting” or equivalent action;
- fails closed for forged JSON claims, mismatched artifact hashes, non-dense
  programs, wrong content types and rejected download bytes. Component and full
  functional suites, TypeScript checking, lint and the production web build cover
  this path.

The existing design/CAM maker-checker review binds two distinct reviewers to
the exact generated result. A review-only release remains `design_review` with
`machine_use=validation_only`. An immutable CAM release is instead returned as
`release_kind=executable_cam` with
`machine_use=executable_cam_candidate`; both variants retain
`physical_cutting_authorized=false` and neither starts the staged physical
workshop chain.

## Remaining trust path before physical cutting

The repository already has strict v2 models and append-only tables for workshop
runs, policies, setup evidence, signed staged attestations and revocations. The
public workshop endpoint still has a deliberate 409-only `NoReturn` path, so
those physical trust models are not connected to the verified candidate. The
remaining work is ordered as follows:

1. Create an immutable workshop-run identity from the already persisted and
   reread candidate, copying the exact ZIP, manifest, source operations, setup
   instructions, program index and every program hash/size and binding it to the
   design-review release and generation job. Keep the preparation endpoint
   blocked for every mismatch.
2. If production uses prebuilt containers rather than the audited source-tree
   verifier, additionally bind and authenticate the actual image digest. The
   implemented `SOURCE_MANIFEST_SHA256` code root must not be relabelled as a
   container digest.
3. Expose the staged workshop workflow over the existing append-only models:
   distinct qualified maker/checker principals, nonce/signature verification,
   revocation and automatic staleness after candidate, profile or policy drift.
4. Run a separately versioned LinuxCNC simulator/removal comparison and retain
   its binary/config hashes, expected/observed removal hashes and deviation.
5. On the named physical machine, verify all-XYZ homing with `NO_FORCE_HOMING=0`,
   the reviewed source-material mapping and exact actual material/lot evidence,
   the raw G5x XYZ/R rows, live H-row XYZ values and atomic tool-table hash, tool
   runout/stick-out, separately identified flat-bottom geometry with zero point
   length, fixture and keep-outs, stock-top probing, M49-disabled overrides,
   M6/remap preservation of the bound H row and every bound raw G5x XYZ/R row,
   Run From Selected Line lockout and full-restart-after-abort behavior, real
   nonzero RPM feedback within tolerance and VFD-fault motion/spindle
   inhibition; then perform a supervised air cut.
6. Cut and measure a coupon for every production material batch, then the
   designated reference part. Record every nominal, limit and observed value.
7. Complete the first-article assembly/load test and retain maker/checker staged
   attestations. Only the workshop's separate physical-release procedure may
   permit machine start; Custombuild's candidate must remain non-authorizing.

Until those review and physical trials pass with real workshop evidence, the
software can be rated complete for deterministic candidate generation, but the
end-to-end physical workshop release cannot honestly be rated 10/10.
