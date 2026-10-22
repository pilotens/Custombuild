import { describe, expect, it } from "vitest";
import { type DesignSpec, type RuleEvaluation } from "@/lib/design-types";
import {
  validationPresentation,
  validationPreviewFieldLabel,
  validationPreviewValue,
} from "@/lib/validation-presentation";

function evaluation(patch: Partial<RuleEvaluation> = {}): RuleEvaluation {
  return {
    rule_id: "UNKNOWN-001",
    rule_version: "1.0.0",
    status: "BLOCK",
    title: "Kontroll från servern",
    summary: "Underlag saknas för den här kontrollen.",
    calculation: "Saknat underlag",
    assumptions: [],
    affected_part_ids: [],
    ...patch,
  };
}

describe("validationPresentation", () => {
  it.each([
    "GEO-001", "BASE-SUPPORT-001", "CB-SUPPORT-001", "STR-DEF-001",
    "CB-DEFLECTION-001", "CB-BENDING-001", "STAB-RACK-001", "CB-STABILITY-001",
    "STR-TOPO-001", "PART-CUSTOM-001",
  ])("keeps the known %s construction check in design", (rule_id) => {
    const result = validationPresentation(evaluation({ rule_id }));
    expect(result.category).toBe("design");
    expect(result.title).not.toBe("Kontroll från servern");
    expect(result.summary.length).toBeLessThanOrEqual(130);
  });

  it.each([
    "DFM-STOCK-001", "DFM-MACHINE-001", "DFM-GRAIN-001", "STAB-TIP-002",
    "CB-TIP-001", "CB-JOINT-001", "CB-HARDWARE-001",
  ])("keeps the known %s evidence check in workshop", (rule_id) => {
    const result = validationPresentation(evaluation({ rule_id, status: "WARNING" }));
    expect(result.category).toBe("workshop");
    expect(result.title).not.toBe("Kontroll från servern");
    expect(result.summary.length).toBeLessThanOrEqual(130);
  });

  it.each(["UNKNOWN-001", "DFM-STOCK-999", "CB-TIP-001-new", "constructor", "toString"])(
    "preserves unknown %s even if its title resembles a known issue",
    (rule_id) => {
      const item = evaluation({ rule_id, title: "Tipprisk och väggförankring" });
      expect(validationPresentation(item)).toEqual({
        title: item.title,
        summary: item.summary,
        category: "design",
      });
    },
  );

  it.each(["STR-DEF-001", "DFM-STOCK-001", "CB-JOINT-001", "CB-TIP-001"])(
    "preserves the actual PASS result for %s without claiming production approval",
    (rule_id) => {
      const item = evaluation({ rule_id, status: "PASS", summary: "Ingen hylla att kontrollera." });
      expect(validationPresentation(item)).toMatchObject({ title: item.title, summary: item.summary });
    },
  );

  it("distinguishes exact oversized stock from insufficient placement capacity", () => {
    expect(validationPresentation(evaluation({
      rule_id: "DFM-STOCK-001",
      summary: "23 delar ryms inte i valt skivformat.",
    })).title).toBe("Skivformatet är för litet");
    expect(validationPresentation(evaluation({
      rule_id: "DFM-STOCK-001",
      summary: "23 delar kan inte placeras inom valda materialseparerade skivor.",
    })).title).toBe("Materialet räcker inte");
  });

  it("does not describe a server near-limit deflection warning as exceeding the limit", () => {
    const result = validationPresentation(evaluation({
      rule_id: "CB-DEFLECTION-001",
      status: "WARNING",
      calculated_value: 850,
      allowed_value: 1000,
      unit: "µm",
    }));
    expect(result.title).toBe("Kontrollera hyllornas stöd");
    expect(result.summary).not.toMatch(/för mycket|överskrid/);
  });

  it("does not infer an oversized part from utilization, part count or unrelated text", () => {
    expect(validationPresentation(evaluation({
      rule_id: "DFM-STOCK-001",
      calculated_value: 42,
      unit: "%",
      summary: "Serverns lagerkontroll behöver kompletteras.",
      affected_part_ids: ["shelf-1-bay-1", "side-left"],
    }))).toEqual({
      title: "Kontrollera materialet",
      summary: "Alla delar ryms inte i det valda materialet.",
      category: "workshop",
    });
  });

  it("uses the exact shelf-type diagnostic without declaring the joint solved", () => {
    const item = evaluation({
      rule_id: "CB-JOINT-001",
      diagnostics: [{ label: "hylltyp", value: "adjustable" }],
    });
    expect(validationPresentation(item)).toMatchObject({
      title: "Kontrollera hyllbärarna",
      category: "workshop",
    });
    expect(validationPresentation({ ...item, diagnostics: [{ label: "hylltyp", value: "fixed" }] }).title)
      .toBe("Kontrollera infästningen");
    expect(item.status).toBe("BLOCK");
  });
});

describe("validation preview formatting", () => {
  it.each([
    ["divider_count", "Avdelare"],
    ["reinforcement_mode", "Förstärkning"],
    ["bay_width_ratios", "Fackens fördelning"],
    ["part_overrides", "Individuella deländringar"],
    ["schema_version", "schema_version"],
  ] satisfies Array<[keyof DesignSpec, string]>)("labels %s without exposing implementation jargon", (field, label) => {
    expect(validationPreviewFieldLabel(field)).toBe(label);
  });

  it.each([
    ["divider_count", 3, "3 st"],
    ["measured_thickness_mm", 17.801, "17,801 mm"],
    ["load_per_shelf_kg", 32.25, "32,25 kg"],
    ["fixed_shelves", true, "Ja"],
    ["back_panel", false, "Nej"],
    ["reinforcement_mode", "auto", "Automatiskt"],
    ["reinforcement_mode", "manual", "Manuellt"],
    ["bay_sizing_mode", "count", "Antal fack"],
    ["bay_width_ratios", [], "Jämn fördelning"],
    ["shelf_height_ratios", [0.25, 0.75], "0,25 / 0,75"],
    ["removed_part_ids", [], "Inga borttagna delar"],
    ["removed_part_ids", ["side-left", "top"], "2 borttagna delar"],
    ["part_overrides", {}, "Inga individuella ändringar"],
    ["part_overrides", { "side-left": { width_mm: 450 } }, "1 individuell deländring"],
    ["topology_baseline", undefined, "Inte angivet"],
    ["reinforcement_mode", "future-value", "future-value"],
    ["revision", 9, "9"],
    ["depth_mm", Number.NaN, "NaN"],
  ] satisfies Array<[keyof DesignSpec, unknown, string]>)("formats %s (%j) for people", (field, value, expected) => {
    expect(validationPreviewValue(field, value)).toBe(expected);
  });

  it("does not mutate complex values and leaves unfamiliar values intact", () => {
    const value = { future: "value" };
    expect(validationPreviewValue("workshop_context", value)).toBe('{"future":"value"}');
    expect(value).toEqual({ future: "value" });
  });
});
