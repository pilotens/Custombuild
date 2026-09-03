import { describe, expect, it } from "vitest";
import { localDesignHash } from "./design-engine";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "./design-types";
import {
  WorkshopProductionContextError,
  exactMillimetreTextToMicrometres,
  micrometresToMillimetreText,
  parseRevisionProductionContext,
  parseWorkshopProductionContext,
  productionContextFromDesignSpec,
  productionContextsEqual,
  type WorkshopProductionContext,
} from "./workshop-production-context";

function validContext(): WorkshopProductionContext {
  return {
    stock_profiles: [
      {
        role: "carcass",
        declaration_authority: "CLIENT_DECLARED",
        supplier_profile_id: "supplier-birch-18",
        supplier_profile_version: "batch-2026-09",
        material_id: "birch-plywood",
        material_version: "screening-2026.1",
        sheet_width_um: 2_440_000,
        sheet_height_um: 1_220_000,
        thickness_um: 17_800,
        sheet_count: 4,
        trim_margin_um: 10_000,
        kerf_um: 6_000,
        grain_direction: "X",
        allow_rotation: false,
        defect_zones: [],
        fixture_keep_out_zones: [
          { x_um: 0, y_um: 0, width_um: 40_000, height_um: 1_220_000 },
        ],
      },
      {
        role: "back",
        declaration_authority: "CLIENT_DECLARED",
        supplier_profile_id: "supplier-birch-6",
        supplier_profile_version: "batch-2026-09",
        material_id: "birch-plywood-6",
        material_version: "screening-2026.1",
        sheet_width_um: 2_440_000,
        sheet_height_um: 1_220_000,
        thickness_um: 6_000,
        sheet_count: 2,
        trim_margin_um: 10_000,
        kerf_um: 6_000,
        grain_direction: "X",
        allow_rotation: false,
        defect_zones: [],
        fixture_keep_out_zones: [],
      },
    ],
    two_sided_registrations: [
      {
        stock_role: "carcass",
        sheet_index: 0,
        declaration_authority: "CLIENT_DECLARED",
        flip_axis: "X",
        fixture_method_id: "supplier-pin-fixture-v1",
        fixture_method_version: "2026.09",
        pin_diameter_um: 10_000,
        position_tolerance_um: 1_000,
        pins: [
          { x_um: 80_000, y_um: 30_000 },
          { x_um: 2_360_000, y_um: 30_000 },
        ],
      },
    ],
  };
}

describe("exact workshop production context", () => {
  it("converts exact millimetres without binary floating-point multiplication", () => {
    expect(exactMillimetreTextToMicrometres("256.001")).toBe(256_001);
    expect(exactMillimetreTextToMicrometres("2.56001e2")).toBe(256_001);
    expect(micrometresToMillimetreText(256_001)).toBe("256.001");
    expect(() => exactMillimetreTextToMicrometres("397.1255"))
      .toThrow(/högst tre decimaler/i);
    expect(() => exactMillimetreTextToMicrometres("NaN")).toThrow();
  });

  it("keeps the default path explicitly stockless", () => {
    expect(productionContextFromDesignSpec(DEFAULT_DESIGN_SPEC)).toEqual({
      stock_width_mm: 2_440,
      stock_height_mm: 1_220,
      stock_count: 4,
      back_stock_width_mm: 2_440,
      back_stock_height_mm: 1_220,
      back_stock_count: 2,
      machine_profile_id: "custombuild-router-1325-linuxcnc",
    });
  });

  it("binds profiles to server-owned design material, version, thickness and sheet dimensions", () => {
    const parsed = parseWorkshopProductionContext(validContext(), DEFAULT_DESIGN_SPEC);
    expect(parsed).toEqual(validContext());
    const spec: DesignSpec = { ...DEFAULT_DESIGN_SPEC, workshop_context: parsed };
    const production = productionContextFromDesignSpec(spec);

    expect(production.stock_profiles).toHaveLength(2);
    expect(production.two_sided_registrations?.[0]?.pins[1]).toEqual({
      x_um: 2_360_000,
      y_um: 30_000,
    });
    expect(parseRevisionProductionContext(production, spec)).toEqual(production);
    expect(productionContextsEqual(production, spec)).toBe(true);
  });

  it("binds a 17.6 mm design measurement to exactly 17600 micrometres", () => {
    const context = validContext();
    context.stock_profiles[0]!.thickness_um = 17_600;
    const spec: DesignSpec = { ...DEFAULT_DESIGN_SPEC, measured_thickness_mm: 17.6 };

    const parsed = parseWorkshopProductionContext(context, spec);
    expect(parsed?.stock_profiles[0]?.thickness_um).toBe(17_600);
    expect(productionContextFromDesignSpec({ ...spec, workshop_context: parsed }).stock_profiles?.[0]?.thickness_um)
      .toBe(17_600);

    const staleContext = validContext();
    expect(() => parseWorkshopProductionContext(staleContext, spec))
      .toThrow(/profilen matchar inte designens frysta material och skivmått/i);
  });

  it("binds a 5.8 mm back measurement to exactly 5800 micrometres", () => {
    const context = validContext();
    context.stock_profiles[1]!.thickness_um = 5_800;
    const spec: DesignSpec = {
      ...DEFAULT_DESIGN_SPEC,
      measured_back_thickness_mm: 5.8,
    };

    const parsed = parseWorkshopProductionContext(context, spec);
    expect(parsed?.stock_profiles[1]?.thickness_um).toBe(5_800);
    expect(productionContextFromDesignSpec({ ...spec, workshop_context: parsed }).stock_profiles?.[1]?.thickness_um)
      .toBe(5_800);

    const staleContext = validContext();
    expect(() => parseWorkshopProductionContext(staleContext, spec))
      .toThrow(/back-profilen matchar inte designens frysta material och skivmått/i);
  });

  it("canonicalizes profile, zone and registration order while preserving pin order", () => {
    const context = validContext();
    const carcass = context.stock_profiles[0]!;
    carcass.fixture_keep_out_zones = [
      { x_um: 100_000, y_um: 50_000, width_um: 10_000, height_um: 10_000 },
      { x_um: 0, y_um: 0, width_um: 40_000, height_um: 1_220_000 },
    ];
    context.stock_profiles.reverse();
    context.two_sided_registrations = [
      {
        stock_role: "carcass",
        sheet_index: 1,
        declaration_authority: "CLIENT_DECLARED",
        flip_axis: "X",
        fixture_method_id: "fixture-2",
        fixture_method_version: "v1",
        pin_diameter_um: 10_000,
        position_tolerance_um: 1_000,
        pins: [{ x_um: 220_000, y_um: 220_000 }, { x_um: 420_000, y_um: 220_000 }],
      },
      context.two_sided_registrations![0]!,
    ];

    const parsed = parseWorkshopProductionContext(context, DEFAULT_DESIGN_SPEC)!;
    expect(parsed.stock_profiles.map((profile) => profile.role)).toEqual(["carcass", "back"]);
    expect(parsed.stock_profiles[0]!.fixture_keep_out_zones[0]!.x_um).toBe(0);
    expect(parsed.two_sided_registrations?.map((row) => row.sheet_index)).toEqual([0, 1]);
    expect(parsed.two_sided_registrations?.[1]?.pins).toEqual([
      { x_um: 220_000, y_um: 220_000 },
      { x_um: 420_000, y_um: 220_000 },
    ]);
  });

  it.each([
    ["unknown field", () => parseWorkshopProductionContext({ ...validContext(), extra: true }, DEFAULT_DESIGN_SPEC)],
    ["profile mismatch", () => {
      const value = validContext();
      value.stock_profiles[0]!.sheet_width_um = 2_439_999;
      return parseWorkshopProductionContext(value, DEFAULT_DESIGN_SPEC);
    }],
    ["duplicate role", () => {
      const value = validContext();
      value.stock_profiles[1] = { ...value.stock_profiles[0]!, supplier_profile_id: "duplicate-role" };
      return parseWorkshopProductionContext(value, DEFAULT_DESIGN_SPEC);
    }],
    ["zone outside stock", () => {
      const value = validContext();
      value.stock_profiles[0]!.defect_zones = [
        { x_um: 2_430_000, y_um: 0, width_um: 20_000, height_um: 10_000 },
      ];
      return parseWorkshopProductionContext(value, DEFAULT_DESIGN_SPEC);
    }],
    ["duplicate pins", () => {
      const value = validContext();
      value.two_sided_registrations![0]!.pins[1] = { ...value.two_sided_registrations![0]!.pins[0]! };
      return parseWorkshopProductionContext(value, DEFAULT_DESIGN_SPEC);
    }],
    ["sheet outside count", () => {
      const value = validContext();
      value.two_sided_registrations![0]!.sheet_index = 4;
      return parseWorkshopProductionContext(value, DEFAULT_DESIGN_SPEC);
    }],
    ["invented declaration authority", () => {
      const value = validContext() as unknown as { stock_profiles: Array<Record<string, unknown>> };
      value.stock_profiles[0]!.declaration_authority = "SERVER_VERIFIED";
      return parseWorkshopProductionContext(value, DEFAULT_DESIGN_SPEC);
    }],
    ["kerf below six millimetres", () => {
      const value = validContext();
      value.stock_profiles[0]!.kerf_um = 5_999;
      return parseWorkshopProductionContext(value, DEFAULT_DESIGN_SPEC);
    }],
    ["tolerance equal to pin radius", () => {
      const value = validContext();
      value.two_sided_registrations![0]!.position_tolerance_um = 5_000;
      return parseWorkshopProductionContext(value, DEFAULT_DESIGN_SPEC);
    }],
    ["pin footprint outside the sheet", () => {
      const value = validContext();
      value.two_sided_registrations![0]!.pins[0]!.y_um = 5_999;
      return parseWorkshopProductionContext(value, DEFAULT_DESIGN_SPEC);
    }],
    ["pin footprint intersecting a declared keep-out", () => {
      const value = validContext();
      value.two_sided_registrations![0]!.pins[0]!.x_um = 45_000;
      return parseWorkshopProductionContext(value, DEFAULT_DESIGN_SPEC);
    }],
  ])("fails closed for %s", (_label, run) => {
    expect(run).toThrow(WorkshopProductionContextError);
  });

  it("enforces the exact pairwise usable baseline boundary", () => {
    const exact = validContext();
    exact.two_sided_registrations![0]!.pins = [
      { x_um: 80_000, y_um: 30_000 },
      { x_um: 192_000, y_um: 30_000 },
    ];
    expect(parseWorkshopProductionContext(exact, DEFAULT_DESIGN_SPEC))
      .toEqual(exact);

    const short = validContext();
    short.two_sided_registrations![0]!.pins = [
      { x_um: 80_000, y_um: 30_000 },
      { x_um: 191_999, y_um: 30_000 },
    ];
    expect(() => parseWorkshopProductionContext(short, DEFAULT_DESIGN_SPEC))
      .toThrow(/100 mm användbar registreringsbaslinje/i);
  });

  it("does not change the geometry hash when only workshop declarations change", () => {
    expect(localDesignHash({ ...DEFAULT_DESIGN_SPEC, workshop_context: validContext() }))
      .toBe(localDesignHash(DEFAULT_DESIGN_SPEC));
  });

  it("rejects nested tampering and unexpected revision fields", () => {
    const spec: DesignSpec = { ...DEFAULT_DESIGN_SPEC, workshop_context: validContext() };
    const production = productionContextFromDesignSpec(spec);
    const tampered = structuredClone(production);
    tampered.stock_profiles![0]!.kerf_um += 1;
    expect(productionContextsEqual(tampered, spec)).toBe(false);
    expect(productionContextsEqual({ ...production, unexpected: true }, spec)).toBe(false);
  });
});
