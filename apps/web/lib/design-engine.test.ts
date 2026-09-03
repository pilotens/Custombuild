import { describe, expect, it } from "vitest";
import {
  adaptStructuralSupports,
  applySuggestion,
  balanceDesignSymmetry,
  currentEqualBayWidthMm,
  editPartParametrically,
  localDesignHash,
  mergeServerDesignWithLocalDfm,
  migrateLegacyStructuralRemovals,
  movePartVertically,
  normalizeBaseCabinetDepth,
  partsDoNotOverlap,
  removePartFromDesign,
  resolveDesign,
  restorePartCustomizations,
  setBayWidthRatio,
  setShelfHeightRatio,
  setShelfOpeningHeight,
  shelfOpeningHeights,
  targetBayLayout,
} from "./design-engine";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "./design-types";

describe("deterministic bookcase preview", () => {
  it("keeps plain DADO under manual dry-joining review in the local fallback", () => {
    const result = resolveDesign(DEFAULT_DESIGN_SPEC);
    const jointRule = result.rule_evaluations.find(
      (rule) => rule.rule_id === "CB-JOINT-001",
    );

    expect(jointRule).toMatchObject({
      rule_version: "1.3.0",
      status: "WARNING",
      title: "Lokalt upplag i hyllspår och hyllbärare",
    });
    expect(jointRule?.summary).toContain("verifierar inte permanent hållning");
    expect(jointRule?.assumptions).toContain(
      "Lim, fogmassa, epoxy och annan adhesiv låsning är förbjuden.",
    );
    expect(jointRule?.diagnostics).toContainEqual({
      label: "permanent hållning verifierad",
      value: "false",
    });
    expect(jointRule?.suggestion).toBeUndefined();
  });

  it("blocks adjustable shelves locally without versioned shelf-pin evidence", () => {
    const result = resolveDesign({ ...DEFAULT_DESIGN_SPEC, fixed_shelves: false });
    const jointRule = result.rule_evaluations.find(
      (rule) => rule.rule_id === "CB-JOINT-001",
    );

    expect(jointRule).toMatchObject({
      status: "BLOCK",
      diagnostics: expect.arrayContaining([
        { label: "hylltyp", value: "adjustable" },
      ]),
    });
    expect(jointRule?.summary).toContain("saknar versionsbunden hyllbärarkapacitet");
    expect(jointRule?.assumptions).toContain(
      "Ingen hyllbärar-SKU, katalogversion, kapacitet eller borrbild är verifierad.",
    );
  });

  it("keeps explicit legacy repair opt-in and blocks unresolved base-depth mismatches", () => {
    const legacy = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      depth_mm: 320,
      base_cabinet_depth_mm: 520,
      base_cabinet_count: 3,
      symmetry_locked: false,
    };

    expect(normalizeBaseCabinetDepth(legacy).base_cabinet_depth_mm).toBe(320);
    expect(balanceDesignSymmetry(legacy).base_cabinet_depth_mm).toBe(520);
    const unresolved = resolveDesign(legacy);
    expect(unresolved.spec.base_cabinet_depth_mm).toBe(520);
    expect(unresolved.rule_evaluations.find((rule) => rule.rule_id === "GEO-001")).toMatchObject({
      status: "BLOCK",
    });
    expect(unresolved.rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.summary)
      .toContain("djup");
  });

  it("maximizes equal bays without making them narrower than the requested clear width", () => {
    const requested = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 4_200,
      measured_thickness_mm: 17.8,
      bay_sizing_mode: "target_width" as const,
      target_bay_width_mm: 300,
      reinforcement_mode: "auto" as const,
    };

    expect(targetBayLayout(requested)).toEqual({
      bayCount: 13,
      dividerCount: 12,
      actualClearWidthMm: 303.9,
      targetMet: true,
      limitedByMaximum: false,
    });

    const adapted = adaptStructuralSupports(requested).spec;
    expect(adapted.divider_count).toBe(12);
    expect(adapted.bay_width_ratios).toEqual([]);
    expect(currentEqualBayWidthMm(adapted)).toBe(303.9);
  });

  it("recalculates width-driven bays and respects the seventeen-bay product limit", () => {
    const resized = adaptStructuralSupports({
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 2_400,
      measured_thickness_mm: 18,
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 300,
      reinforcement_mode: "auto",
    }).spec;
    expect(resized.divider_count).toBe(6);
    expect(currentEqualBayWidthMm(resized)).toBe(322.3);

    const limited = targetBayLayout({
      width_mm: 6_000,
      measured_thickness_mm: 18,
      target_bay_width_mm: 100,
    });
    expect(limited.bayCount).toBe(17);
    expect(limited.limitedByMaximum).toBe(true);
  });

  it("uses exact micrometre boundaries and clamps target width to 50–2000 mm", () => {
    expect(targetBayLayout({
      width_mm: 4_149.2,
      measured_thickness_mm: 17.8,
      target_bay_width_mm: 300,
    })).toEqual({
      bayCount: 13,
      dividerCount: 12,
      actualClearWidthMm: 300,
      targetMet: true,
      limitedByMaximum: false,
    });

    expect(targetBayLayout({
      width_mm: 6_000,
      measured_thickness_mm: 18,
      target_bay_width_mm: 1,
    }).bayCount).toBe(17);
    expect(targetBayLayout({
      width_mm: 6_000,
      measured_thickness_mm: 18,
      target_bay_width_mm: 5_000,
    }).bayCount).toBe(2);
  });

  it("enforces 200 mm clear modules when a wall library has base cabinets", () => {
    const requested = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 2_400,
      measured_thickness_mm: 18,
      target_bay_width_mm: 50,
      bay_sizing_mode: "target_width" as const,
      reinforcement_mode: "manual" as const,
      furniture_type: "wall_library" as const,
      base_cabinet_count: 3,
    };
    const layout = targetBayLayout(requested);
    const adapted = adaptStructuralSupports(requested).spec;

    expect(layout.bayCount).toBe(10);
    expect(layout.dividerCount).toBe(9);
    expect(layout.actualClearWidthMm).toBe(220.2);
    expect(layout.targetMet).toBe(true);
    expect(adapted.divider_count).toBe(9);
    expect(adapted.base_cabinet_count).toBe(10);
    expect(adapted.reinforcement_mode).toBe("manual");
  });

  it("keeps workspace sizing intent out of the local geometry hash", () => {
    const resolved = {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 3,
      reinforcement_mode: "manual" as const,
    };
    const countHash = localDesignHash({
      ...resolved,
      bay_sizing_mode: "count",
      target_bay_width_mm: 300,
    });
    const targetHash = localDesignHash({
      ...resolved,
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 777,
    });

    expect(targetHash).toBe(countHash);
    expect(localDesignHash({ ...resolved, divider_count: 4 })).not.toBe(countHash);
  });

  it("preserves manual target sizing but lets auto mode add structural supports", () => {
    const target = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 4_200,
      measured_thickness_mm: 17.8,
      load_per_shelf_kg: 32,
      bay_sizing_mode: "target_width" as const,
      target_bay_width_mm: 1_000,
      bay_width_ratios: [0.25, 0.25, 0.25, 0.25],
    };
    const manual = adaptStructuralSupports({
      ...target,
      reinforcement_mode: "manual",
    }).spec;
    const automatic = adaptStructuralSupports({
      ...target,
      reinforcement_mode: "auto",
    }).spec;

    expect(manual.divider_count).toBe(3);
    expect(manual.reinforcement_mode).toBe("manual");
    expect(manual.bay_width_ratios).toEqual([]);
    expect(automatic.divider_count).toBe(4);
    expect(automatic.reinforcement_mode).toBe("auto");
    expect(automatic.bay_width_ratios).toEqual([]);
  });

  it("leaves target sizing when a bay is customized or a divider is removed", () => {
    const target = {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 3,
      bay_sizing_mode: "target_width" as const,
      target_bay_width_mm: 300,
      reinforcement_mode: "auto" as const,
    };
    const customized = setBayWidthRatio(target, 0, 0.2);
    const removed = removePartFromDesign(target, "divider-2").spec;

    expect(customized.bay_sizing_mode).toBe("count");
    expect(customized.reinforcement_mode).toBe("manual");
    expect(removed.bay_sizing_mode).toBe("count");
    expect(removed.reinforcement_mode).toBe("manual");
  });

  it("returns identical canonical output for identical input", () => {
    const first = resolveDesign({ ...DEFAULT_DESIGN_SPEC });
    const second = resolveDesign({ ...DEFAULT_DESIGN_SPEC });

    expect(second.design_hash).toBe(first.design_hash);
    expect(second.parts).toEqual(first.parts);
    expect(second.bom).toEqual(first.bom);
    expect(second.operations).toEqual(first.operations);
    expect(partsDoNotOverlap(first.nesting)).toBe(true);
  });

  it("binds the selected back material to parts, weight, BOM and design identity", () => {
    const birch = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      back_material_id: "birch-plywood-6",
    });
    const mdf = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      back_material_id: "mdf-6",
    });
    const birchBack = birch.parts.find((part) => part.kind === "back");
    const mdfBack = mdf.parts.find((part) => part.kind === "back");

    expect(birchBack?.material_id).toBe("birch-plywood-6");
    expect(mdfBack?.material_id).toBe("mdf-6");
    expect(mdfBack?.weight_kg).toBeGreaterThan(birchBack?.weight_kg ?? 0);
    expect(mdf.bom.find((line) => line.part_ids.includes("back-panel"))?.material)
      .toBe("MDF, bakstycke");
    expect(mdf.parts.find((part) => part.kind === "side")?.material_id)
      .toBe(DEFAULT_DESIGN_SPEC.material_id);
    expect(mdf.design_hash).not.toBe(birch.design_hash);
  });

  it("segments an inset back by bay with the authoritative captured geometry", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 2,
      reinforcement_mode: "manual",
      back_panel: true,
      back_panel_type: "inset_groove",
    });
    const backs = result.parts.filter((part) => part.kind === "back");
    const firstRowShelves = result.parts.filter(
      (part) => part.kind === "shelf" && part.part_id.startsWith("shelf-1-"),
    );

    expect(backs.map((part) => part.part_id)).toEqual([
      "back-panel-bay-1",
      "back-panel-bay-2",
      "back-panel-bay-3",
    ]);
    expect(backs.map((part) => part.width_mm)).toEqual(
      firstRowShelves.map((part) => part.width_mm),
    );
    expect(backs.map((part) => part.position_mm.x)).toEqual([
      expect.closeTo(205.933, 3),
      expect.closeTo(600, 3),
      expect.closeTo(994.067, 3),
    ]);
    expect(backs.every((part) => part.depth_mm === 1_996.266)).toBe(true);
    expect(backs.every((part) => part.position_mm.y === 305)).toBe(true);
    expect(backs.every((part) => part.position_mm.z === 1_090)).toBe(true);
    expect(result.parts.filter((part) => part.kind === "divider"))
      .toEqual(expect.arrayContaining([
        expect.objectContaining({ depth_mm: 308, width_mm: 1_996.266 }),
      ]));
  });

  it("keeps a surface-mounted back as one canonical full panel", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 2,
      reinforcement_mode: "manual",
      back_panel: true,
      back_panel_type: "surface_mounted",
    });
    const backs = result.parts.filter((part) => part.kind === "back");

    expect(backs).toEqual([
      expect.objectContaining({
        part_id: "back-panel",
        width_mm: 1_200,
        depth_mm: 2_100,
        thickness_mm: 6,
        position_mm: { x: 600, y: 317, z: 1_050 },
      }),
    ]);
    expect(result.parts.filter((part) => part.kind === "side").every(
      (part) => part.depth_mm === 314,
    )).toBe(true);
    expect(result.parts.filter((part) => part.kind === "divider").every(
      (part) => part.depth_mm === 312,
    )).toBe(true);
    const rabbets = result.parts.flatMap((part) => part.features).filter(
      (feature) => feature.kind === "rabbet",
    );
    expect(rabbets).toHaveLength(4);
    expect(rabbets.every((feature) => feature.depth_mm === 5.933)).toBe(true);
    expect(result.operations.filter((operation) => operation.operation === "Falsfräsning"))
      .toHaveLength(4);
  });

  it("matches the authoritative inset-back cutter clearance in local geometry", () => {
    const withBack = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      depth_mm: 320,
      back_panel: true,
      divider_count: 1,
      reinforcement_mode: "manual",
    });
    const shelves = withBack.parts.filter((part) => part.kind === "shelf");
    const dividers = withBack.parts.filter((part) => part.kind === "divider");

    expect(shelves.length).toBeGreaterThan(0);
    expect(shelves.every((part) => part.depth_mm === 298)).toBe(true);
    expect(shelves.every((part) => part.position_mm.y === 149)).toBe(true);
    expect(dividers.every((part) => part.depth_mm === 308)).toBe(true);
    expect(dividers.every((part) => part.position_mm.y === 154)).toBe(true);
    expect([...shelves, ...dividers].every(
      (part) => part.position_mm.y - part.depth_mm / 2 === 0,
    )).toBe(true);

    const withoutBack = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      depth_mm: 320,
      back_panel: false,
      divider_count: 1,
      reinforcement_mode: "manual",
    });
    expect(
      withoutBack.parts
        .filter((part) => part.kind === "shelf" || part.kind === "divider")
        .every((part) => part.depth_mm === 318),
    ).toBe(true);
  });

  it("adds a proposed divider to model, BOM, nesting and operations", () => {
    const initial = resolveDesign(DEFAULT_DESIGN_SPEC);
    const shelfRule = initial.rule_evaluations.find((rule) => rule.rule_id === "STR-DEF-001");

    expect(shelfRule?.status).not.toBe("PASS");
    expect(shelfRule?.suggestion?.action).toBe("set_divider_count");
    if (!shelfRule) throw new Error("Shelf rule missing");

    const applied = applySuggestion(DEFAULT_DESIGN_SPEC, shelfRule);
    const reinforced = resolveDesign(applied.spec, applied.diff);

    expect(applied.spec.divider_count).toBeGreaterThan(0);
    expect(applied.spec.reinforcement_mode).toBe("auto");
    expect(reinforced.parts.some((part) => part.kind === "divider")).toBe(true);
    expect(reinforced.bom.some((line) => line.part_ids.some((partId) => partId.startsWith("divider-")))).toBe(true);
    expect(reinforced.nesting.placements.some((placement) => placement.part_id.startsWith("divider-"))).toBe(true);
    expect(reinforced.operations.some((operation) => operation.part_id.startsWith("divider-"))).toBe(true);
    expect(reinforced.rule_evaluations.find((rule) => rule.rule_id === "STR-DEF-001")?.status).toBe("PASS");
  });

  it("clears stale segmented-back customizations when divider topology changes", () => {
    const spec: DesignSpec = {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 2,
      part_overrides: {
        "back-panel-bay-3": { width_mm: 500 },
        "side-left": { depth_mm: 300 },
      },
      removed_part_ids: ["back-panel-bay-2"],
    };
    const changed = applySuggestion(spec, {
      rule_id: "TEST-TOPOLOGY",
      rule_version: "1.0.0",
      status: "WARNING",
      title: "Test",
      summary: "Ändra facktopologi",
      calculation: "test",
      assumptions: [],
      affected_part_ids: [],
      suggestion: {
        action: "set_divider_count",
        label: "Ändra",
        value: 1,
        explanation: "Test",
      },
    });

    expect(changed.spec.divider_count).toBe(1);
    expect(changed.spec.part_overrides).toEqual({
      "side-left": { depth_mm: 300 },
    });
    expect(changed.spec.removed_part_ids).toEqual([]);
    expect(resolveDesign(changed.spec).parts.filter((part) => part.kind === "back").map(
      (part) => part.part_id,
    )).toEqual(["back-panel-bay-1", "back-panel-bay-2"]);
  });

  it("automatically supports a 4200 mm loaded shelf row", () => {
    const requested = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 4_200,
      load_per_shelf_kg: 32,
      divider_count: 0,
      reinforcement_mode: "auto" as const,
    };

    const adapted = adaptStructuralSupports(requested);
    const resolved = resolveDesign(adapted.spec, adapted.diff);
    const shelfRule = resolved.rule_evaluations.find((rule) => rule.rule_id === "STR-DEF-001");

    expect(adapted.spec.divider_count).toBe(4);
    expect(adapted.diff).toEqual([
      expect.objectContaining({ field: "divider_count", before: 0, after: 4 }),
    ]);
    expect(resolved.parts.filter((part) => part.kind === "divider")).toHaveLength(4);
    expect(resolved.parts.filter((part) => part.kind === "shelf")).toHaveLength(
      requested.shelf_count * 5,
    );
    expect(shelfRule?.status).toBe("PASS");
    expect(shelfRule?.assumptions).toContain("Fri spännvidd 818.64 mm");

    const narrowed = adaptStructuralSupports({ ...adapted.spec, width_mm: 1_200 });
    expect(narrowed.spec.divider_count).toBe(1);
    expect(narrowed.diff).toEqual([
      expect.objectContaining({ field: "divider_count", before: 4, after: 1 }),
    ]);
  });

  it("keeps an unsafe manual layout visible but production-blocked", () => {
    const requested = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 4_200,
      divider_count: 0,
      reinforcement_mode: "manual" as const,
    };

    expect(adaptStructuralSupports(requested).spec.divider_count).toBe(0);
    expect(
      resolveDesign(requested).rule_evaluations.find((rule) => rule.rule_id === "STR-DEF-001")?.status,
    ).toBe("BLOCK");
  });

  it("never turns an incomplete shelf-load value into the maximum divider count", () => {
    const incomplete = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 2_400,
      shelf_count: 2,
      divider_count: 0,
      load_per_shelf_kg: Number.NaN,
    };

    expect(adaptStructuralSupports(incomplete).spec.divider_count).toBe(0);
  });

  it("keeps an automatic wall-library base on the same structural grid as the upper bays", () => {
    const adapted = adaptStructuralSupports({
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      width_mm: 4_200,
      height_mm: 2_600,
      depth_mm: 320,
      shelf_count: 5,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 320,
      base_cabinet_count: 4,
    });
    const result = resolveDesign(adapted.spec);

    expect(adapted.spec.divider_count).toBe(4);
    expect(result.parts.filter((part) => part.kind === "shelf")).toHaveLength(25);
    expect(adapted.spec.base_cabinet_count).toBe(5);
    expect(adapted.diff).toContainEqual(expect.objectContaining({
      field: "base_cabinet_count",
      before: 4,
      after: 5,
    }));
    expect(result.parts.filter((part) => part.kind === "base_side")).toHaveLength(4);
    expect(
      result.parts
        .filter((part) => part.kind === "base_side")
        .map((part) => part.part_id),
    ).toEqual(["base-side-2", "base-side-3", "base-side-4", "base-side-5"]);
    expect(result.parts.filter((part) => part.kind === "base_bottom")).toHaveLength(5);
    expect(result.parts.filter((part) => part.kind === "cabinet_front")).toHaveLength(5);
    expect(result.parts.filter((part) => part.kind === "cabinet_front").every((part) => part.position_mm.y < 20)).toBe(true);
    expect(result.parts.find((part) => part.kind === "back")?.position_mm.y).toBeGreaterThan(300);
    expect(result.parts.filter((part) => part.kind === "shelf").every((part) => part.position_mm.z > 800)).toBe(true);
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "CB-HARDWARE-001")?.status).toBe("WARNING");
    const supportRule = result.rule_evaluations.find((rule) => rule.rule_id === "BASE-SUPPORT-001");
    expect(supportRule?.status).toBe("PASS");
  });

  it("preserves custom upper bay widths through aligned underskåp", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      width_mm: 2_400,
      height_mm: 2_400,
      divider_count: 2,
      base_cabinet_count: 3,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 320,
      reinforcement_mode: "manual",
      symmetry_locked: true,
      bay_width_ratios: [0.25, 0.5, 0.25],
    });
    const dividers = result.parts.filter((part) => part.kind === "divider");
    const internalBaseSides = result.parts.filter((part) => part.kind === "base_side");

    expect(internalBaseSides.map((part) => part.position_mm.x))
      .toEqual(dividers.map((part) => part.position_mm.x));
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "BASE-SUPPORT-001")?.status)
      .toBe("PASS");
  });

  it("keeps a manually mismatched underskåpsgrid blocked and names the support positions", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      width_mm: 4_200,
      height_mm: 2_600,
      divider_count: 4,
      base_cabinet_count: 4,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 320,
      reinforcement_mode: "manual",
    });
    const supportRule = result.rule_evaluations.find((rule) => rule.rule_id === "BASE-SUPPORT-001");

    expect(supportRule).toMatchObject({
      status: "BLOCK",
      suggestion: { action: "align_base_cabinets", value: 5 },
    });
    expect(supportRule?.summary).toMatch(/från vänster ytterkant/);
  });

  it("blocks a machine profile whose work area cannot contain the side panels", () => {
    const result = resolveDesign({ ...DEFAULT_DESIGN_SPEC, machine_profile_id: "shapeoko-hdmi-v1" });
    const machineRule = result.rule_evaluations.find((rule) => rule.rule_id === "DFM-MACHINE-001");

    expect(machineRule?.status).toBe("BLOCK");
    expect(machineRule?.affected_part_ids).toContain("side-left");
    expect(result.status).toBe("BLOCK");
  });

  it("keeps wide furniture blocked for review and validates only an explicitly selected profile", () => {
    const wide = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 4_200,
      height_mm: 2_400,
      divider_count: 6,
      stock_width_mm: 2_440,
      stock_height_mm: 1_220,
      stock_count: 24,
    });
    expect(wide.rule_evaluations.find((rule) => rule.rule_id === "DFM-MACHINE-001"))
      .toMatchObject({
        status: "BLOCK",
        suggestion: { action: "create_stockless_review_package" },
      });
    expect(wide.rule_evaluations.find((rule) => rule.rule_id === "DFM-STOCK-001"))
      .toMatchObject({
        status: "BLOCK",
        suggestion: { action: "create_stockless_review_package" },
      });

    const largeFormat = resolveDesign({
      ...wide.spec,
      stock_width_mm: 5_000,
      stock_height_mm: 2_500,
      back_stock_width_mm: 5_000,
      back_stock_height_mm: 2_500,
      machine_profile_id: "custombuild-router-5125-linuxcnc",
    });
    expect(largeFormat.rule_evaluations.find((rule) => rule.rule_id === "DFM-MACHINE-001")?.status)
      .toBe("PASS");
    expect(largeFormat.rule_evaluations.find((rule) => rule.rule_id === "DFM-STOCK-001")?.status)
      .toBe("PASS");
  });

  it("keeps capacity-only stock overflow as a hard blocker without a stockless action", () => {
    const capacityOnly = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      stock_count: 1,
    });
    const stockRule = capacityOnly.rule_evaluations.find(
      (rule) => rule.rule_id === "DFM-STOCK-001",
    );

    expect(stockRule?.status).toBe("BLOCK");
    expect(stockRule?.affected_part_ids.length).toBeGreaterThan(0);
    expect(stockRule?.suggestion).toBeUndefined();
    expect(stockRule?.summary).toContain("kan inte placeras inom");
  });

  it("nests 6 mm backs only on the selected back stock profile", () => {
    const blocked = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 2,
      reinforcement_mode: "manual",
      stock_count: 24,
      back_stock_width_mm: 100,
      back_stock_height_mm: 100,
      back_stock_count: 3,
    });
    expect(blocked.nesting.overflow_part_ids).toEqual([
      "back-panel-bay-1",
      "back-panel-bay-2",
      "back-panel-bay-3",
    ]);
    expect(blocked.nesting.placements.every(
      (placement) => placement.stock_role === "carcass",
    )).toBe(true);

    const nested = resolveDesign({
      ...blocked.spec,
      back_stock_width_mm: 2_440,
      back_stock_height_mm: 1_220,
    });
    const carcassSheets = nested.nesting.placements
      .filter((placement) => placement.stock_role === "carcass")
      .map((placement) => placement.sheet);
    const backPlacements = nested.nesting.placements.filter(
      (placement) => placement.stock_role === "back",
    );
    expect(backPlacements).toHaveLength(3);
    expect(backPlacements.every(
      (placement) => placement.material_id === nested.spec.back_material_id,
    )).toBe(true);
    expect(backPlacements.every(
      (placement) => placement.sheet > Math.max(...carcassSheets),
    )).toBe(true);
    expect(nested.nesting.overflow_part_ids).toEqual([]);
  });

  it("keeps local manufacturing blockers in an authoritative server preview", () => {
    const local = resolveDesign({ ...DEFAULT_DESIGN_SPEC, width_mm: 4_200 });
    const localJoint = local.rule_evaluations.find(
      (rule) => rule.rule_id === "CB-JOINT-001",
    )!;
    const serverJoint = {
      ...localJoint,
      summary: "Serverns auktoritativa förbandsbedömning.",
    };
    const server = {
      ...local,
      source: "server-preview" as const,
      status: "PASS" as const,
      rule_evaluations: [
        ...local.rule_evaluations.filter(
          (rule) => !rule.rule_id.startsWith("DFM-") && rule.rule_id !== "CB-JOINT-001",
        ),
        serverJoint,
      ],
    };

    const merged = mergeServerDesignWithLocalDfm(server, local);

    expect(merged.source).toBe("server-preview");
    expect(merged.rule_evaluations.find((rule) => rule.rule_id === "DFM-MACHINE-001")?.status)
      .toBe("BLOCK");
    expect(merged.rule_evaluations.filter((rule) => rule.rule_id === "CB-JOINT-001"))
      .toEqual([serverJoint]);
    expect(merged.status).toBe("BLOCK");
  });

  it("does not synthesize a grain decision in the browser", () => {
    const local = resolveDesign({ ...DEFAULT_DESIGN_SPEC, back_panel: true });

    expect(local.rule_evaluations.some((rule) => rule.rule_id === "DFM-GRAIN-001"))
      .toBe(false);
  });

  it("preserves an authoritative server grain warning without replacing it locally", () => {
    const local = resolveDesign({ ...DEFAULT_DESIGN_SPEC, back_panel: true });
    const serverGrain = {
      rule_id: "DFM-GRAIN-001",
      rule_version: "1.0.0",
      status: "WARNING" as const,
      title: "Fiberriktning för skivmaterial",
      summary: "Servern saknar en strukturerad råskiveaxel.",
      calculation: "Råskiveaxel = ej strukturerat bunden",
      assumptions: ["Okänd råskiveaxel får inte behandlas som riktningslöst material."],
      affected_part_ids: ["side-left"],
    };
    const server = {
      ...local,
      source: "server-preview" as const,
      status: "WARNING" as const,
      rule_evaluations: [...local.rule_evaluations, serverGrain],
    };

    const merged = mergeServerDesignWithLocalDfm(server, local);

    expect(merged.rule_evaluations.filter((rule) => rule.rule_id === "DFM-GRAIN-001"))
      .toEqual([serverGrain]);
  });

  it("changes the preview hash when any production-driving input changes", () => {
    const initial = resolveDesign(DEFAULT_DESIGN_SPEC);
    const changed = resolveDesign({ ...DEFAULT_DESIGN_SPEC, measured_thickness_mm: 17.7 });

    expect(changed.design_hash).not.toBe(initial.design_hash);
    expect(changed.parts.find((part) => part.part_id === "side-left")?.thickness_mm).toBe(17.7);
  });

  it("reports invalid dimensions as blocking instead of inventing valid output", () => {
    const result = resolveDesign({ ...DEFAULT_DESIGN_SPEC, width_mm: 20 });
    const geometryRule = result.rule_evaluations.find((rule) => rule.rule_id === "GEO-001");

    expect(geometryRule?.status).toBe("BLOCK");
    expect(geometryRule?.summary).toContain("bredden");
  });

  it("accepts the published envelope boundaries and blocks values immediately outside them", () => {
    const maximum = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 6_000,
      height_mm: 4_000,
      depth_mm: 1_200,
      shelf_count: 40,
      divider_count: 16,
      load_per_shelf_kg: 500,
      reinforcement_mode: "manual",
    });
    expect(maximum.rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.status).toBe("PASS");
    expect(maximum.spec.bay_width_ratios).toEqual([]);
    expect(maximum.spec.shelf_height_ratios).toEqual([]);

    const outsideDesigns = [
      { ...DEFAULT_DESIGN_SPEC, width_mm: 6_001 },
      { ...DEFAULT_DESIGN_SPEC, height_mm: 4_001 },
      { ...DEFAULT_DESIGN_SPEC, depth_mm: 1_201 },
      { ...DEFAULT_DESIGN_SPEC, shelf_count: 41 },
      { ...DEFAULT_DESIGN_SPEC, divider_count: 17 },
      { ...DEFAULT_DESIGN_SPEC, load_per_shelf_kg: 501 },
    ] satisfies DesignSpec[];
    for (const outside of outsideDesigns) {
      const result = resolveDesign({ ...outside, reinforcement_mode: "manual" });
      expect(result.rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.status).toBe("BLOCK");
    }
  });

  it("enforces wall-library base family limits without silently changing invalid input", () => {
    const valid = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      height_mm: 1_000,
      shelf_count: 0,
      base_cabinet_count: 1,
      base_cabinet_height_mm: 782,
      base_cabinet_depth_mm: DEFAULT_DESIGN_SPEC.depth_mm,
      reinforcement_mode: "manual" as const,
    };
    expect(resolveDesign(valid).rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.status)
      .toBe("PASS");

    const tooTall = resolveDesign({ ...valid, base_cabinet_height_mm: 783 });
    expect(tooTall.spec.base_cabinet_height_mm).toBe(783);
    expect(tooTall.rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.status).toBe("BLOCK");
  });

  it("renders individually sized bays and shelf levels in deliberate free mode", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      symmetry_locked: false,
      width_mm: 1_200,
      height_mm: 2_000,
      shelf_count: 2,
      divider_count: 1,
      reinforcement_mode: "manual",
      bay_width_ratios: [0.25, 0.75],
      shelf_height_ratios: [0.2, 0.8],
    });
    const shelves = result.parts.filter((part) => part.kind === "shelf");
    const widths = [...new Set(shelves.map((part) => part.width_mm))].sort((left, right) => left - right);
    const levels = [...new Set(shelves.map((part) => part.position_mm.z))].sort((left, right) => left - right);

    expect(widths[0]).toBeCloseTo(298.516);
    expect(widths[1]).toBeCloseTo(871.816);
    expect(levels[1]! - levels[0]!).toBeGreaterThan(900);
    expect(result.parts.find((part) => part.kind === "divider")?.position_mm.x).toBeCloseTo(313.35);
  });

  it("blocks malformed free-layout ratios", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 2,
      reinforcement_mode: "manual",
      bay_width_ratios: [0.5, 0.5],
    });

    expect(result.rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.summary).toContain("fackbredder");
    expect(result.status).toBe("BLOCK");
  });

  it("normalizes custom bay proportions before enforcing the eight-percent minimum", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 1,
      reinforcement_mode: "manual",
      symmetry_locked: false,
      bay_width_ratios: [1, 99],
    });

    expect(result.rule_evaluations.find((rule) => rule.rule_id === "GEO-001")).toMatchObject({
      status: "BLOCK",
    });
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.summary)
      .toContain("8 %");
  });

  it("accepts a shelf that is exactly forty millimetres after side clearances", () => {
    const innerWidthMm = 250 - 3 * DEFAULT_DESIGN_SPEC.measured_thickness_mm;
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 250,
      divider_count: 1,
      bay_width_ratios: [42 / innerWidthMm, (innerWidthMm - 42) / innerWidthMm],
      symmetry_locked: false,
      reinforcement_mode: "manual",
    });

    expect(result.parts.find((part) => part.part_id === "shelf-1-bay-1")?.width_mm)
      .toBeCloseTo(42 + 2 * (Math.floor(DEFAULT_DESIGN_SPEC.measured_thickness_mm * 1_000 / 3) / 1_000));
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.status).toBe("PASS");
  });

  it("blocks layouts that violate clear shelf, opening, or base-module geometry", () => {
    const invalidDesigns = [
      { ...DEFAULT_DESIGN_SPEC, width_mm: 250, divider_count: 16, reinforcement_mode: "manual" as const },
      { ...DEFAULT_DESIGN_SPEC, height_mm: 300, shelf_count: 40, reinforcement_mode: "manual" as const },
      {
        ...DEFAULT_DESIGN_SPEC,
        furniture_type: "wall_library" as const,
        width_mm: 600,
        height_mm: 1_000,
        shelf_count: 0,
        base_cabinet_count: 3,
        base_cabinet_height_mm: 500,
        base_cabinet_depth_mm: DEFAULT_DESIGN_SPEC.depth_mm,
        reinforcement_mode: "manual" as const,
      },
    ];

    for (const invalid of invalidDesigns) {
      expect(resolveDesign(invalid).rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.status)
        .toBe("BLOCK");
    }
  });

  it("checks the actual custom bay widths used by aligned base cabinets", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      width_mm: 2_400,
      divider_count: 2,
      bay_width_ratios: [0.08, 0.46, 0.46],
      symmetry_locked: false,
      base_cabinet_count: 3,
      base_cabinet_height_mm: 680,
      base_cabinet_depth_mm: DEFAULT_DESIGN_SPEC.depth_mm,
      reinforcement_mode: "manual",
    });
    const geometryRule = result.rule_evaluations.find((rule) => rule.rule_id === "GEO-001");

    expect(geometryRule?.status).toBe("BLOCK");
    expect(geometryRule?.summary).toContain("200 mm");
  });

  it("validates unrounded shelf openings at the forty-millimetre boundary", () => {
    const thicknessMm = DEFAULT_DESIGN_SPEC.measured_thickness_mm;
    const innerHeightMm = 300 - 2 * thicknessMm;
    const firstClearOpeningMm = 94.42;
    const internalClearOpeningMm = 39.96;
    const firstRatio = (firstClearOpeningMm + thicknessMm / 2) / innerHeightMm;
    const secondRatio = firstRatio + (internalClearOpeningMm + thicknessMm) / innerHeightMm;
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      height_mm: 300,
      plinth: false,
      shelf_count: 2,
      shelf_height_ratios: [firstRatio, secondRatio],
      symmetry_locked: false,
      reinforcement_mode: "manual" as const,
    };

    expect(shelfOpeningHeights(spec)[1]).toBe(40);
    const geometryRule = resolveDesign(spec).rule_evaluations.find((rule) => rule.rule_id === "GEO-001");
    expect(geometryRule?.status).toBe("BLOCK");
    expect(geometryRule?.summary).toContain("40 mm");
  });

  it("applies individual board edits everywhere and blocks unreviewed production", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 1,
      reinforcement_mode: "manual",
      part_overrides: {
        "divider-1": {
          width_mm: 1_650,
          depth_mm: 280,
          position_x_mm: 430,
          position_z_mm: 1_100,
        },
      },
      removed_part_ids: ["shelf-1-bay-1"],
    });
    const divider = result.parts.find((part) => part.part_id === "divider-1");

    expect(divider).toMatchObject({
      width_mm: 1_650,
      depth_mm: 280,
      position_mm: { x: 430, z: 1_100 },
    });
    expect(result.parts.some((part) => part.part_id === "shelf-1-bay-1")).toBe(false);
    expect(result.bom.some((line) => line.part_ids.includes("shelf-1-bay-1"))).toBe(false);
    expect(result.nesting.placements.some((placement) => placement.part_id === "shelf-1-bay-1")).toBe(false);
    expect(result.operations.some((operation) => operation.part_id === "shelf-1-bay-1")).toBe(false);
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "PART-CUSTOM-001")).toMatchObject({
      status: "BLOCK",
      affected_part_ids: expect.arrayContaining(["divider-1", "shelf-1-bay-1"]),
    });
    expect(result.status).toBe("BLOCK");
  });

  it("merges adjacent bays, restores symmetry and regenerates continuous shelves when a divider is removed", () => {
    const original = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 4_200,
      divider_count: 4,
      reinforcement_mode: "auto" as const,
      removed_part_ids: ["shelf-2-bay-3"],
      part_overrides: { "divider-3": { position_x_mm: 2_700 } },
    };
    const before = resolveDesign({ ...original, removed_part_ids: [], part_overrides: {} });
    const oldRow = before.parts.filter((part) => part.part_id.startsWith("shelf-1-")).sort((left, right) => left.position_mm.x - right.position_mm.x);
    const removal = removePartFromDesign(original, "divider-2");
    const result = resolveDesign(removal.spec);
    const rebuiltRow = result.parts.filter((part) => part.part_id.startsWith("shelf-1-")).sort((left, right) => left.position_mm.x - right.position_mm.x);

    expect(removal.topologyRebuilt).toBe(true);
    expect(removal.spec.divider_count).toBe(3);
    expect(removal.spec.reinforcement_mode).toBe("manual");
    expect(removal.spec.topology_baseline?.divider_count).toBe(4);
    expect(removal.spec.removed_part_ids).not.toContain("shelf-2-bay-3");
    expect(removal.spec.part_overrides).not.toHaveProperty("divider-3");
    expect(result.parts.filter((part) => part.kind === "divider")).toHaveLength(3);
    expect(rebuiltRow).toHaveLength(4);
    expect(rebuiltRow[0]?.width_mm).toBeCloseTo(rebuiltRow[3]!.width_mm, 3);
    expect(rebuiltRow[1]?.width_mm).toBeCloseTo(rebuiltRow[2]!.width_mm, 3);
    expect(rebuiltRow.reduce((sum, part) => sum + part.width_mm, 0)).toBeCloseTo(
      oldRow.reduce((sum, part) => sum + part.width_mm, 0)
        + DEFAULT_DESIGN_SPEC.measured_thickness_mm
        - 2 * (Math.floor(DEFAULT_DESIGN_SPEC.measured_thickness_mm * 1_000 / 3) / 1_000),
      3,
    );
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "STR-DEF-001")?.status).toBe("BLOCK");
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "PART-CUSTOM-001")?.status).toBe("WARNING");
  });

  it("removes a complete shelf row instead of leaving a bay-sized hole", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 2,
      reinforcement_mode: "manual" as const,
    };
    const removal = removePartFromDesign(spec, "shelf-3-bay-2");
    const result = resolveDesign(removal.spec);

    expect(removal.spec.shelf_count).toBe(4);
    expect(removal.spec.shelf_height_ratios).toHaveLength(4);
    expect(result.parts.filter((part) => part.kind === "shelf")).toHaveLength(4 * 3);
    for (let row = 1; row <= 4; row += 1) {
      expect(result.parts.filter((part) => part.part_id.startsWith(`shelf-${row}-`))).toHaveLength(3);
    }
  });

  it("repairs legacy saved divider holes and can restore the original topology", () => {
    const legacy = {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 3,
      reinforcement_mode: "manual" as const,
      removed_part_ids: ["divider-2"],
    };
    const migrated = migrateLegacyStructuralRemovals(legacy);
    const repaired = resolveDesign(migrated.spec);

    expect(migrated.spec.divider_count).toBe(2);
    expect(migrated.spec.removed_part_ids).toEqual([]);
    expect(repaired.parts.filter((part) => part.kind === "shelf")).toHaveLength(DEFAULT_DESIGN_SPEC.shelf_count * 3);

    const restored = restorePartCustomizations(migrated.spec);
    expect(restored.divider_count).toBe(3);
    expect(restored.topology_baseline).toBeUndefined();
    expect(restored.part_overrides).toEqual({});
    expect(restored.removed_part_ids).toEqual([]);
  });

  it("detects any remaining hole in a bearing grid immediately", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 1,
      reinforcement_mode: "manual",
      removed_part_ids: ["shelf-2-bay-1"],
    });

    expect(result.rule_evaluations.find((rule) => rule.rule_id === "STR-TOPO-001")).toMatchObject({
      status: "BLOCK",
      affected_part_ids: ["shelf-2-bay-1"],
    });
  });

  it("moves an entire shelf row together and clamps it between its neighbours", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 2,
      reinforcement_mode: "manual" as const,
    };
    const moved = movePartVertically(spec, "shelf-3-bay-2", 1_850);
    const result = resolveDesign(moved.spec);
    const row = result.parts.filter((part) => part.part_id.startsWith("shelf-3-"));

    expect(moved.topologyRebuilt).toBe(true);
    expect(moved.spec.topology_baseline).toBeUndefined();
    expect(row).toHaveLength(3);
    expect(new Set(row.map((part) => part.position_mm.z)).size).toBe(1);
    expect(moved.spec.shelf_height_ratios[2]).toBeLessThan(moved.spec.shelf_height_ratios[3]!);
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "STR-TOPO-001")?.status).toBe("PASS");
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "PART-CUSTOM-001")).toBeUndefined();
  });

  it("keeps a full forty-shelf equal layout unchanged when direct movement cannot create valid custom levels", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      height_mm: 4_000,
      shelf_count: 40,
      shelf_height_ratios: [],
      reinforcement_mode: "manual" as const,
    };
    const moved = movePartVertically(spec, "shelf-20-bay-1", 2_500);
    const edited = editPartParametrically(spec, "shelf-20-bay-1", { position_z_mm: 2_500 });

    expect(moved.spec).toBe(spec);
    expect(moved.topologyRebuilt).toBe(false);
    expect(moved.spec.shelf_height_ratios).toEqual([]);
    expect(edited.spec).toBe(spec);
    expect(edited.supported).toBe(false);
    expect(localDesignHash(edited.spec)).toBe(localDesignHash(spec));
  });

  it("moves the top board by resizing all connected vertical geometry", () => {
    const moved = movePartVertically(DEFAULT_DESIGN_SPEC, "top", 2_450);
    const result = resolveDesign(moved.spec);

    expect(moved.spec.height_mm).toBeGreaterThan(DEFAULT_DESIGN_SPEC.height_mm);
    expect(result.parts.find((part) => part.part_id === "side-left")?.width_mm).toBe(moved.spec.height_mm);
    expect(result.parts.find((part) => part.part_id === "top")?.position_mm.z).toBeCloseTo(2_450, -1);
  });

  it("moves a divider parametrically and mirrors the opposite bay boundary", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 3_600,
      divider_count: 3,
      reinforcement_mode: "manual" as const,
      symmetry_locked: true,
    };
    const before = resolveDesign(spec);
    const target = before.parts.find((part) => part.part_id === "divider-1")!.position_mm.x + 120;
    const edited = editPartParametrically(spec, "divider-1", { position_x_mm: target });
    const after = resolveDesign(edited.spec);
    const widths = after.parts
      .filter((part) => part.part_id.startsWith("shelf-1-"))
      .sort((left, right) => left.position_mm.x - right.position_mm.x)
      .map((part) => part.width_mm);

    expect(edited.supported).toBe(true);
    expect(edited.spec.part_overrides).toEqual({});
    expect(widths[0]).toBeCloseTo(widths[3]!, 3);
    expect(widths[1]).toBeCloseTo(widths[2]!, 3);
  });

  it("reports divider-position and shelf-width edits as unsupported for a seventeen-bay equal layout", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 6_000,
      divider_count: 16,
      shelf_count: 1,
      bay_width_ratios: [],
      reinforcement_mode: "manual" as const,
    };
    const divider = resolveDesign(spec).parts.find((part) => part.part_id === "divider-1");
    if (!divider) throw new Error("Expected divider-1 in the seventeen-bay fixture");

    const dividerEdit = editPartParametrically(spec, divider.part_id, {
      position_x_mm: divider.position_mm.x + 100,
    });
    const shelfEdit = editPartParametrically(spec, "shelf-1-bay-1", { width_mm: 500 });

    expect(dividerEdit.spec).toBe(spec);
    expect(dividerEdit.supported).toBe(false);
    expect(shelfEdit.spec).toBe(spec);
    expect(shelfEdit.supported).toBe(false);
    expect(localDesignHash(dividerEdit.spec)).toBe(localDesignHash(spec));
    expect(localDesignHash(shelfEdit.spec)).toBe(localDesignHash(spec));
  });

  it("rejects detached board coordinates instead of creating broken geometry", () => {
    const edited = editPartParametrically(DEFAULT_DESIGN_SPEC, "side-left", { position_z_mm: 900 });

    expect(edited.supported).toBe(false);
    expect(edited.spec).toBe(DEFAULT_DESIGN_SPEC);
    expect(edited.spec.part_overrides).toEqual({});
  });

  it("accepts exact clear shelf openings in millimetres without changing total height", () => {
    const before = shelfOpeningHeights(DEFAULT_DESIGN_SPEC);
    const changed = setShelfOpeningHeight(DEFAULT_DESIGN_SPEC, 2, 420);
    const after = shelfOpeningHeights(changed);

    expect(before).toHaveLength(DEFAULT_DESIGN_SPEC.shelf_count + 1);
    expect(after[2]).toBeCloseTo(420, 0);
    expect(after.reduce((sum, value) => sum + value, 0)).toBeCloseTo(
      before.reduce((sum, value) => sum + value, 0),
      0,
    );
    expect(changed.height_mm).toBe(DEFAULT_DESIGN_SPEC.height_mm);
    const minimum = setShelfOpeningHeight(DEFAULT_DESIGN_SPEC, 2, 0);
    expect(shelfOpeningHeights(minimum)[2]).toBeGreaterThanOrEqual(40);
    expect(minimum.shelf_height_ratios.every((value, index, values) => (
      index === 0 || value - values[index - 1]! >= 0.05
    ))).toBe(true);
    expect(resolveDesign(changed).parts.filter((part) => part.part_id.startsWith("shelf-2-")).every((part) => part.position_mm.z === resolveDesign(changed).parts.find((part) => part.part_id.startsWith("shelf-2-"))?.position_mm.z)).toBe(true);
  });

  it("keeps edge shelf edits inside the five-percent centre bounds", () => {
    const changed = setShelfOpeningHeight(DEFAULT_DESIGN_SPEC, 0, 0);
    const ratios = changed.shelf_height_ratios;

    expect(ratios[0]).toBeGreaterThanOrEqual(0.05);
    expect(ratios.at(-1)).toBeLessThanOrEqual(0.95);
    expect(ratios.every((value, index) => index === 0 || value - ratios[index - 1]! >= 0.05))
      .toBe(true);
    expect(shelfOpeningHeights(changed).every((openingMm) => openingMm >= 40)).toBe(true);
    expect(resolveDesign(changed).rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.status)
      .toBe("PASS");
  });

  it("allows twelve custom bays but fails closed when thirteen bays cannot each occupy eight percent", () => {
    const twelveBays = {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 11,
      reinforcement_mode: "manual" as const,
      bay_width_ratios: [],
    };
    const accepted = setBayWidthRatio(twelveBays, 0, 0.09);

    expect(accepted).not.toBe(twelveBays);
    expect(accepted.bay_width_ratios).toHaveLength(12);
    expect(accepted.bay_width_ratios.reduce((sum, ratio) => sum + ratio, 0)).toBeCloseTo(1, 6);
    expect(accepted.bay_width_ratios.every((ratio) => ratio >= 0.08)).toBe(true);

    for (const symmetryLocked of [true, false]) {
      const thirteenBays = {
        ...DEFAULT_DESIGN_SPEC,
        divider_count: 12,
        reinforcement_mode: "manual" as const,
        symmetry_locked: symmetryLocked,
        bay_width_ratios: [],
      };
      expect(setBayWidthRatio(thirteenBays, 0, 0.08)).toBe(thirteenBays);
      expect(thirteenBays.bay_width_ratios).toEqual([]);
    }
  });

  it("allows nineteen custom shelf levels but keeps twenty-level height and opening edits as no-ops", () => {
    const nineteenShelves = {
      ...DEFAULT_DESIGN_SPEC,
      shelf_count: 19,
      shelf_height_ratios: [],
      symmetry_locked: false,
    };
    const accepted = setShelfHeightRatio(nineteenShelves, 0, 0.05);

    expect(accepted).not.toBe(nineteenShelves);
    expect(accepted.shelf_height_ratios).toHaveLength(19);
    expect(accepted.shelf_height_ratios[0]).toBeGreaterThanOrEqual(0.05);
    expect(accepted.shelf_height_ratios.at(-1)).toBeLessThanOrEqual(0.95);
    expect(accepted.shelf_height_ratios.every((ratio, index, ratios) => (
      index === 0 || ratio - ratios[index - 1]! >= 0.05 - 1e-9
    ))).toBe(true);

    for (const symmetryLocked of [true, false]) {
      const twentyShelves = {
        ...DEFAULT_DESIGN_SPEC,
        shelf_count: 20,
        shelf_height_ratios: [],
        symmetry_locked: symmetryLocked,
      };
      expect(setShelfHeightRatio(twentyShelves, 0, 0.05)).toBe(twentyShelves);
      expect(setShelfOpeningHeight(twentyShelves, 0, 40)).toBe(twentyShelves);
      expect(twentyShelves.shelf_height_ratios).toEqual([]);
    }
  });

  it("repairs imported asymmetric proportions around both centre lines", () => {
    const balanced = balanceDesignSymmetry({
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 6,
      shelf_count: 5,
      bay_width_ratios: [0.09, 0.17, 0.14, 0.15, 0.15, 0.15, 0.15],
      shelf_height_ratios: [0.18, 0.38, 0.55, 0.71, 0.84],
    });

    expect(balanced.bay_width_ratios.reduce((sum, value) => sum + value, 0)).toBeCloseTo(1, 5);
    expect(balanced.bay_width_ratios[0]).toBeCloseTo(balanced.bay_width_ratios[6]!, 5);
    expect(balanced.bay_width_ratios[1]).toBeCloseTo(balanced.bay_width_ratios[5]!, 5);
    expect(balanced.bay_width_ratios[2]).toBeCloseTo(balanced.bay_width_ratios[4]!, 5);
    expect(balanced.shelf_height_ratios[0]! + balanced.shelf_height_ratios[4]!).toBeCloseTo(1, 5);
    expect(balanced.shelf_height_ratios[1]! + balanced.shelf_height_ratios[3]!).toBeCloseTo(1, 5);
    expect(balanced.shelf_height_ratios[2]).toBe(0.5);
  });

  it("mirrors bay and shelf edits while the symmetry lock is active", () => {
    const fourBays = {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 3,
      reinforcement_mode: "manual" as const,
    };
    const bayChange = setBayWidthRatio(fourBays, 0, 0.15);
    const shelfChange = setShelfHeightRatio(DEFAULT_DESIGN_SPEC, 0, 0.2);

    expect(bayChange.bay_width_ratios).toEqual([0.15, 0.35, 0.35, 0.15]);
    expect(shelfChange.shelf_height_ratios[0]).toBeCloseTo(0.2, 5);
    expect(shelfChange.shelf_height_ratios[4]).toBeCloseTo(0.8, 5);
    expect(shelfChange.shelf_height_ratios[2]).toBe(0.5);
  });

  it("mirrors exact openings and direct shelf dragging", () => {
    const changed = setShelfOpeningHeight(DEFAULT_DESIGN_SPEC, 1, 260);
    const openings = shelfOpeningHeights(changed);
    const moved = movePartVertically(DEFAULT_DESIGN_SPEC, "shelf-1-bay-1", 520);

    expect(openings[1]).toBeCloseTo(260, 0);
    expect(openings[1]).toBeCloseTo(openings[openings.length - 2]!, 0);
    expect(moved.spec.shelf_height_ratios[0]! + moved.spec.shelf_height_ratios[4]!).toBeCloseTo(1, 5);
  });

  it("allows a deliberate asymmetric layout only after symmetry is unlocked", () => {
    const free = setBayWidthRatio({
      ...DEFAULT_DESIGN_SPEC,
      symmetry_locked: false,
      divider_count: 1,
      reinforcement_mode: "manual",
    }, 0, 0.35);

    expect(free.bay_width_ratios).toEqual([0.35, 0.65]);
  });

  it("uses full-height carcass sides and verified dado overlaps as base supports", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      width_mm: 4_200,
      height_mm: 2_600,
      divider_count: 4,
      shelf_count: 2,
      base_cabinet_count: 5,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 320,
      plinth_height_mm: 125,
      reinforcement_mode: "manual" as const,
    };
    const result = resolveDesign(spec);
    const byId = new Map(result.parts.map((part) => [part.part_id, part]));
    const baseSides = result.parts.filter((part) => part.kind === "base_side");
    const baseBottoms = result.parts
      .filter((part) => part.kind === "base_bottom")
      .sort((left, right) => left.position_mm.x - right.position_mm.x);
    const upperBottom = byId.get("bottom")!;
    const dadoDepth = Math.floor((spec.measured_thickness_mm * 1_000) / 3) / 1_000;
    const xInterval = (part: (typeof result.parts)[number]) => (
      part.orientation === "YZ"
        ? [part.position_mm.x - part.thickness_mm / 2, part.position_mm.x + part.thickness_mm / 2]
        : [part.position_mm.x - part.width_mm / 2, part.position_mm.x + part.width_mm / 2]
    );
    const zInterval = (part: (typeof result.parts)[number]) => (
      part.orientation === "YZ"
        ? [part.position_mm.z - part.width_mm / 2, part.position_mm.z + part.width_mm / 2]
        : [part.position_mm.z - part.thickness_mm / 2, part.position_mm.z + part.thickness_mm / 2]
    );
    const overlap = (first: number[], second: number[]) => (
      Math.max(0, Math.min(first[1]!, second[1]!) - Math.max(first[0]!, second[0]!))
    );

    expect(baseSides.map((part) => part.part_id)).toEqual([
      "base-side-2",
      "base-side-3",
      "base-side-4",
      "base-side-5",
    ]);
    expect(byId.has("base-side-1")).toBe(false);
    expect(byId.has("base-side-6")).toBe(false);
    expect(byId.get("side-left")?.features.some((feature) => feature.id.endsWith("base-bottom-support"))).toBe(true);
    expect(byId.get("side-right")?.features.some((feature) => feature.id.endsWith("base-bottom-support"))).toBe(true);
    expect(upperBottom.features.filter((feature) => feature.id.includes("base-side-support-"))).toHaveLength(4);

    baseBottoms.forEach((baseBottom, index) => {
      const leftSupport = byId.get(index === 0 ? "side-left" : `base-side-${index + 1}`)!;
      const rightSupport = byId.get(
        index === baseBottoms.length - 1 ? "side-right" : `base-side-${index + 2}`,
      )!;
      expect(overlap(xInterval(baseBottom), xInterval(leftSupport))).toBeCloseTo(dadoDepth, 3);
      expect(overlap(xInterval(baseBottom), xInterval(rightSupport))).toBeCloseTo(dadoDepth, 3);
    });
    baseSides.forEach((baseSide) => {
      expect(overlap(zInterval(baseSide), zInterval(upperBottom))).toBeCloseTo(dadoDepth, 3);
      expect(baseSide.features.filter((feature) => feature.id.includes("bottom-support"))).toHaveLength(2);
    });
  });

  it("accepts sixteen dividers with seventeen aligned base modules", () => {
    const result = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      width_mm: 6_000,
      height_mm: 2_600,
      shelf_count: 1,
      load_per_shelf_kg: 0,
      divider_count: 16,
      base_cabinet_count: 17,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 320,
      reinforcement_mode: "manual",
    });
    const baseSideIds = result.parts
      .filter((part) => part.kind === "base_side")
      .map((part) => part.part_id);

    expect(baseSideIds).toHaveLength(16);
    expect(baseSideIds[0]).toBe("base-side-2");
    expect(baseSideIds.at(-1)).toBe("base-side-17");
    expect(result.parts.filter((part) => part.kind === "base_bottom")).toHaveLength(17);
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "GEO-001")?.status).toBe("PASS");
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "STR-TOPO-001")?.status).toBe("PASS");
    expect(result.rule_evaluations.find((rule) => rule.rule_id === "BASE-SUPPORT-001")?.status).toBe("PASS");
  });

  it("rebuilds the full lower grid when an internal base side is removed", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      width_mm: 4_200,
      height_mm: 2_600,
      divider_count: 4,
      base_cabinet_count: 5,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 320,
      reinforcement_mode: "manual" as const,
    };
    const removal = removePartFromDesign(spec, "base-side-3");
    const rebuilt = resolveDesign(removal.spec);

    expect(removal.topologyRebuilt).toBe(true);
    expect(removal.spec.base_cabinet_count).toBe(4);
    expect(removal.spec.removed_part_ids).toEqual([]);
    expect(removal.spec.symmetry_locked).toBe(true);
    expect(rebuilt.parts.filter((part) => part.kind === "base_side").map((part) => part.part_id))
      .toEqual(["base-side-2", "base-side-3", "base-side-4"]);
    expect(rebuilt.parts.filter((part) => part.kind === "base_bottom")).toHaveLength(4);
    expect(rebuilt.parts.filter((part) => part.kind === "cabinet_front")).toHaveLength(4);
    expect(rebuilt.rule_evaluations.find((rule) => rule.rule_id === "STR-TOPO-001")?.status)
      .toBe("PASS");
    expect(rebuilt.rule_evaluations.find((rule) => rule.rule_id === "BASE-SUPPORT-001"))
      .toMatchObject({ status: "BLOCK", suggestion: { value: 5 } });
  });

  it("maps an edited internal base-side panel height back to cabinet height", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      base_cabinet_count: 3,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 320,
      reinforcement_mode: "manual" as const,
    };
    const before = resolveDesign(spec).parts.find((part) => part.part_id === "base-side-2")!;
    const requestedPanelHeight = before.width_mm + 20;
    const edited = editPartParametrically(spec, before.part_id, { width_mm: requestedPanelHeight });
    const after = resolveDesign(edited.spec).parts.find((part) => part.part_id === before.part_id)!;

    expect(edited.supported).toBe(true);
    expect(after.width_mm).toBeCloseTo(requestedPanelHeight, 3);
    expect(after.position_mm.z - after.width_mm / 2).toBeCloseTo(spec.plinth_height_mm, 3);
  });

  it("treats an edited base-side depth as the furniture outer depth", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      depth_mm: 320,
      base_cabinet_depth_mm: 320,
      base_cabinet_count: 3,
      reinforcement_mode: "manual" as const,
    };

    const edited = editPartParametrically(spec, "base-side-2", { depth_mm: 410 });

    expect(edited.supported).toBe(true);
    expect(edited.spec.depth_mm).toBe(410);
    expect(edited.spec.base_cabinet_depth_mm).toBe(410);
    expect(edited.notice).toMatch(/yttermått/);
  });

  it("clamps parametric outer and base edits to the published envelope", () => {
    expect(editPartParametrically(DEFAULT_DESIGN_SPEC, "top", { width_mm: 10_000 }).spec.width_mm)
      .toBe(6_000);
    expect(editPartParametrically(DEFAULT_DESIGN_SPEC, "side-left", { width_mm: 10_000 }).spec.height_mm)
      .toBe(4_000);
    expect(editPartParametrically(DEFAULT_DESIGN_SPEC, "side-left", { depth_mm: 10_000 }).spec.depth_mm)
      .toBe(1_200);

    const wallLibrary = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      height_mm: 2_400,
      base_cabinet_count: 3,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: DEFAULT_DESIGN_SPEC.depth_mm,
    };
    expect(editPartParametrically(wallLibrary, "base-side-2", { width_mm: 10_000 }).spec.base_cabinet_height_mm)
      .toBe(2_000);
  });
});
