import { describe, expect, it } from "vitest";
import { resolveDesign } from "@/lib/design-engine";
import { DEFAULT_DESIGN_SPEC, type RuleEvaluation } from "@/lib/design-types";
import {
  automaticValidationFix,
  automaticValidationFixPreview,
  validationGuidance,
} from "@/lib/validation-guidance";
import { parseLocalDesignSpec } from "@/lib/workspace-design-envelope";

function evaluation(patch: Partial<RuleEvaluation> = {}): RuleEvaluation {
  return {
    rule_id: "UNKNOWN-001",
    rule_version: "1.0.0",
    status: "BLOCK",
    title: "Okänd konstruktionskontroll",
    summary: "Ett verifierat värde saknas.",
    calculation: "saknas",
    assumptions: [],
    affected_part_ids: [],
    ...patch,
  };
}

describe("validationGuidance", () => {
  it.each([
    ["BASE-SUPPORT-001", "Lodrät lastväg genom underskåpen"],
    ["STR-DEF-001", "Hyllnedböjning"],
    ["CB-BENDING-001", "Böjspänning i hylla"],
    ["STAB-RACK-001", "Sidostabilitet"],
    ["STAB-TIP-002", "Tipprisk och väggförankring"],
    ["CB-HARDWARE-001", "Beslag och borrbild för underskåp"],
    ["DFM-GRAIN-001", "Fiberriktning för skivmaterial"],
    ["DFM-MACHINE-001", "Maskinens arbetsområde"],
    ["DFM-STOCK-001", "Delar ryms i råmaterial"],
    ["CB-JOINT-001", "Lokalt upplag i hyllspår och hyllbärare"],
    ["STR-TOPO-001", "Sammanhängande bärande geometri"],
    ["PART-CUSTOM-001", "Individuellt ändrade delar"],
    ["GEO-001", "Giltig parametrisk geometri"],
  ])("gives %s a problem, consequence, solution and exact next input", (ruleId, title) => {
    const guidance = validationGuidance(evaluation({ rule_id: ruleId, title }));

    expect(guidance.problem.length).toBeGreaterThan(0);
    expect(guidance.impact.length).toBeGreaterThan(20);
    expect(guidance.solution.length).toBeGreaterThan(20);
    expect(guidance.requiredInput.length).toBeGreaterThan(20);
    expect(guidance.target.control.length).toBeGreaterThan(3);
  });

  it.each([
    ["DFM-MACHINE-001", "Maskinens arbetsområde"],
    ["DFM-STOCK-001", "Delar ryms i råmaterial"],
  ])("routes the blocking %s issue to stockless review without a geometry patch", (ruleId, title) => {
    const item = evaluation({
      rule_id: ruleId,
      title,
      affected_part_ids: ["side-left"],
      suggestion: {
        action: "create_stockless_review_package",
        label: "Skapa lagerobundet granskningsunderlag",
        value: true,
        explanation: "Behåll verkliga mått och blockera nesting och CAM.",
      },
    });
    const guidance = validationGuidance(item);

    expect(automaticValidationFix(item, DEFAULT_DESIGN_SPEC)).toBeUndefined();
    expect(automaticValidationFixPreview(item, DEFAULT_DESIGN_SPEC)).toBeUndefined();
    expect(guidance.target).toEqual({
      kind: "production",
      control: "Lagerobundet granskningspaket",
      gate: "stockless_review",
    });
    expect(guidance.solution).toContain("Behåll verkliga mått");
    expect(`${guidance.solution} ${guidance.requiredInput}`).not.toMatch(/5\s?000|5\s?100|2\s?500|2\s?600|storformat/i);
  });

  it.each([
    ["DFM-STOCK-001", "Delar ryms i råmaterial"],
    ["DFM-MACHINE-001", "Maskinens arbetsområde"],
  ] as const)("does not route %s without the exact stockless-review contract", (ruleId, title) => {
    const item = evaluation({
      rule_id: ruleId,
      title,
      affected_part_ids: ["side-left"],
    });
    const guidance = validationGuidance(item);

    expect(guidance.target.kind).toBe("none");
    expect(guidance.solution).not.toMatch(/lagerobundet/i);
    expect(automaticValidationFix(item, DEFAULT_DESIGN_SPEC)).toBeUndefined();
    expect(automaticValidationFixPreview(item, DEFAULT_DESIGN_SPEC)).toBeUndefined();
  });

  it("repairs invalid wall-library geometry with finite, mutually aligned dimensions", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      nominal_thickness_mm: Number.POSITIVE_INFINITY,
      measured_thickness_mm: Number.POSITIVE_INFINITY,
      width_mm: Number.NaN,
      height_mm: Number.NaN,
      depth_mm: Number.NaN,
      base_cabinet_height_mm: Number.NaN,
      base_cabinet_depth_mm: Number.NaN,
      shelf_count: Number.NaN,
      divider_count: Number.NaN,
      base_cabinet_count: Number.NaN,
    };
    const geoEvaluation = evaluation({ rule_id: "GEO-001", title: "Giltig parametrisk geometri" });
    const fix = automaticValidationFix(geoEvaluation, spec);

    expect(validationGuidance(geoEvaluation).requiredInput).toContain("0–40 hyllor");
    expect(fix).toBeDefined();
    const numericValues = [
      fix?.patch.width_mm,
      fix?.patch.height_mm,
      fix?.patch.depth_mm,
      fix?.patch.base_cabinet_height_mm,
      fix?.patch.base_cabinet_depth_mm,
      fix?.patch.shelf_count,
      fix?.patch.divider_count,
      fix?.patch.base_cabinet_count,
    ];
    expect(numericValues.every((value) => typeof value === "number" && Number.isFinite(value))).toBe(true);
    expect(fix?.patch.measured_thickness_mm).toBe(DEFAULT_DESIGN_SPEC.measured_thickness_mm);
    expect(fix?.patch.base_cabinet_depth_mm).toBe(fix?.patch.depth_mm);
    expect(fix?.patch.base_cabinet_depth_mm).toBeLessThanOrEqual(1_200);
    expect(fix?.patch.base_cabinet_height_mm).toBeLessThanOrEqual(2_000);
    expect(fix?.patch.height_mm).toBeGreaterThan(
      (fix?.patch.base_cabinet_height_mm as number)
      + (fix?.patch.measured_thickness_mm as number)
      + 200,
    );
    expect(fix?.patch.bay_sizing_mode).toBe("count");
    expect(fix?.patch.reinforcement_mode).toBe("manual");

    const repaired = parseLocalDesignSpec({ ...spec, ...fix?.patch });
    expect(resolveDesign(repaired).rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.status)
      .toBe("PASS");
  });

  it("preserves a B1-valid full-envelope design while producing a valid GEO patch", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      width_mm: 6_000,
      height_mm: 4_000,
      depth_mm: 1_200,
      shelf_count: 40,
      divider_count: 16,
      load_per_shelf_kg: 500,
      base_cabinet_count: 17,
      base_cabinet_height_mm: 1_000,
      base_cabinet_depth_mm: 1_200,
      reinforcement_mode: "manual" as const,
    };
    const fix = automaticValidationFix(
      evaluation({ rule_id: "GEO-001", title: "Giltig parametrisk geometri" }),
      spec,
    );

    expect(fix?.patch).toEqual(expect.objectContaining({
      width_mm: 6_000,
      height_mm: 4_000,
      depth_mm: 1_200,
      shelf_count: 40,
      divider_count: 16,
      base_cabinet_count: 17,
      base_cabinet_height_mm: 1_000,
      base_cabinet_depth_mm: 1_200,
    }));
    const repaired = parseLocalDesignSpec({ ...spec, ...fix?.patch });
    expect(resolveDesign(repaired).rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.status)
      .toBe("PASS");
  });

  it("realigns the lower cabinets when adding deterministic shelf supports", () => {
    const fix = automaticValidationFix(
      evaluation({
        rule_id: "STR-DEF-001",
        title: "Hyllnedböjning",
        suggestion: {
          action: "set_divider_count",
          label: "Inför 4 vertikala avdelare totalt",
          value: 4,
          explanation: "Korta spännvidden.",
        },
      }),
      { ...DEFAULT_DESIGN_SPEC, furniture_type: "wall_library" },
    );

    expect(fix?.patch).toEqual(expect.objectContaining({
      divider_count: 4,
      base_cabinet_count: 5,
      bay_sizing_mode: "count",
      reinforcement_mode: "manual",
      bay_width_ratios: [],
      symmetry_locked: true,
    }));
  });

  it("previews only exact DesignSpec fields whose values will change", () => {
    const item = evaluation({
      rule_id: "STR-DEF-001",
      title: "Hyllnedböjning",
      suggestion: {
        action: "set_divider_count",
        label: "Inför 2 vertikala avdelare",
        value: 2,
        explanation: "Korta spännvidden.",
      },
    });
    const preview = automaticValidationFixPreview(item, {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 0,
      bay_sizing_mode: "count",
      bay_width_ratios: [],
      symmetry_locked: true,
      reinforcement_mode: "auto",
    });

    expect(preview?.changes).toEqual([
      { field: "reinforcement_mode", before: "auto", after: "manual" },
      { field: "divider_count", before: 0, after: 2 },
    ]);
    expect(preview?.patch).toEqual(expect.objectContaining({
      reinforcement_mode: "manual",
      divider_count: 2,
    }));
  });

  it("omits no-op and unsupported previews instead of inventing changes", () => {
    const alreadyApplied = evaluation({
      rule_id: "STAB-RACK-001",
      title: "Sidostabilitet",
      suggestion: {
        action: "enable_back",
        label: "Aktivera bakstycke",
        value: true,
        explanation: "Bakstycket staggar stommen.",
      },
    });
    const unsupported = evaluation({
      rule_id: "STAB-TIP-002",
      title: "Tipprisk och väggförankring",
      suggestion: {
        action: "manual_review",
        label: "Registrera underlag",
        value: false,
        explanation: "Externt underlag krävs.",
      },
    });

    expect(automaticValidationFixPreview(alreadyApplied, { ...DEFAULT_DESIGN_SPEC, back_panel: true })).toBeUndefined();
    expect(automaticValidationFixPreview(unsupported, DEFAULT_DESIGN_SPEC)).toBeUndefined();
  });

  it("does not substitute a made-up numeric limit when the rule has none", () => {
    const guidance = validationGuidance(evaluation({
      rule_id: "STR-DEF-001",
      title: "Hyllnedböjning",
      allowed_value: undefined,
      unit: undefined,
      diagnostics: undefined,
    }));

    expect(guidance.requiredInput).toContain("saknar en uttrycklig numerisk gräns");
    expect(guidance.requiredInput).not.toContain("5 mm");
  });

  it("leaves target-width mode when restoring an explicit topology baseline", () => {
    const fix = automaticValidationFix(
      evaluation({ rule_id: "PART-CUSTOM-001", title: "Individuellt ändrade delar" }),
      {
        ...DEFAULT_DESIGN_SPEC,
        bay_sizing_mode: "target_width",
        target_bay_width_mm: 300,
        topology_baseline: {
          divider_count: 3,
          shelf_count: 5,
          base_cabinet_count: 0,
          bay_width_ratios: [0.2, 0.3, 0.3, 0.2],
          shelf_height_ratios: [],
          reinforcement_mode: "auto",
        },
      },
    );

    expect(fix?.patch).toEqual(expect.objectContaining({
      divider_count: 3,
      bay_sizing_mode: "count",
      reinforcement_mode: "manual",
    }));
  });

  it("clamps base-module repairs at seventeen while dividers remain capped at sixteen", () => {
    const baseFix = automaticValidationFix(
      evaluation({
        rule_id: "BASE-SUPPORT-001",
        title: "Lodrät lastväg genom underskåpen",
        suggestion: {
          action: "align_base_cabinets",
          label: "Rikta underskåpen",
          value: 99,
          explanation: "Rikta varje modul mot ett övre stöd.",
        },
      }),
      { ...DEFAULT_DESIGN_SPEC, furniture_type: "wall_library" },
    );
    const geometryFix = automaticValidationFix(
      evaluation({ rule_id: "GEO-001", title: "Giltig parametrisk geometri" }),
      {
        ...DEFAULT_DESIGN_SPEC,
        furniture_type: "wall_library",
        divider_count: 99,
        base_cabinet_count: 99,
      },
    );

    expect(baseFix?.patch.base_cabinet_count).toBe(17);
    expect(geometryFix?.patch.divider_count).toBe(16);
    expect(geometryFix?.patch.base_cabinet_count).toBe(17);
  });

  it("removes non-finite inactive cabinet values from an open bookcase repair", () => {
    const fix = automaticValidationFix(
      evaluation({ rule_id: "GEO-001", title: "Giltig parametrisk geometri" }),
      {
        ...DEFAULT_DESIGN_SPEC,
        furniture_type: "bookcase",
        base_cabinet_count: Number.NaN,
        base_cabinet_height_mm: Number.NaN,
        base_cabinet_depth_mm: Number.NaN,
      },
    );

    expect(fix?.patch).toEqual(expect.objectContaining({
      base_cabinet_count: 0,
      base_cabinet_height_mm: 0,
      base_cabinet_depth_mm: 0,
    }));
  });

  it("offers a real automatic switch for adjustable shelves without dismissing the block", () => {
    const fix = automaticValidationFix(
      evaluation({
        rule_id: "CB-JOINT-001",
        title: "Lokalt upplag i hyllspår och hyllbärare",
        diagnostics: [{ label: "hylltyp", value: "adjustable" }],
      }),
      DEFAULT_DESIGN_SPEC,
    );

    expect(fix).toEqual(expect.objectContaining({
      label: "Byt till fasta hyllor",
      patch: { fixed_shelves: true },
    }));
  });

  it("keeps a plain DADO review-only until dry retention is verified", () => {
    const guidance = validationGuidance(evaluation({
      rule_id: "CB-JOINT-001",
      status: "WARNING",
      title: "Lokalt upplag i hyllspår och hyllbärare",
      calculated_value: 150,
      allowed_value: 1_589,
      unit: "N",
    }));

    expect(guidance.impact).toMatch(/dras isär/i);
    expect(guidance.solution).toMatch(/självlåsande torrförband|mekanisk säkring/i);
    expect(guidance.solution).toMatch(/lim, fogmassa och epoxy är förbjudna/i);
    expect(guidance.requiredInput).toMatch(/mekanisk hållning mot isärdragning/i);
    expect(guidance.target).toEqual({
      kind: "none",
      control: "Förbandsval saknas i nuvarande produktions-MVP",
    });
  });

  it.each([
    ["STAB-TIP-002", "Tipprisk och väggförankring", "väggtyp"],
    ["CB-HARDWARE-001", "Beslag och borrbild för underskåp", "SKU"],
  ])("keeps %s as an external evidence check instead of pretending it is fixed", (ruleId, title, evidence) => {
    const item = evaluation({ rule_id: ruleId, status: "WARNING", title });
    const guidance = validationGuidance(item, DEFAULT_DESIGN_SPEC);

    expect(automaticValidationFix(item, DEFAULT_DESIGN_SPEC)).toBeUndefined();
    expect(guidance.target).toEqual(expect.objectContaining({ kind: "production" }));
    expect(guidance.requiredInput.toLowerCase()).toContain(evidence.toLowerCase());
  });

  it("keeps the server grain warning explanatory without a phantom evidence action", () => {
    const item = evaluation({
      rule_id: "DFM-GRAIN-001",
      status: "WARNING",
      title: "Fiberriktning för skivmaterial",
    });
    const guidance = validationGuidance(item, DEFAULT_DESIGN_SPEC);

    expect(automaticValidationFix(item, DEFAULT_DESIGN_SPEC)).toBeUndefined();
    expect(guidance.target).toEqual(expect.objectContaining({ kind: "none" }));
    expect(guidance.solution).toMatch(/strukturerade bindning/i);
    expect(guidance.solution).toMatch(/dokument|varningsgodkännande/i);
    expect(guidance.requiredInput.toLowerCase()).toContain("fiberriktningsaxel");
  });
});
