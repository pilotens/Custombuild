import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  CustombuildApiClient,
  normalizePreviewResponse,
  productionContextFromSpec,
  toPreviewRequest,
  toSourceProvenance,
  type DesignVersionRead,
  type ProjectDraftRead,
  versionProductionContextMatches,
} from "./api-client";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "./design-types";
import { referenceVerificationFingerprint } from "./reference-image";
import * as designEngine from "./design-engine";

function serverSemanticName(partId: string): string {
  if (partId === "side-left") return "left-side";
  if (partId === "side-right") return "right-side";
  if (partId === "back-panel") return "back";
  const backField = /^back-panel-bay-(\d+)$/.exec(partId);
  if (backField) return `back-b${Number(backField[1]) - 1}`;
  if (partId === "plinth-front") return "plinth";
  const divider = /^divider-(\d+)$/.exec(partId);
  if (divider) return `divider-${Number(divider[1]) - 1}`;
  const shelf = /^shelf-(\d+)-bay-(\d+)$/.exec(partId);
  if (shelf) return `shelf-r${Number(shelf[1]) - 1}-b${Number(shelf[2]) - 1}`;
  const base = /^(base-side|base-bottom|base-top|cabinet-front)-(\d+)$/.exec(partId);
  if (base) return `${base[1]}-${Number(base[2]) - 1}`;
  return partId;
}

function exactServerParts(spec: DesignSpec = DEFAULT_DESIGN_SPEC) {
  return designEngine.resolveDesign(spec).parts.map((part) => ({
    part_id: `server:${part.part_id}`,
    name: serverSemanticName(part.part_id),
    kind: part.kind,
    width_mm: part.width_mm,
    depth_mm: part.depth_mm,
    thickness_mm: part.thickness_mm,
    position_mm: part.position_mm,
    orientation: part.orientation,
    material_id: part.material_id,
    weight_kg: part.weight_kg,
    features: [],
  }));
}

async function sha256Hex(content: Uint8Array): Promise<string> {
  const buffer = new ArrayBuffer(content.byteLength);
  new Uint8Array(buffer).set(content);
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function sha256Base64(sha256: string): string {
  const bytes = Array.from({ length: 32 }, (_, index) => (
    Number.parseInt(sha256.slice(index * 2, index * 2 + 2), 16)
  ));
  return btoa(String.fromCharCode(...bytes));
}

function artifactResponse(
  content: Uint8Array,
  artifact: { content_type: string; sha256: string; size_bytes: number; download_path: string },
  overrides: {
    headers?: Record<string, string | undefined>;
    redirected?: boolean;
    status?: number;
    url?: string;
  } = {},
): Response {
  const headers = new Headers({
    "Content-Length": String(artifact.size_bytes),
    "Content-Type": artifact.content_type,
    Digest: `sha-256=${sha256Base64(artifact.sha256)}`,
    ETag: `"${artifact.sha256}"`,
  });
  for (const [name, value] of Object.entries(overrides.headers ?? {})) {
    if (value === undefined) headers.delete(name);
    else headers.set(name, value);
  }
  const body = new ArrayBuffer(content.byteLength);
  new Uint8Array(body).set(content);
  const response = new Response(body, {
    status: overrides.status ?? 200,
    headers,
  });
  Object.defineProperties(response, {
    redirected: { configurable: true, value: overrides.redirected ?? false },
    url: {
      configurable: true,
      value: overrides.url ?? `https://api.example.test${artifact.download_path}`,
    },
  });
  return response;
}

describe("API preview normalization", () => {
  it("merges a tolerant server response with deterministic local output", () => {
    const result = normalizePreviewResponse(
      {
        design_hash: "a".repeat(64),
        status: "warning",
        rule_evaluations: [
          {
            rule_id: "SERVER-001",
            rule_version: "2.0.0",
            status: "WARNING",
            title: "Serverregel",
            summary: "Verifierad av servern",
            calculation: "1 + 1 = 2",
            assumptions: ["Test"],
            affected_part_ids: ["side-left"],
          },
        ],
      },
      DEFAULT_DESIGN_SPEC,
    );

    expect(result.source).toBe("server-preview");
    expect(result.design_hash).toBe("a".repeat(64));
    expect(result.parts.length).toBeGreaterThan(0);
    expect(result.rule_evaluations[0]?.rule_id).toBe("SERVER-001");
    expect(result.status).toBe("WARNING");
  });

  it("rejects a non-object server payload", () => {
    expect(() => normalizePreviewResponse(null, DEFAULT_DESIGN_SPEC)).toThrow(/okänt format/i);
  });

  it("accepts only the rule units that the server contract declares nullable", () => {
    const rule = {
      rule_id: "CB-JOINT-001",
      rule_version: "1.0.0",
      status: "WARNING",
      title: "Fogkontroll",
      calculated_value: 1,
      allowed_value: 0,
      unit: "N",
      inputs: [{ name: "hylltyp", value: "fixed", unit: null }],
      trace: [{ expression: "hylltyp", result: "fixed", unit: null }],
    };

    const result = normalizePreviewResponse({ rule_evaluations: [rule] }, DEFAULT_DESIGN_SPEC);

    expect(result.rule_evaluations[0]?.diagnostics).toEqual([
      { label: "hylltyp", value: "fixed" },
    ]);
    expect(result.rule_evaluations[0]?.calculation).toBe("hylltyp: fixed");
    expect(() => normalizePreviewResponse({
      rule_evaluations: [{ ...rule, unit: null }],
    }, DEFAULT_DESIGN_SPEC)).toThrow(/rule_evaluations\[0\]\.unit/);
  });

  it("uses a neutral fallback label for a minimum rule threshold", () => {
    const result = normalizePreviewResponse({
      rule_evaluations: [{
        rule_id: "CB-TIP-001",
        rule_version: "1.3.0",
        status: "PASS",
        title: "Tippsäkerhetsfaktor",
        calculated_value: 2_000,
        allowed_value: 1_500,
        unit: "‰ safety factor",
      }],
    }, DEFAULT_DESIGN_SPEC);

    expect(result.rule_evaluations[0]?.summary).toContain("gränsvärde 1500");
    expect(result.rule_evaluations[0]?.summary).not.toContain("tillåtet");
  });

  it("accepts live rule integers above the generic measurement bound and rejects unsafe numbers", () => {
    const rule = {
      rule_id: "CB-JOINT-001",
      rule_version: "1.0.0",
      status: "PASS",
      title: "Fogkontroll",
      calculated_value: 53,
      allowed_value: 4_049,
      safety_margin_permille: 75_396,
      unit: "N",
      inputs: [{ name: "bärande_area", value: 1_768_034_000, unit: "µm²" }],
    };

    const result = normalizePreviewResponse({ rule_evaluations: [rule] }, DEFAULT_DESIGN_SPEC);
    expect(result.rule_evaluations[0]?.diagnostics).toEqual([
      { label: "bärande area", value: "1768034000", unit: "µm²" },
    ]);

    for (const value of [1.5, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1]) {
      expect(() => normalizePreviewResponse({
        rule_evaluations: [{
          ...rule,
          inputs: [{ name: "bärande_area", value, unit: "µm²" }],
        }],
      }, DEFAULT_DESIGN_SPEC)).toThrow(/rule_evaluations\[0\]\.inputs\[0\]\.value/);
    }
  });

  it.each([
    ["vertical_divider_count", 1_000_000_000],
    ["vertical_divider_count", 1.5],
    ["width_um", 6_000_001],
  ])("rejects unsafe server spec %s before any design composition", (field, value) => {
    const resolveSpy = vi.spyOn(designEngine, "resolveDesign");
    expect(() => normalizePreviewResponse({
      spec: {
        parameters: { [field]: value, back_panel: DEFAULT_DESIGN_SPEC.back_panel_type },
        back_material: { material_id: DEFAULT_DESIGN_SPEC.back_material_id },
      },
    }, DEFAULT_DESIGN_SPEC)).toThrow(/ogiltigt värde|utanför/i);
    expect(resolveSpy).not.toHaveBeenCalled();
  });

  it("rejects oversized response collections and out-of-envelope part geometry", () => {
    expect(() => normalizePreviewResponse({
      parts: Array.from({ length: 1_025 }, (_, index) => ({ id: index })),
    }, DEFAULT_DESIGN_SPEC)).toThrow(/för stor lista/i);

    const parts = exactServerParts();
    parts[0] = { ...parts[0]!, width_mm: 6_001 };
    expect(() => normalizePreviewResponse({ parts }, DEFAULT_DESIGN_SPEC)).toThrow(/width_mm/i);
  });

  it("rejects duplicate server part_id values before rule identity mapping", () => {
    const parts = exactServerParts();
    parts[1] = { ...parts[1]!, part_id: parts[0]!.part_id };
    expect(() => normalizePreviewResponse({ parts }, DEFAULT_DESIGN_SPEC)).toThrow(/duplicerat part_id/i);
  });

  it.each([
    ["XY", "top", "width_mm"],
    ["XZ", "back", "depth_mm"],
    ["YZ", "left-side", "width_mm"],
  ] as const)("rejects %s parts whose oriented AABB escapes the exact design envelope", (
    _orientation,
    semanticName,
    dimension,
  ) => {
    const parts = exactServerParts();
    const index = parts.findIndex((part) => part.name === semanticName);
    expect(index).toBeGreaterThanOrEqual(0);
    parts[index] = { ...parts[index]!, [dimension]: 6_000 };
    expect(() => normalizePreviewResponse({ parts }, DEFAULT_DESIGN_SPEC)).toThrow(/designrymden/i);
  });

  it("allows only the half-micrometre centre loss in the server part envelope", () => {
    const parts = exactServerParts();
    const index = parts.findIndex((part) => part.name === "plinth");
    expect(index).toBeGreaterThanOrEqual(0);
    const plinth = parts[index]!;
    const zAtExactLowerEdge = plinth.depth_mm / 2;

    parts[index] = {
      ...plinth,
      position_mm: {
        ...plinth.position_mm,
        z: zAtExactLowerEdge - 0.000_5,
      },
    };
    expect(() => normalizePreviewResponse({ parts }, DEFAULT_DESIGN_SPEC)).not.toThrow();

    parts[index] = {
      ...plinth,
      position_mm: {
        ...plinth.position_mm,
        z: zAtExactLowerEdge - 0.000_502,
      },
    };
    expect(() => normalizePreviewResponse({ parts }, DEFAULT_DESIGN_SPEC)).toThrow(/designrymden/i);
  });

  it("sends only the strict server contract and never client-borne safety evidence", () => {
    const request = toPreviewRequest({
      ...DEFAULT_DESIGN_SPEC,
      fixed_shelves: false,
      joint_system: "dado",
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 300,
      divider_count: 2,
      wall_anchor_verified: true,
      reference_image_import: {
        source: "reference_image",
        import_id: "11111111-1111-4111-8111-111111111111",
        image_sha256: "a".repeat(64),
        file_name: "referens.jpg",
        image_width_px: 800,
        image_height_px: 600,
        confidence: 0.8,
        detected_shelves: 4,
        detected_dividers: 2,
        detected_base_cabinets: false,
        warnings: [],
      },
      part_overrides: { "side-left": { width_mm: 2_000 } },
      symmetry_locked: false,
      removed_part_ids: ["shelf-1-bay-1"],
      topology_baseline: {
        divider_count: 2,
        shelf_count: 5,
        base_cabinet_count: 0,
        bay_width_ratios: [],
        shelf_height_ratios: [],
        reinforcement_mode: "auto",
      },
    });

    expect(request.shelf_mount).toBe("adjustable");
    expect(request.joint_system).toBe("dado");
    expect(request.back_material_id).toBe(DEFAULT_DESIGN_SPEC.back_material_id);
    expect(request.back_panel).toBe("inset_groove");
    expect(request.wall_anchor_verified).toBe(false);
    expect(request.divider_count).toBe(2);
    expect(request).not.toHaveProperty("schema_version");
    expect(request).not.toHaveProperty("machine_profile_id");
    expect(request.bay_width_ratios).toEqual(DEFAULT_DESIGN_SPEC.bay_width_ratios);
    expect(request.shelf_height_ratios).toEqual(DEFAULT_DESIGN_SPEC.shelf_height_ratios);
    expect(request).not.toHaveProperty("reference_image_import");
    expect(request).not.toHaveProperty("part_overrides");
    expect(request).not.toHaveProperty("removed_part_ids");
    expect(request).not.toHaveProperty("topology_baseline");
    expect(request).not.toHaveProperty("symmetry_locked");
    expect(request).not.toHaveProperty("bay_sizing_mode");
    expect(request).not.toHaveProperty("target_bay_width_mm");
  });

  it("omits a dormant back-material choice when the design has no back panel", () => {
    expect(toPreviewRequest({
      ...DEFAULT_DESIGN_SPEC,
      back_panel: false,
      back_material_id: "mdf-6",
    })).not.toHaveProperty("back_material_id");
  });

  it("sends an exact surface mount instead of collapsing it to a boolean", () => {
    expect(toPreviewRequest({
      ...DEFAULT_DESIGN_SPEC,
      back_panel: true,
      back_panel_type: "surface_mounted",
    }).back_panel).toBe("surface_mounted");
  });

  it("rejects an unsupported joint instead of silently normalizing it to DADO", () => {
    const unsupported = {
      ...DEFAULT_DESIGN_SPEC,
      joint_system: "confirmat",
    } as unknown as DesignSpec;

    expect(() => toPreviewRequest(unsupported)).toThrow(
      /Förbandssystemet confirmat.*stöds inte/i,
    );
  });

  it("sends audited image provenance only while all confirmations match the exact model", () => {
    const base = {
      ...DEFAULT_DESIGN_SPEC,
      reference_image_import: {
        source: "reference_image" as const,
        import_id: "11111111-1111-4111-8111-111111111111",
        image_sha256: "a".repeat(64),
        file_name: "verified.png",
        image_width_px: 1200,
        image_height_px: 800,
        confidence: 0.81,
        detected_shelves: 4,
        detected_dividers: 2,
        detected_base_cabinets: false,
        warnings: [],
        verification_status: "parametric_confirmed" as const,
        confirmed_inputs: {
          dimensions_measured: true,
          layout_confirmed: true,
          material_confirmed: true,
          construction_assumptions_confirmed: true,
        },
      },
    };
    const verified: DesignSpec = {
      ...base,
      reference_image_import: {
        ...base.reference_image_import,
        verified_model_fingerprint: referenceVerificationFingerprint(base),
      },
    };

    expect(toSourceProvenance(verified, "b".repeat(64))).toMatchObject({
      source: "reference_image",
      import_id: "11111111-1111-4111-8111-111111111111",
      image_sha256: "a".repeat(64),
      verification_status: "parametric_confirmed",
      verified_model_fingerprint: "b".repeat(64),
      confirmed_inputs: { material_confirmed: true },
    });
    expect(toSourceProvenance({ ...verified, width_mm: verified.width_mm + 10 })).toBeUndefined();
  });

  it("sends custom grid ratios to the authoritative production engine", () => {
    const request = toPreviewRequest({
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 2,
      shelf_count: 3,
      bay_width_ratios: [0.2, 0.6, 0.2],
      shelf_height_ratios: [0.2, 0.5, 0.8],
    });

    expect(request.bay_width_ratios).toEqual([0.2, 0.6, 0.2]);
    expect(request.shelf_height_ratios).toEqual([0.2, 0.5, 0.8]);
  });

  it("maps production IDs back to stable editable part identities", () => {
    const parts = exactServerParts();
    parts[0] = {
      ...parts[0]!,
      part_id: "par_server-left",
      name: "left-side",
      width_mm: 2100,
      depth_mm: 320,
      thickness_mm: 17.8,
      position_mm: { x: 8.9, y: 160, z: 1050 },
      orientation: "YZ",
      material_id: "birch-plywood",
      weight_kg: 8,
    };
    const result = normalizePreviewResponse(
      {
        design_hash: "c".repeat(64),
        status: "WARNING",
        parts,
        rule_evaluations: [{
          rule_id: "SERVER-PART-001",
          rule_version: "1.0.0",
          status: "WARNING",
          title: "Kontroll",
          calculated_value: 1,
          allowed_value: 0,
          unit: "st",
          inputs: [
            { name: "ostödda_centrumlinjer", value: "836 mm, 1673 mm" },
            { name: "inre_underskåpssidor", value: 3, unit: "st" },
          ],
          applies_to_part_ids: ["par_server-left"],
          suggested_actions: [{
            action_type: "verify_hardware_capacity",
            description: "Välj ett versionslåst beslag och verifiera borrbilden.",
          }],
        }],
      },
      DEFAULT_DESIGN_SPEC,
    );

    expect(result.parts[0]?.part_id).toBe("side-left");
    expect(result.parts[0]?.name).toBe("Vänster gavel");
    expect(result.rule_evaluations[0]?.affected_part_ids).toEqual(["side-left"]);
    expect(result.rule_evaluations[0]?.suggestion).toMatchObject({
      action: "manual_review",
      explanation: "Välj ett versionslåst beslag och verifiera borrbilden.",
    });
    expect(result.rule_evaluations[0]?.summary).toBe(
      "Välj ett versionslåst beslag och verifiera borrbilden.",
    );
    expect(result.rule_evaluations[0]?.diagnostics).toEqual([
      { label: "ostödda centrumlinjer", value: "836 mm, 1673 mm" },
      { label: "inre underskåpssidor", value: "3", unit: "st" },
    ]);
  });

  it("maps canonical server rules, autofix spec and diff without mixing local rules", () => {
    const result = normalizePreviewResponse(
      {
        design_hash: "b".repeat(64),
        status: "PASS",
        spec: {
          material: { material_id: "birch-plywood", name: "Björkplywood 18 mm" },
          back_material: { material_id: "mdf-6", name: "MDF 6 mm" },
          parameters: {
            width_um: 1_200_000,
            height_um: 2_100_000,
            depth_um: 320_000,
            nominal_thickness_um: 18_000,
            actual_thickness_um: 17_800,
            shelf_count: 5,
            shelf_mount: "fixed",
            shelf_load_n: 314,
            back_panel: "inset_groove",
            plinth_height_um: 80_000,
            vertical_divider_count: 1,
            reinforcement_mode: "auto",
            joint_system: "dado",
            edge_band_thickness_um: 1_000,
          },
        },
        rule_evaluations: [
          {
            rule_id: "CB-DEFLECTION-001",
            rule_version: "1.2.0",
            status: "PASS",
            title: "Hyllnedböjning",
            applies_to_part_ids: ["server-shelf-id"],
            assumptions: ["Serverantagande"],
            trace: [{ expression: "5wL^4/(384EI)", result: "1.2", unit: "mm" }],
            calculated_value: 1200,
            allowed_value: 3000,
            unit: "µm",
            safety_margin_permille: 600,
            suggested_actions: [
              {
                action_type: "add_vertical_divider",
                description: "Lägg till en vertikal avdelare",
                changes: [
                  { path: "parameters.vertical_divider_count", before: 0, after: 1 },
                ],
              },
            ],
          },
        ],
        change_diff: [
          {
            explanation: "Avdelaren minskar fri spännvidd.",
            changes: [
              { path: "parameters.vertical_divider_count", before: 0, after: 1 },
            ],
          },
        ],
      },
      DEFAULT_DESIGN_SPEC,
    );

    expect(result.spec.divider_count).toBe(1);
    expect(result.spec.reinforcement_mode).toBe("auto");
    expect(result.spec.back_material_id).toBe("mdf-6");
    expect(result.rule_evaluations).toHaveLength(1);
    expect(result.rule_evaluations[0]?.calculation).toContain("5wL^4");
    expect(result.rule_evaluations[0]?.affected_part_ids).toEqual(["server-shelf-id"]);
    expect(result.rule_evaluations[0]?.suggestion).toMatchObject({
      action: "set_divider_count",
      value: 1,
    });
    expect(result.change_diff).toEqual([
      expect.objectContaining({ field: "divider_count", before: 0, after: 1 }),
    ]);
  });

  it("rejects an unsupported joint returned by the server", () => {
    expect(() => normalizePreviewResponse(
      {
        design_hash: "c".repeat(64),
        status: "PASS",
        spec: {
          parameters: { joint_system: "dowel" },
        },
      },
      DEFAULT_DESIGN_SPEC,
    )).toThrow(/förbandssystemet dowel.*inte stöds/i);
  });

  it("rejects unknown, incomplete and backless server back-material identities", () => {
    const parameters = { back_panel: "inset_groove" };
    expect(() => normalizePreviewResponse({
      spec: { parameters: {}, back_material: { material_id: "mdf-6" } },
    }, DEFAULT_DESIGN_SPEC)).toThrow(/saknar en exakt bakstyckestyp/i);
    expect(() => normalizePreviewResponse({
      spec: { parameters, back_material: { material_id: "oak-6" } },
    }, DEFAULT_DESIGN_SPEC)).toThrow(/okända bakstyckesmaterialet oak-6/i);
    expect(() => normalizePreviewResponse({
      spec: { parameters, back_material: {} },
    }, DEFAULT_DESIGN_SPEC)).toThrow(/materialdefinition/i);
    expect(() => normalizePreviewResponse({
      spec: { parameters },
    }, DEFAULT_DESIGN_SPEC)).toThrow(/saknar en exakt materialdefinition/i);
    expect(() => normalizePreviewResponse({
      spec: {
        parameters: { back_panel: "none" },
        back_material: { material_id: "mdf-6" },
      },
    }, DEFAULT_DESIGN_SPEC)).toThrow(/utan ett bakstycke/i);
  });

  it.each([
    ["inset_groove", ["back-panel-bay-1", "back-panel-bay-2", "back-panel-bay-3"]],
    ["surface_mounted", ["back-panel"]],
  ] as const)("requires exact local/server back topology for %s", (backPanelType, expectedIds) => {
    const requested: DesignSpec = {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 2,
      reinforcement_mode: "manual",
      back_panel: true,
      back_panel_type: backPanelType,
    };
    const parts = exactServerParts(requested);
    const payload = {
      spec: {
        back_material: { material_id: requested.back_material_id },
        parameters: { back_panel: backPanelType },
      },
      parts,
    };
    const normalized = normalizePreviewResponse(payload, requested);

    expect(normalized.parts.filter((part) => part.kind === "back").map((part) => part.part_id))
      .toEqual(expectedIds);
    expect(normalized.spec.back_panel_type).toBe(backPanelType);
    expect(() => normalizePreviewResponse({ ...payload, parts: parts.slice(0, -1) }, requested))
      .toThrow(/exakt den verifierade lokala topologin/i);
  });
});

afterEach(() => vi.restoreAllMocks());

describe("production API contract", () => {
  it("uses a public development token only after an OIDC session token", async () => {
    const tokenKey = "custombuild:oidc:access-token";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
      JSON.stringify([]),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CustombuildApiClient(
      "https://api.example.test",
      undefined,
      "public-development-token",
    );

    await api.listProjects();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "https://api.example.test/v1/projects",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer public-development-token" }),
      }),
    );

    window.sessionStorage.setItem(tokenKey, JSON.stringify({
      accessToken: "signed-oidc-session-token",
      expiresAt: Date.now() + 120_000,
    }));
    await api.listProjects();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "https://api.example.test/v1/projects",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer signed-oidc-session-token" }),
      }),
    );
    window.sessionStorage.removeItem(tokenKey);
  });

  it("binds a fetched draft to the explicitly requested project", async () => {
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(
      JSON.stringify({ project_id: "project-b" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    await expect(api.getProjectDraft("project-a")).rejects.toMatchObject({
      code: "INVALID_SERVER_DRAFT",
      transportFailure: false,
    });

    fetchMock.mockResolvedValueOnce(new Response(
      "null",
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    await expect(api.getProjectDraft("project-a")).rejects.toMatchObject({
      code: "INVALID_SERVER_DRAFT",
      transportFailure: false,
    });

    fetchMock.mockResolvedValueOnce(new Response(
      JSON.stringify({
        project_id: "project-a",
        draft_revision: "1",
        template_id: null,
        design_hash: null,
        spec_json: null,
        workspace_spec_json: null,
        result_json: null,
        updated_at: "2026-08-15T08:00:00Z",
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    await expect(api.getProjectDraft("project-a")).rejects.toMatchObject({
      code: "INVALID_SERVER_DRAFT",
      transportFailure: false,
    });

    fetchMock.mockResolvedValueOnce(new Response(
      "not-json",
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    await expect(api.getProjectDraft("project-a")).rejects.toMatchObject({
      code: "INVALID_SERVER_RESPONSE",
      transportFailure: false,
    });
  });

  it.each([401, 403, 404, 500])("never marks HTTP %s as a transport fallback", async (status) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "blocked" }),
      { status, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");
    await expect(api.getProjectDraft("project-a")).rejects.toMatchObject({
      status,
      transportFailure: false,
    });
  });

  it("marks only an actual fetch failure as transport fallback", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network down"));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");
    await expect(api.getProjectDraft("project-a")).rejects.toMatchObject({
      code: "API_TRANSPORT_FAILURE",
      transportFailure: true,
    });
  });

  it("freezes every production choice and optimistic revision guard when creating a version", async () => {
    const response = { id: "version-8", revision: 8 };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify(response),
      { status: 201, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");
    const spec: DesignSpec = {
      ...DEFAULT_DESIGN_SPEC,
      stock_width_mm: 5000,
      stock_height_mm: 2500,
      stock_count: 3,
      back_stock_width_mm: 5000,
      back_stock_height_mm: 2500,
      back_stock_count: 2,
      machine_profile_id: "custombuild-router-5125-linuxcnc",
    };

    await expect(api.createVersion("project/1", spec, "d".repeat(64), 7, "wall-library")).resolves.toEqual(response);
    const request = fetchMock.mock.calls.at(-1)?.[1];
    const body = JSON.parse(String(request?.body)) as Record<string, unknown>;

    expect(body).toEqual(expect.objectContaining({
      expected_design_hash: "d".repeat(64),
      expected_current_revision: 7,
      template_id: "wall-library",
      production_context: productionContextFromSpec(spec),
    }));
    expect((body.spec as Record<string, unknown>).back_material_id).toBe(
      spec.back_material_id,
    );
  });

  it("treats legacy, extra-key and wrong-type production snapshots as stale", () => {
    const exact = productionContextFromSpec(DEFAULT_DESIGN_SPEC);
    const asVersion = (resultJson: Record<string, unknown>) => (
      { result_json: resultJson } as unknown as DesignVersionRead
    );
    const base = asVersion({ production_context: exact });

    expect(versionProductionContextMatches(base, DEFAULT_DESIGN_SPEC)).toBe(true);
    expect(versionProductionContextMatches(
      asVersion({}),
      DEFAULT_DESIGN_SPEC,
    )).toBe(false);
    expect(versionProductionContextMatches(
      asVersion({ production_context: { ...exact, unexpected: true } }),
      DEFAULT_DESIGN_SPEC,
    )).toBe(false);
    expect(versionProductionContextMatches(
      asVersion({ production_context: { ...exact, stock_count: "4" } }),
      DEFAULT_DESIGN_SPEC,
    )).toBe(false);
  });

  it("persists only a bounded V1 workspace intent beside the canonical server spec", async () => {
    const workspaceSpec: DesignSpec = {
      ...DEFAULT_DESIGN_SPEC,
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 300,
      divider_count: 2,
    };
    const draft = {
      project_id: "project/1",
      draft_revision: 4,
      template_id: "wall-library",
      design_hash: "a".repeat(64),
      spec_json: { width_mm: DEFAULT_DESIGN_SPEC.width_mm },
      workspace_spec_json: workspaceSpec,
      result_json: { design_hash: "a".repeat(64) },
      updated_at: "2026-08-10T12:00:00Z",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
      JSON.stringify(draft),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");

    await expect(api.getProjectDraft("project/1")).resolves.toEqual(draft);
    await expect(api.updateProjectDraft("project/1", "wall-library", workspaceSpec, 4)).resolves.toEqual(draft);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "https://api.example.test/v1/projects/project%2F1/draft",
      expect.objectContaining({
        method: "PUT",
        body: expect.stringContaining('"expected_draft_revision":4'),
      }),
    );
    const request = fetchMock.mock.calls.at(-1)?.[1];
    const body = JSON.parse(String(request?.body)) as {
      spec: Record<string, unknown>;
      workspace_spec: Record<string, unknown>;
    };
    expect(body.spec).not.toHaveProperty("bay_sizing_mode");
    expect(body.spec).not.toHaveProperty("target_bay_width_mm");
    expect(body.spec.divider_count).toBe(2);
    expect(body.spec.back_material_id).toBe(workspaceSpec.back_material_id);
    expect(body.workspace_spec).toEqual(expect.objectContaining({
      schema_version: "custombuild.workspace-intent.v1",
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 300,
    }));
    expect(body.workspace_spec).not.toHaveProperty("width_mm");
    expect(body.workspace_spec).not.toHaveProperty("divider_count");
    expect(body.workspace_spec).not.toHaveProperty("bay_width_ratios");
    expect(body.workspace_spec).not.toHaveProperty("material_id");
  });

  it("keeps two browser tabs conflict-safe until the stale tab reloads", async () => {
    let serverDraft: ProjectDraftRead = {
      project_id: "project-1",
      draft_revision: 0,
      template_id: "shelving",
      design_hash: "a".repeat(64),
      spec_json: { ...DEFAULT_DESIGN_SPEC },
      workspace_spec_json: { ...DEFAULT_DESIGN_SPEC },
      result_json: { design_hash: "a".repeat(64) },
      updated_at: "2026-08-11T12:00:00Z",
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      if (init?.method === "GET") {
        return new Response(JSON.stringify(serverDraft), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      const body = JSON.parse(String(init?.body)) as {
        expected_draft_revision: number;
        spec: Record<string, unknown>;
        workspace_spec: Record<string, unknown>;
      };
      if (body.expected_draft_revision !== serverDraft.draft_revision) {
        return new Response(JSON.stringify({
          detail: {
            code: "DRAFT_REVISION_CONFLICT",
            message: "The project draft was changed by another editor.",
            solution: "Reload the latest project draft and review it before saving again.",
            expected_draft_revision: body.expected_draft_revision,
            current_draft_revision: serverDraft.draft_revision,
          },
        }), { status: 409, headers: { "Content-Type": "application/json" } });
      }
      serverDraft = {
        ...serverDraft,
        draft_revision: serverDraft.draft_revision + 1,
        spec_json: body.spec,
        workspace_spec_json: body.workspace_spec,
      };
      return new Response(JSON.stringify(serverDraft), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    const tabA = new CustombuildApiClient("https://api.example.test", "tenant-token");
    const tabB = new CustombuildApiClient("https://api.example.test", "tenant-token");
    const initialA = await tabA.getProjectDraft("project-1");
    const initialB = await tabB.getProjectDraft("project-1");
    const tabBSpec = { ...DEFAULT_DESIGN_SPEC, width_mm: 1_100 };
    const tabASpec = { ...DEFAULT_DESIGN_SPEC, width_mm: 1_300 };

    const savedB = await tabB.updateProjectDraft(
      "project-1",
      "shelving",
      tabBSpec,
      initialB.draft_revision,
    );
    expect(savedB.draft_revision).toBe(1);
    await expect(tabA.updateProjectDraft(
      "project-1",
      "shelving",
      tabASpec,
      initialA.draft_revision,
    )).rejects.toMatchObject({ status: 409, code: "DRAFT_REVISION_CONFLICT" });
    expect(serverDraft.spec_json?.width_mm).toBe(1_100);
    expect(serverDraft.workspace_spec_json).not.toHaveProperty("width_mm");

    const reloadedA = await tabA.getProjectDraft("project-1");
    expect(reloadedA.draft_revision).toBe(1);
    expect(reloadedA.spec_json?.width_mm).toBe(1_100);
    const savedA = await tabA.updateProjectDraft(
      "project-1",
      "shelving",
      tabASpec,
      reloadedA.draft_revision,
    );
    expect(savedA.draft_revision).toBe(2);
    expect(serverDraft.spec_json?.width_mm).toBe(1_300);
  });

  it("uploads reference originals as multipart project assets without forcing a JSON content type", async () => {
    const inspection = {
      import_id: "11111111-1111-4111-8111-111111111111",
      project_id: "project/1",
      image_sha256: "a".repeat(64),
      media_type: "image/png",
      size_bytes: 6,
      status: "needs_calibration",
      furniture_type: "bookcase",
      furniture_type_confidence: 0.8,
      assumptions: [],
      unknown_fields: [],
    } as const;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify(inspection),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");
    const file = new File(["pixels"], "referens.png", { type: "image/png" });

    await expect(api.inspectReferenceImage("project/1", file)).resolves.toEqual(inspection);
    const request = fetchMock.mock.calls.at(-1)?.[1];
    expect(fetchMock).toHaveBeenLastCalledWith(
      "https://api.example.test/v1/projects/project%2F1/imports/inspect",
      expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
    );
    expect(new Headers(request?.headers).has("Content-Type")).toBe(false);
    expect((request?.body as FormData).get("document")).toMatchObject({
      name: "referens.png",
      type: "image/png",
      size: 6,
    });
  });

  it("lists and uploads immutable external evidence as typed multipart data", async () => {
    const evidence = {
      id: "22222222-2222-4222-8222-222222222222",
      project_id: "project/1",
      evidence_type: "wall_anchor" as const,
      rule_id: "CB-TIP-001",
      catalog_id: "ANCHOR-M8",
      catalog_version: "2026.2",
      design_hash: "d".repeat(64),
      sha256: "e".repeat(64),
      size_bytes: 8,
      content_type: "application/pdf",
      created_by: "reviewer-1",
      expires_at: "2027-02-01T23:59:59.999Z",
      created_at: "2026-08-11T12:00:00Z",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify([evidence]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(evidence), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");

    await expect(api.listExternalEvidence("project/1")).resolves.toEqual([evidence]);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "https://api.example.test/v1/projects/project%2F1/evidence",
      expect.objectContaining({ method: "GET" }),
    );

    const document = new File(["evidence"], "forankring.pdf", { type: "application/pdf" });
    await expect(api.uploadExternalEvidence("project/1", {
      document,
      evidenceType: "wall_anchor",
      ruleId: "CB-TIP-001",
      catalogId: " ANCHOR-M8 ",
      catalogVersion: " 2026.2 ",
      designHash: "d".repeat(64),
      expiresAt: "2027-02-01T23:59:59.999Z",
    })).resolves.toEqual(evidence);

    const request = fetchMock.mock.calls.at(-1)?.[1];
    expect(new Headers(request?.headers).has("Content-Type")).toBe(false);
    const form = request?.body as FormData;
    expect(form.get("document")).toMatchObject({ name: "forankring.pdf", type: "application/pdf" });
    expect(Object.fromEntries([
      "evidence_type",
      "rule_id",
      "catalog_id",
      "catalog_version",
      "design_hash",
      "expires_at",
    ].map((field) => [field, form.get(field)]))).toEqual({
      evidence_type: "wall_anchor",
      rule_id: "CB-TIP-001",
      catalog_id: "ANCHOR-M8",
      catalog_version: "2026.2",
      design_hash: "d".repeat(64),
      expires_at: "2027-02-01T23:59:59.999Z",
    });
  });

  it("preserves a structured server block with its concrete solution", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      detail: {
        code: "TEMPLATE_CAPABILITY_BLOCKED",
        message: "Konceptmallen kan inte bli produktionsrevision.",
        solution: "Välj en konstruktionsscreenad grundmodell.",
        template_id: "cupboard",
      },
    }), { status: 409, headers: { "Content-Type": "application/json" } }));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");

    await expect(api.createVersion("project-1", DEFAULT_DESIGN_SPEC, "d".repeat(64), 0, "cupboard"))
      .rejects.toMatchObject({
        status: 409,
        code: "TEMPLATE_CAPABILITY_BLOCKED",
        solution: "Välj en konstruktionsscreenad grundmodell.",
        message: expect.stringContaining("Lösning: Välj en konstruktionsscreenad grundmodell."),
      });
  });

  it("accepts only complete signed relative artifact paths and discards external URLs", async () => {
    const downloadPath = `/v1/artifacts/artifact-1/download?expires=1780000000&signature=${"b".repeat(64)}`;
    const payload = [{
      id: "artifact-1",
      kind: "production_bundle",
      sha256: "a".repeat(64),
      size_bytes: 2_048,
      content_type: "application/zip",
      download_url: "https://untrusted-storage.example.test/bundle.zip?signature=fresh",
      download_path: downloadPath,
    }];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify(payload),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CustombuildApiClient("https://api.example.test/", "tenant-token");

    await expect(api.listArtifacts("job /1")).resolves.toEqual([{
      ...payload[0],
      download_url: downloadPath,
    }]);
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.example.test/v1/jobs/job%20%2F1/artifacts",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ Authorization: "Bearer tenant-token" }),
      }),
    );

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify([{
      ...payload[0],
      download_url: "javascript:alert(1)",
    }]), { status: 200, headers: { "Content-Type": "application/json" } }));
    await expect(api.listArtifacts("job-2")).resolves.toEqual([{
      ...payload[0],
      download_url: downloadPath,
    }]);

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify([{
      ...payload[0],
      sha256: "not-a-sha",
    }]), { status: 200, headers: { "Content-Type": "application/json" } }));
    await expect(api.listArtifacts("job-3")).rejects.toThrow(/ogiltig artefaktlänk/i);

    for (const invalidPath of [
      `https://evil.example/v1/artifacts/artifact-1/download?expires=1780000000&signature=${"b".repeat(64)}`,
      `//evil.example/v1/artifacts/artifact-1/download?expires=1780000000&signature=${"b".repeat(64)}`,
      `/v1/artifacts/other/download?expires=1780000000&signature=${"b".repeat(64)}`,
      `/v1/artifacts/artifact-1/download?expires=1780000000&signature=${"b".repeat(64)}&next=evil`,
      "/v1/artifacts/artifact-1/download?expires=1780000000&signature=unsigned",
    ]) {
      fetchMock.mockResolvedValueOnce(new Response(JSON.stringify([{
        ...payload[0],
        download_path: invalidPath,
      }]), { status: 200, headers: { "Content-Type": "application/json" } }));
      await expect(api.listArtifacts("job-invalid")).rejects.toThrow(/signerad artefaktsökväg/i);
    }
  });

  it("downloads only authenticated same-origin bytes whose headers, size and SHA-256 agree", async () => {
    const content = new TextEncoder().encode("verified review bundle");
    const sha256 = await sha256Hex(content);
    const artifact = {
      id: "artifact-1",
      kind: "production_bundle",
      sha256,
      size_bytes: content.byteLength,
      content_type: "application/zip",
      download_url: "https://untrusted-storage.example.test/bundle.zip",
      download_path: `/v1/artifacts/artifact-1/download?expires=1780000000&signature=${"c".repeat(64)}`,
    } satisfies import("./api-client").ArtifactRead;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      artifactResponse(content, artifact),
    );
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");

    const blob = await api.downloadArtifact(artifact);

    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toBe("application/zip");
    expect(Array.from(new Uint8Array(await blob.arrayBuffer()))).toEqual(Array.from(content));
    expect(fetchMock).toHaveBeenCalledWith(
      `https://api.example.test${artifact.download_path}`,
      {
        method: "GET",
        headers: {
          Accept: "application/zip",
          Authorization: "Bearer tenant-token",
        },
        cache: "no-store",
        redirect: "error",
        signal: undefined,
      },
    );
  });

  it("rejects a tampered artifact body even when every response header claims the expected digest", async () => {
    const expected = new TextEncoder().encode("expected bytes");
    const tampered = new TextEncoder().encode("tampered bytes");
    expect(tampered.byteLength).toBe(expected.byteLength);
    const artifact = {
      id: "artifact-2",
      kind: "production_bundle",
      sha256: await sha256Hex(expected),
      size_bytes: expected.byteLength,
      content_type: "application/zip",
      download_url: "ignored",
      download_path: `/v1/artifacts/artifact-2/download?expires=1780000000&signature=${"d".repeat(64)}`,
    } satisfies import("./api-client").ArtifactRead;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      artifactResponse(tampered, artifact),
    );
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");

    await expect(api.downloadArtifact(artifact)).rejects.toThrow(/SHA-256-identiteten/i);

    fetchMock.mockResolvedValueOnce(artifactResponse(tampered.slice(1), artifact));
    await expect(api.downloadArtifact(artifact)).rejects.toThrow(/faktiska storlek/i);
  });

  it.each([
    ["missing Content-Length", { headers: { "Content-Length": undefined } }],
    ["non-canonical Content-Length", { headers: { "Content-Length": "04" } }],
    ["wrong Content-Type", { headers: { "Content-Type": "application/octet-stream" } }],
    ["missing Digest", { headers: { Digest: undefined } }],
    ["wrong ETag", { headers: { ETag: `"${"e".repeat(64)}"` } }],
    ["HTTP error", { status: 409 }],
    ["HTTP redirect", { redirected: true }],
    ["cross-origin response", { url: "https://evil.example/bundle.zip" }],
  ])("rejects artifact download metadata: %s", async (_name, overrides) => {
    const content = new TextEncoder().encode("safe");
    const artifact = {
      id: "artifact-3",
      kind: "production_bundle",
      sha256: await sha256Hex(content),
      size_bytes: content.byteLength,
      content_type: "application/zip",
      download_url: "ignored",
      download_path: `/v1/artifacts/artifact-3/download?expires=1780000000&signature=${"f".repeat(64)}`,
    } satisfies import("./api-client").ArtifactRead;
    vi.spyOn(globalThis, "fetch").mockResolvedValue(artifactResponse(content, artifact, overrides));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");

    await expect(api.downloadArtifact(artifact)).rejects.toThrow(ApiError);
  });

  it("binds release confirmation and rejects incomplete release evidence", async () => {
    const release = {
      release_id: "release-1",
      release_number: "R7",
      status: "released",
      manifest_sha256: "f".repeat(64),
      release_kind: "design_review",
      machine_use: "validation_only",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify(release),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");

    await expect(api.releaseVersion("project/1", 3, "R7")).resolves.toEqual(release);
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.example.test/v1/projects/project%2F1/versions/3/release",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ release_number: "R7", confirmation: "RELEASE" }),
      }),
    );

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
      ...release,
      machine_use: "production",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    await expect(api.releaseVersion("project-1", 3, "R8")).rejects.toThrow(
      /ofullständigt frisläppningsbevis/i,
    );
  });

  it("shows FastAPI validation details instead of a generic 422 message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      detail: [{
        loc: ["body", "warning_overrides", 2, "rule_id"],
        msg: "String should match pattern '^CB-[A-Z]+-[0-9]{3}$'",
      }],
    }), { status: 422, headers: { "Content-Type": "application/json" } }));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");

    await expect(api.listProjects()).rejects.toThrow(
      /warning_overrides → 2 → rule_id.*String should match pattern/i,
    );
  });
});
