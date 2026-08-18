import { describe, expect, it } from "vitest";
import { toPreviewRequest } from "./api-client";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "./design-types";
import { referenceVerificationFingerprint } from "./reference-image";
import {
  DesignHydrationError,
  MAX_CUSTOM_PART_IDS,
  WORKSPACE_INTENT_SCHEMA_V1,
  parseLocalDesignSpec,
  parseLocalDesignPatch,
  parseServerProjectDraft,
  workspaceIntentEnvelopeFromSpec,
} from "./workspace-design-envelope";

function serverDraft(
  spec: DesignSpec = DEFAULT_DESIGN_SPEC,
  workspaceSpecJson: unknown = workspaceIntentEnvelopeFromSpec(spec),
) {
  return {
    projectId: spec.design_id,
    draftRevision: spec.revision,
    specJson: toPreviewRequest(spec),
    workspaceSpecJson,
  };
}

function expectHydrationCode(run: () => unknown, code: DesignHydrationError["code"]): void {
  try {
    run();
    throw new Error("Expected hydration parsing to fail");
  } catch (error) {
    expect(error).toBeInstanceOf(DesignHydrationError);
    expect(error).toMatchObject({ code });
  }
}

describe("strict server draft hydration", () => {
  it("accepts only the explicit null/null revision-zero empty state", () => {
    expect(parseServerProjectDraft({
      projectId: "project-1",
      draftRevision: 0,
      specJson: null,
      workspaceSpecJson: null,
    })).toEqual({ kind: "empty" });

    expectHydrationCode(() => parseServerProjectDraft({
      projectId: "project-1",
      draftRevision: 1,
      specJson: null,
      workspaceSpecJson: null,
    }), "INVALID_SERVER_DRAFT");
  });

  it("uses canonical spec_json for every production field and only whitelists legacy UI intent", () => {
    const canonical = {
      ...DEFAULT_DESIGN_SPEC,
      design_id: "project-authoritative",
      revision: 7,
      width_mm: 2_400,
      divider_count: 2,
      bay_width_ratios: [0.3, 0.4, 0.3],
    };
    const maliciousLegacyWorkspace = {
      ...canonical,
      width_mm: 5_999,
      height_mm: 3_999,
      divider_count: 16,
      bay_width_ratios: Array.from({ length: 17 }, () => 1),
      material_id: "mdf",
      joint_system: "not-a-joint",
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 420,
    };
    const parsed = parseServerProjectDraft(serverDraft(canonical, maliciousLegacyWorkspace));

    expect(parsed.kind).toBe("ready");
    if (parsed.kind !== "ready") return;
    expect(parsed.spec).toMatchObject({
      width_mm: 2_400,
      height_mm: DEFAULT_DESIGN_SPEC.height_mm,
      divider_count: 2,
      bay_width_ratios: [0.3, 0.4, 0.3],
      material_id: DEFAULT_DESIGN_SPEC.material_id,
      joint_system: "dado",
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 420,
    });
  });

  it.each([
    ["width_mm", 249],
    ["height_mm", 4_001],
    ["depth_mm", Number.NaN],
    ["shelf_count", 41],
    ["divider_count", 1.5],
    ["load_per_shelf_kg", 501],
    ["base_cabinet_count", 18],
  ])("rejects invalid canonical %s before composition", (field, value) => {
    const draft = serverDraft();
    expectHydrationCode(() => parseServerProjectDraft({
      ...draft,
      specJson: { ...(draft.specJson as Record<string, unknown>), [field]: value },
    }), "INVALID_SERVER_DRAFT");
  });

  it("enforces ratio and furniture-family invariants", () => {
    const defaultDraft = serverDraft();
    const canonical = defaultDraft.specJson as Record<string, unknown>;
    expectHydrationCode(() => parseServerProjectDraft({
      ...defaultDraft,
      specJson: { ...canonical, divider_count: 2, bay_width_ratios: [0.01, 0.49, 0.5] },
    }), "INVALID_SERVER_DRAFT");
    expectHydrationCode(() => parseServerProjectDraft({
      ...defaultDraft,
      specJson: { ...canonical, shelf_count: 2, shelf_height_ratios: [0.4, 0.44] },
    }), "INVALID_SERVER_DRAFT");
    expectHydrationCode(() => parseServerProjectDraft({
      ...defaultDraft,
      specJson: { ...canonical, base_cabinet_count: 1, base_cabinet_height_mm: 600, base_cabinet_depth_mm: 320 },
    }), "INVALID_SERVER_DRAFT");

    const wallLibrary: DesignSpec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      height_mm: 2_400,
      depth_mm: 340,
      base_cabinet_count: 4,
      base_cabinet_height_mm: 700,
      base_cabinet_depth_mm: 340,
    };
    const wallDraft = serverDraft(wallLibrary);
    expectHydrationCode(() => parseServerProjectDraft({
      ...wallDraft,
      specJson: { ...(wallDraft.specJson as Record<string, unknown>), base_cabinet_depth_mm: 339 },
    }), "INVALID_SERVER_DRAFT");
    expectHydrationCode(() => parseServerProjectDraft({
      ...wallDraft,
      specJson: { ...(wallDraft.specJson as Record<string, unknown>), base_cabinet_height_mm: 2_200 },
    }), "INVALID_SERVER_DRAFT");
    expectHydrationCode(() => parseServerProjectDraft({
      ...defaultDraft,
      specJson: { ...canonical, wall_anchor_verified: true },
    }), "INVALID_SERVER_DRAFT");
  });

  it("validates production context independently and never lets it change geometry", () => {
    const canonical = { ...DEFAULT_DESIGN_SPEC, width_mm: 2_100 };
    const envelope = workspaceIntentEnvelopeFromSpec({
      ...canonical,
      stock_width_mm: 5_000,
      stock_count: 9,
      machine_profile_id: "custombuild-router-5125-linuxcnc",
    });
    const parsed = parseServerProjectDraft(serverDraft(canonical, envelope));
    expect(parsed.kind).toBe("ready");
    if (parsed.kind !== "ready") return;
    expect(parsed.spec.width_mm).toBe(2_100);
    expect(parsed.spec.stock_width_mm).toBe(5_000);
    expect(parsed.spec.stock_count).toBe(9);

    expectHydrationCode(() => parseServerProjectDraft(serverDraft(canonical, {
      ...envelope,
      production_context: { ...envelope.production_context, machine_profile_id: "unknown-machine" },
    })), "INVALID_SERVER_DRAFT");
  });
});

describe("bounded workspace intent", () => {
  it("writes a small V1 envelope without canonical production fields or layout ratios", () => {
    const envelope = workspaceIntentEnvelopeFromSpec({
      ...DEFAULT_DESIGN_SPEC,
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 360,
      bay_width_ratios: [1],
      shelf_height_ratios: [0.1, 0.25, 0.4, 0.6, 0.8],
    });
    expect(envelope).toMatchObject({
      schema_version: WORKSPACE_INTENT_SCHEMA_V1,
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 360,
    });
    expect(envelope).not.toHaveProperty("width_mm");
    expect(envelope).not.toHaveProperty("material_id");
    expect(envelope).not.toHaveProperty("joint_system");
    expect(envelope).not.toHaveProperty("bay_width_ratios");
    expect(envelope).not.toHaveProperty("shelf_height_ratios");
  });

  it("fails closed on unknown V1 fields, unknown IDs and excessive customization", () => {
    const envelope = workspaceIntentEnvelopeFromSpec(DEFAULT_DESIGN_SPEC);
    expectHydrationCode(() => parseServerProjectDraft(serverDraft(DEFAULT_DESIGN_SPEC, {
      ...envelope,
      width_mm: 5_000,
    })), "INVALID_SERVER_DRAFT");
    expectHydrationCode(() => parseServerProjectDraft(serverDraft(DEFAULT_DESIGN_SPEC, {
      ...envelope,
      removed_part_ids: ["unknown-part"],
    })), "INVALID_SERVER_DRAFT");
    expectHydrationCode(() => parseServerProjectDraft(serverDraft(DEFAULT_DESIGN_SPEC, {
      ...envelope,
      removed_part_ids: Array.from({ length: MAX_CUSTOM_PART_IDS + 1 }, (_, index) => `part-${index}`),
    })), "INVALID_SERVER_DRAFT");
    expectHydrationCode(() => workspaceIntentEnvelopeFromSpec({
      ...DEFAULT_DESIGN_SPEC,
      part_overrides: { "unknown-part": { width_mm: 500 } },
    }), "INVALID_WORKSPACE_INTENT");
  });

  it("requires a known schema version and exact legacy top-level keys", () => {
    const envelope = workspaceIntentEnvelopeFromSpec(DEFAULT_DESIGN_SPEC);
    const schemaLess: Record<string, unknown> = { ...envelope };
    delete schemaLess.schema_version;
    expectHydrationCode(() => parseServerProjectDraft(serverDraft(
      DEFAULT_DESIGN_SPEC,
      schemaLess,
    )), "INVALID_SERVER_DRAFT");
    expectHydrationCode(() => parseServerProjectDraft(serverDraft(DEFAULT_DESIGN_SPEC, {
      ...DEFAULT_DESIGN_SPEC,
      unexpected: true,
    })), "INVALID_SERVER_DRAFT");
  });

  it("rejects a topology baseline that cannot be safely restored for the canonical furniture", () => {
    const envelope = workspaceIntentEnvelopeFromSpec(DEFAULT_DESIGN_SPEC);
    expectHydrationCode(() => parseServerProjectDraft(serverDraft(DEFAULT_DESIGN_SPEC, {
      ...envelope,
      topology_baseline: {
        divider_count: DEFAULT_DESIGN_SPEC.divider_count,
        shelf_count: DEFAULT_DESIGN_SPEC.shelf_count,
        base_cabinet_count: 1,
        bay_width_ratios: DEFAULT_DESIGN_SPEC.bay_width_ratios,
        shelf_height_ratios: DEFAULT_DESIGN_SPEC.shelf_height_ratios,
        reinforcement_mode: DEFAULT_DESIGN_SPEC.reinforcement_mode,
      },
    })), "INVALID_SERVER_DRAFT");

    const divided = { ...DEFAULT_DESIGN_SPEC, divider_count: 2, bay_width_ratios: [] };
    const dividedEnvelope = workspaceIntentEnvelopeFromSpec(divided);
    expectHydrationCode(() => parseServerProjectDraft(serverDraft(divided, {
      ...dividedEnvelope,
      topology_baseline: {
        divider_count: 1,
        shelf_count: divided.shelf_count,
        base_cabinet_count: 0,
        bay_width_ratios: [],
        shelf_height_ratios: divided.shelf_height_ratios,
        reinforcement_mode: divided.reinforcement_mode,
      },
    })), "INVALID_SERVER_DRAFT");
  });

  it("accepts the exact bounded IDs emitted by the current base generator", () => {
    const wallLibrary: DesignSpec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      depth_mm: 340,
      divider_count: 1,
      shelf_count: 1,
      base_cabinet_height_mm: 700,
      base_cabinet_depth_mm: 340,
      base_cabinet_count: 2,
      part_overrides: {
        "base-side-2": { position_x_mm: 600 },
        "cabinet-front-2": { width_mm: 500 },
        "back-panel": { width_mm: 1_100 },
        "plinth-front": { width_mm: 1_100 },
      },
      removed_part_ids: ["shelf-1-bay-2"],
    };
    expect(workspaceIntentEnvelopeFromSpec(wallLibrary).part_overrides).toEqual(wallLibrary.part_overrides);
  });

  it("rejects malformed override values before they reach part generation", () => {
    const envelope = workspaceIntentEnvelopeFromSpec(DEFAULT_DESIGN_SPEC);
    expectHydrationCode(() => parseServerProjectDraft(serverDraft(DEFAULT_DESIGN_SPEC, {
      ...envelope,
      part_overrides: { top: { position_z_mm: Number.POSITIVE_INFINITY } },
    })), "INVALID_SERVER_DRAFT");
    expectHydrationCode(() => parseServerProjectDraft(serverDraft(DEFAULT_DESIGN_SPEC, {
      ...envelope,
      part_overrides: { top: { width_mm: 900, unexpected: true } },
    })), "INVALID_SERVER_DRAFT");
  });
});

describe("local legacy parsing and reference provenance", () => {
  it("bounds a partial legacy spec before it can be resolved", () => {
    expect(parseLocalDesignSpec({ width_mm: 1_500 })).toMatchObject({
      width_mm: 1_500,
      schema_version: "1.0",
    });
    expectHydrationCode(() => parseLocalDesignSpec({ width_mm: "1500" }), "INVALID_LOCAL_DESIGN");
    expectHydrationCode(() => parseLocalDesignSpec({ removed_part_ids: ["not-generated"] }), "INVALID_LOCAL_DESIGN");
    expectHydrationCode(() => parseLocalDesignSpec({ unexpected_nested_payload: { width_mm: 1_500 } }), "INVALID_LOCAL_DESIGN");
    expectHydrationCode(() => parseLocalDesignSpec({ wall_anchor_verified: true }), "INVALID_LOCAL_DESIGN");
  });

  it("accepts the exact nineteen-level five-percent boundary despite floating-point noise", () => {
    const shelfHeightRatios = Array.from({ length: 19 }, (_, index) => (index + 1) / 20);

    expect(parseLocalDesignSpec({
      shelf_count: 19,
      shelf_height_ratios: shelfHeightRatios,
    }).shelf_height_ratios).toEqual(shelfHeightRatios);

    expectHydrationCode(() => parseLocalDesignSpec({
      shelf_count: 19,
      shelf_height_ratios: shelfHeightRatios.map((ratio, index) => (
        index === 1 ? ratio - 0.000_001 : ratio
      )),
    }), "INVALID_LOCAL_DESIGN");
  });

  it("applies wall-library depth edits atomically before strict family parsing", () => {
    const wallLibrary: DesignSpec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      depth_mm: 340,
      base_cabinet_count: 1,
      base_cabinet_height_mm: 700,
      base_cabinet_depth_mm: 340,
    };

    expect(parseLocalDesignPatch(wallLibrary, { depth_mm: 1_200 })).toMatchObject({
      depth_mm: 1_200,
      base_cabinet_depth_mm: 1_200,
    });
    expect(parseLocalDesignPatch(DEFAULT_DESIGN_SPEC, { depth_mm: 1_200 })).toMatchObject({
      depth_mm: 1_200,
      base_cabinet_depth_mm: 0,
    });
    expectHydrationCode(() => parseLocalDesignPatch(wallLibrary, {
      depth_mm: 400,
      base_cabinet_depth_mm: 399,
    }), "INVALID_LOCAL_DESIGN");
  });

  it("rejects an out-of-envelope live patch instead of returning persistable state", () => {
    expectHydrationCode(() => parseLocalDesignPatch(DEFAULT_DESIGN_SPEC, {
      width_mm: 6_001,
    }), "INVALID_LOCAL_DESIGN");
  });

  it("keeps valid confirmed provenance only for the exact composed fingerprint", () => {
    const provenanceBase = {
      source: "reference_image" as const,
      import_id: "11111111-1111-4111-8111-111111111111",
      image_sha256: "a".repeat(64),
      file_name: "referens.png",
      image_width_px: 1_200,
      image_height_px: 900,
      confidence: 0.9,
      detected_shelves: 5,
      detected_dividers: 0,
      detected_base_cabinets: false,
      warnings: [],
      verification_status: "parametric_confirmed" as const,
      confirmed_inputs: {
        dimensions_measured: true,
        layout_confirmed: true,
        material_confirmed: true,
        construction_assumptions_confirmed: true,
      },
    };
    const unverified: DesignSpec = { ...DEFAULT_DESIGN_SPEC, reference_image_import: provenanceBase };
    const verified: DesignSpec = {
      ...unverified,
      reference_image_import: {
        ...provenanceBase,
        verified_model_fingerprint: referenceVerificationFingerprint(unverified),
      },
    };
    expect(parseLocalDesignSpec(verified).reference_image_import?.verification_status).toBe("parametric_confirmed");

    const changed = parseLocalDesignSpec({ ...verified, width_mm: verified.width_mm + 1 });
    expect(changed.reference_image_import).toMatchObject({
      verification_status: "concept",
      confirmed_inputs: {
        dimensions_measured: false,
        layout_confirmed: false,
        material_confirmed: false,
        construction_assumptions_confirmed: false,
      },
    });
    expect(changed.reference_image_import?.verified_model_fingerprint).toBeUndefined();

    const persistedIntent = workspaceIntentEnvelopeFromSpec({
      ...verified,
      width_mm: verified.width_mm + 1,
    });
    expect(persistedIntent.reference_image_import).toMatchObject({
      verification_status: "concept",
      confirmed_inputs: {
        dimensions_measured: false,
        layout_confirmed: false,
        material_confirmed: false,
        construction_assumptions_confirmed: false,
      },
    });
    expect(persistedIntent.reference_image_import?.verified_model_fingerprint).toBeUndefined();
  });
});
