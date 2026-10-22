import { describe, expect, it } from "vitest";
import { resolveDesign } from "@/lib/design-engine";
import { designPlanning } from "@/lib/design-planning";
import { DEFAULT_DESIGN_SPEC, type DesignSpec, type RuleEvaluation } from "@/lib/design-types";

function evaluation(ruleId: string, patch: Partial<RuleEvaluation> = {}): RuleEvaluation {
  return {
    rule_id: ruleId,
    rule_version: "1.0.0",
    status: "WARNING",
    title: "Kontroll",
    summary: "Underlag saknas.",
    calculation: "saknas",
    assumptions: [],
    affected_part_ids: [],
    ...patch,
  };
}

const wallLibrary: DesignSpec = {
  ...DEFAULT_DESIGN_SPEC,
  furniture_type: "wall_library",
  width_mm: 2_400,
  height_mm: 2_400,
  depth_mm: 340,
  divider_count: 3,
  base_cabinet_count: 4,
  base_cabinet_height_mm: 700,
  base_cabinet_depth_mm: 340,
};

describe("designPlanning", () => {
  it.each([
    [1_199, 300, false, false],
    [1_200, 300, false, true],
    [1_201, 300, false, true],
    [1_799, 600, false, false],
    [1_800, 600, false, true],
    [1_801, 600, false, true],
    [800, 400, true, true],
  ])("plans anchoring at %i × %i mm with explicit requirement %s", (
    heightMm,
    depthMm,
    explicitRequirement,
    expected,
  ) => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      height_mm: heightMm,
      depth_mm: depthMm,
      wall_anchor_required: explicitRequirement,
    };

    expect(designPlanning(spec, [], "local").wallAnchoringRequired).toBe(expected);
    expect(designPlanning(spec, [], "server-preview").wallAnchoringRequired).toBe(expected);
    expect(spec.wall_anchor_verified).toBe(false);
    expect(spec.wall_anchor_required).toBe(explicitRequirement);
  });

  it("keeps the anchoring requirement even when an existing verification is present", () => {
    const spec = { ...DEFAULT_DESIGN_SPEC, wall_anchor_verified: true };

    expect(designPlanning(spec, [], "local").wallAnchoringRequired).toBe(true);
    expect(spec.wall_anchor_verified).toBe(true);
  });

  it("identifies the actual unresolved local wall-library capabilities", () => {
    const design = resolveDesign(wallLibrary);

    expect(designPlanning(design.spec, design.rule_evaluations, design.source)).toEqual({
      wallAnchoringRequired: true,
      unsupportedSystems: ["joint_retention", "cabinet_hardware"],
    });
  });

  it.each(["WARNING", "BLOCK"] as const)("reports unresolved %s capabilities once in a stable order", (status) => {
    const evaluations = [
      evaluation("CB-HARDWARE-001", { status }),
      evaluation("CB-JOINT-001", { status }),
      evaluation("CB-HARDWARE-001", { status }),
      evaluation("CB-JOINT-001", { status }),
    ];

    expect(designPlanning(wallLibrary, evaluations, "local").unsupportedSystems)
      .toEqual(["joint_retention", "cabinet_hardware"]);
  });

  it("does not infer missing capabilities for absent or passing rules", () => {
    expect(designPlanning(wallLibrary, [], "local").unsupportedSystems).toEqual([]);
    expect(designPlanning(wallLibrary, [
      evaluation("CB-JOINT-001", { status: "PASS" }),
      evaluation("CB-HARDWARE-001", { status: "PASS" }),
    ], "local").unsupportedSystems).toEqual([]);
  });

  it("does not classify adjustable shelves as an unavailable fixed-shelf system", () => {
    const evaluations = [evaluation("CB-JOINT-001")];

    expect(designPlanning({ ...DEFAULT_DESIGN_SPEC, fixed_shelves: false }, evaluations, "local")
      .unsupportedSystems).toEqual([]);
  });

  it("preserves an actionable adjustable-shelf evaluation even if its spec is stale", () => {
    const evaluations = [evaluation("CB-JOINT-001", {
      diagnostics: [{ label: "Hylltyp", value: "ADJUSTABLE" }],
    })];

    expect(designPlanning(DEFAULT_DESIGN_SPEC, evaluations, "local").unsupportedSystems)
      .toEqual([]);
  });

  it.each([
    { ...wallLibrary, furniture_type: "bookcase" as const },
    { ...wallLibrary, base_cabinet_count: 0 },
  ])("does not infer cabinet hardware without active wall-library cabinets", (spec) => {
    expect(designPlanning(spec, [evaluation("CB-HARDWARE-001")], "local").unsupportedSystems)
      .toEqual([]);
  });

  it("matches exact known local rule identities rather than text or prefixes", () => {
    const evaluations = [
      evaluation("CB-JOINT-002", { title: "CB-JOINT-001" }),
      evaluation("CB-HARDWARE-001-FUTURE", { summary: "CB-HARDWARE-001" }),
      evaluation("UNKNOWN", { title: "Lokalt upplag i hyllspår och hyllbärare" }),
    ];

    expect(designPlanning(wallLibrary, evaluations, "local").unsupportedSystems).toEqual([]);
  });

  it("never infers local capability gaps from authoritative server evaluations", () => {
    const evaluations = [
      evaluation("CB-JOINT-001", { status: "BLOCK" }),
      evaluation("CB-HARDWARE-001", { status: "WARNING" }),
    ];

    expect(designPlanning(wallLibrary, evaluations, "server-preview").unsupportedSystems)
      .toEqual([]);
    expect(evaluations.map((item) => item.status)).toEqual(["BLOCK", "WARNING"]);
  });

  it("does not mutate the design, rule evaluations, diagnostics or verification", () => {
    const spec = Object.freeze(structuredClone(wallLibrary));
    const evaluations = Object.freeze([
      Object.freeze(evaluation("CB-JOINT-001", {
        diagnostics: [{ label: "hylltyp", value: "fixed" }],
      })),
      Object.freeze(evaluation("CB-HARDWARE-001")),
    ]);
    const before = structuredClone({ spec, evaluations });

    const result = designPlanning(spec, evaluations, "local");

    expect(result.unsupportedSystems).toEqual(["joint_retention", "cabinet_hardware"]);
    expect({ spec, evaluations }).toEqual(before);
    expect(spec.wall_anchor_verified).toBe(false);
  });
});
