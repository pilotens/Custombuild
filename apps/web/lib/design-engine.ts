import {
  MACHINES,
  MATERIALS,
  type BomLine,
  type CamOperation,
  type ChangeDiff,
  type DesignSpec,
  type ManufacturingFeature,
  type NestingPlacement,
  type NestingResult,
  type ResolvedDesign,
  type ResolvedPart,
  type RuleEvaluation,
  type ValidationStatus,
} from "./design-types";

const GRAVITY = 9.80665;
const BACK_THICKNESS_MM = 6;
const PLINTH_HEIGHT_MM = 80;
const NESTING_GAP_MM = 8;

function round(value: number, decimals = 3): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => `${JSON.stringify(key)}:${canonicalJson(child)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

/** Stable local preview identifier. Production hashes are returned by the API. */
export function localDesignHash(spec: DesignSpec): string {
  const text = canonicalJson(spec);
  let left = 0x811c9dc5;
  let right = 0x9e3779b9;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    left = Math.imul(left ^ code, 0x01000193) >>> 0;
    right = Math.imul(right ^ code, 0x85ebca6b) >>> 0;
  }
  const token = `${left.toString(16).padStart(8, "0")}${right.toString(16).padStart(8, "0")}`;
  return token.repeat(4);
}

function partWeightKg(widthMm: number, depthMm: number, thicknessMm: number, density: number): number {
  return round((widthMm * depthMm * thicknessMm * density) / 1_000_000_000, 2);
}

function makeFeature(
  partId: string,
  suffix: string,
  kind: ManufacturingFeature["kind"],
  face: ManufacturingFeature["face"],
  depthMm: number,
  description: string,
  toolDiameterMm?: number,
): ManufacturingFeature {
  return {
    id: `${partId}:${suffix}`,
    kind,
    face,
    depth_mm: round(depthMm),
    description,
    ...(toolDiameterMm === undefined ? {} : { tool_diameter_mm: toolDiameterMm }),
  };
}

function commonFeatures(partId: string, thickness: number): ManufacturingFeature[] {
  return [
    makeFeature(partId, "outline", "outline", "A", thickness, "Utvändig kontur", 6),
    makeFeature(partId, "label", "label", "A", 0.4, "Part-ID och orienteringsmärke", 3),
  ];
}

function generateParts(spec: DesignSpec): ResolvedPart[] {
  const width = Math.max(spec.width_mm, 1);
  const height = Math.max(spec.height_mm, 1);
  const depth = Math.max(spec.depth_mm, 1);
  const thickness = Math.max(spec.measured_thickness_mm, 0.1);
  const dividerCount = Math.max(0, Math.trunc(spec.divider_count));
  const shelfCount = Math.max(0, Math.trunc(spec.shelf_count));
  const material = MATERIALS.find((candidate) => candidate.id === spec.material_id) ?? MATERIALS[0]!;
  const density = material.densityKgM3;
  const innerWidth = Math.max(width - 2 * thickness - dividerCount * thickness, 1);
  const bayCount = dividerCount + 1;
  const bayWidth = innerWidth / bayCount;
  const innerHeight = Math.max(height - 2 * thickness, 1);
  const shelfDepth = Math.max(depth - (spec.back_panel ? BACK_THICKNESS_MM + 4 : 0), 1);
  const parts: ResolvedPart[] = [];

  const addPart = (part: Omit<ResolvedPart, "material_id" | "weight_kg">) => {
    parts.push({
      ...part,
      material_id: spec.material_id,
      weight_kg: partWeightKg(part.width_mm, part.depth_mm, part.thickness_mm, density),
    });
  };

  for (const side of ["left", "right"] as const) {
    const partId = `side-${side}`;
    const features = commonFeatures(partId, thickness);
    if (spec.back_panel) {
      features.push(
        makeFeature(partId, "back-groove", "groove", "A", 4, "Not för bakstycke", 6),
      );
    }
    for (let shelfIndex = 0; shelfIndex < shelfCount; shelfIndex += 1) {
      features.push(
        makeFeature(
          partId,
          `shelf-joint-${shelfIndex + 1}`,
          spec.fixed_shelves ? "groove" : "drill",
          "A",
          spec.fixed_shelves ? Math.min(thickness / 3, 6) : 12,
          spec.fixed_shelves
            ? `Not för fast hylla ${shelfIndex + 1}`
            : `Hyllbärarhål för hylla ${shelfIndex + 1}`,
          spec.fixed_shelves ? 6 : 5,
        ),
      );
    }
    addPart({
      part_id: partId,
      name: side === "left" ? "Vänster gavel" : "Höger gavel",
      kind: "side",
      width_mm: height,
      depth_mm: depth,
      thickness_mm: thickness,
      position_mm: {
        x: side === "left" ? thickness / 2 : width - thickness / 2,
        y: depth / 2,
        z: height / 2,
      },
      orientation: "YZ",
      color: "#c7a97c",
      features,
    });
  }

  for (const horizontal of ["bottom", "top"] as const) {
    const partId = horizontal;
    const features = commonFeatures(partId, thickness);
    if (spec.back_panel) {
      features.push(
        makeFeature(partId, "back-groove", "groove", "A", 4, "Not för bakstycke", 6),
      );
    }
    addPart({
      part_id: partId,
      name: horizontal === "bottom" ? "Botten" : "Topp",
      kind: horizontal,
      width_mm: Math.max(width - 2 * thickness, 1),
      depth_mm: depth,
      thickness_mm: thickness,
      position_mm: {
        x: width / 2,
        y: depth / 2,
        z: horizontal === "bottom" ? thickness / 2 : height - thickness / 2,
      },
      orientation: "XY",
      color: "#d3b78d",
      features,
    });
  }

  for (let dividerIndex = 0; dividerIndex < dividerCount; dividerIndex += 1) {
    const partId = `divider-${dividerIndex + 1}`;
    const x = thickness + bayWidth * (dividerIndex + 1) + thickness * (dividerIndex + 0.5);
    addPart({
      part_id: partId,
      name: `Vertikal avdelare ${dividerIndex + 1}`,
      kind: "divider",
      width_mm: innerHeight,
      depth_mm: shelfDepth,
      thickness_mm: thickness,
      position_mm: { x, y: shelfDepth / 2, z: height / 2 },
      orientation: "YZ",
      color: "#b8925d",
      features: [
        ...commonFeatures(partId, thickness),
        makeFeature(partId, "top-joint", "groove", "EDGE", Math.min(thickness / 3, 6), "Fog mot topp", 6),
        makeFeature(partId, "bottom-joint", "groove", "EDGE", Math.min(thickness / 3, 6), "Fog mot botten", 6),
      ],
    });
  }

  for (let shelfIndex = 0; shelfIndex < shelfCount; shelfIndex += 1) {
    const z = thickness + (innerHeight * (shelfIndex + 1)) / (shelfCount + 1);
    for (let bayIndex = 0; bayIndex < bayCount; bayIndex += 1) {
      const partId = `shelf-${shelfIndex + 1}-bay-${bayIndex + 1}`;
      const x = thickness + bayIndex * (bayWidth + thickness) + bayWidth / 2;
      const features = commonFeatures(partId, thickness);
      addPart({
        part_id: partId,
        name: bayCount === 1 ? `Hylla ${shelfIndex + 1}` : `Hylla ${shelfIndex + 1}.${bayIndex + 1}`,
        kind: "shelf",
        width_mm: bayWidth,
        depth_mm: shelfDepth,
        thickness_mm: thickness,
        position_mm: { x, y: shelfDepth / 2, z },
        orientation: "XY",
        color: "#d7bc91",
        features,
      });
    }
  }

  if (spec.back_panel) {
    const partId = "back-panel";
    addPart({
      part_id: partId,
      name: "Bakstycke",
      kind: "back",
      width_mm: Math.max(width - 8, 1),
      depth_mm: Math.max(height - 8, 1),
      thickness_mm: BACK_THICKNESS_MM,
      position_mm: { x: width / 2, y: BACK_THICKNESS_MM / 2, z: height / 2 },
      orientation: "XZ",
      color: "#b99869",
      features: commonFeatures(partId, BACK_THICKNESS_MM),
    });
  }

  if (spec.plinth) {
    const partId = "plinth-front";
    addPart({
      part_id: partId,
      name: "Främre sockel",
      kind: "plinth",
      width_mm: Math.max(width - 2 * thickness, 1),
      depth_mm: PLINTH_HEIGHT_MM,
      thickness_mm: thickness,
      position_mm: { x: width / 2, y: depth - thickness / 2, z: thickness + PLINTH_HEIGHT_MM / 2 },
      orientation: "XZ",
      color: "#a98455",
      features: commonFeatures(partId, thickness),
    });
  }

  return parts;
}

function calculateShelfDeflection(spec: DesignSpec): {
  deflectionMm: number;
  allowableMm: number;
  stressMpa: number;
  spanMm: number;
} {
  const material = MATERIALS.find((candidate) => candidate.id === spec.material_id) ?? MATERIALS[0]!;
  const thickness = Math.max(spec.measured_thickness_mm, 0.1);
  const bayCount = Math.max(1, Math.trunc(spec.divider_count) + 1);
  const span = Math.max((spec.width_mm - 2 * thickness - Math.max(0, spec.divider_count) * thickness) / bayCount, 1);
  const depth = Math.max(spec.depth_mm - (spec.back_panel ? BACK_THICKNESS_MM + 4 : 0), 1);
  const loadN = Math.max(spec.load_per_shelf_kg, 0) * GRAVITY;
  const lineLoad = loadN / span;
  const inertia = (depth * thickness ** 3) / 12;
  const creepFactor = 1.6;
  const deflection = (5 * lineLoad * span ** 4 * creepFactor) / (384 * material.elasticModulusMpa * inertia);
  const bendingMoment = (lineLoad * span ** 2) / 8;
  const stress = (bendingMoment * (thickness / 2)) / inertia;
  return {
    deflectionMm: round(deflection),
    allowableMm: round(Math.min(span / 200, 5)),
    stressMpa: round(stress),
    spanMm: round(span),
  };
}

export function suggestedDividerCount(spec: DesignSpec): number {
  const current = Math.max(0, Math.trunc(spec.divider_count));
  for (let candidate = current; candidate <= 8; candidate += 1) {
    const result = calculateShelfDeflection({ ...spec, divider_count: candidate });
    if (result.deflectionMm <= result.allowableMm) return candidate;
  }
  return Math.max(current, 8);
}

function invalidGeometryRule(spec: DesignSpec): RuleEvaluation {
  const failures: string[] = [];
  if (spec.width_mm <= 2 * spec.measured_thickness_mm) failures.push("bredden måste överstiga två materialtjocklekar");
  if (spec.height_mm <= 2 * spec.measured_thickness_mm + (spec.plinth ? PLINTH_HEIGHT_MM : 0)) failures.push("höjden lämnar inget användbart innerutrymme");
  if (spec.depth_mm <= spec.measured_thickness_mm) failures.push("djupet måste överstiga materialtjockleken");
  if (spec.measured_thickness_mm <= 0) failures.push("uppmätt tjocklek måste vara större än noll");
  if (!Number.isInteger(spec.shelf_count) || spec.shelf_count < 0 || spec.shelf_count > 30) failures.push("antal hyllor måste vara ett heltal mellan 0 och 30");
  if (!Number.isInteger(spec.divider_count) || spec.divider_count < 0 || spec.divider_count > 8) failures.push("antal avdelare måste vara ett heltal mellan 0 och 8");
  return {
    rule_id: "GEO-001",
    rule_version: "1.0.0",
    status: failures.length > 0 ? "BLOCK" : "PASS",
    title: "Giltig parametrisk geometri",
    summary: failures.length > 0 ? failures.join("; ") : "Alla grundmått ger ett giltigt innerutrymme.",
    calculation: "B > 2t, H > 2t + sockel, D > t",
    assumptions: [],
    affected_part_ids: [],
  };
}

function shelfRule(spec: DesignSpec, parts: ResolvedPart[]): RuleEvaluation {
  if (spec.shelf_count === 0) {
    return {
      rule_id: "STR-DEF-001",
      rule_version: "1.1.0",
      status: "PASS",
      title: "Hyllnedböjning",
      summary: "Ingen hylla att kontrollera.",
      calculation: "Ej tillämplig",
      assumptions: [],
      affected_part_ids: [],
    };
  }
  const result = calculateShelfDeflection(spec);
  const ratio = result.deflectionMm / Math.max(result.allowableMm, 0.001);
  const status: ValidationStatus = ratio > 1.5 ? "BLOCK" : ratio > 1 ? "WARNING" : "PASS";
  const proposedCount = suggestedDividerCount(spec);
  const material = MATERIALS.find((candidate) => candidate.id === spec.material_id) ?? MATERIALS[0]!;
  const affected = parts.filter((part) => part.kind === "shelf").map((part) => part.part_id);
  return {
    rule_id: "STR-DEF-001",
    rule_version: "1.1.0",
    status,
    title: "Hyllnedböjning",
    summary:
      status === "PASS"
        ? `Beräknad långtidsnedböjning ${result.deflectionMm} mm ligger inom gränsen.`
        : `Beräknad långtidsnedböjning ${result.deflectionMm} mm överskrider gränsen ${result.allowableMm} mm.`,
    calculation: "δ = 5wL⁴ / (384EI) × 1,6",
    calculated_value: result.deflectionMm,
    allowed_value: result.allowableMm,
    unit: "mm",
    margin_percent: round(((result.allowableMm - result.deflectionMm) / Math.max(result.allowableMm, 0.001)) * 100, 1),
    assumptions: [
      `Jämnt fördelad last ${spec.load_per_shelf_kg} kg`,
      `Fri spännvidd ${result.spanMm} mm`,
      `E-modul ${material.elasticModulusMpa} MPa (${material.version})`,
      "Långtidsfaktor 1,6; screening, inte certifiering",
    ],
    affected_part_ids: affected,
    ...(status === "PASS" || proposedCount <= spec.divider_count
      ? {}
      : {
          suggestion: {
            action: "set_divider_count" as const,
            label: `Inför ${proposedCount} vertikal ${proposedCount === 1 ? "avdelare" : "avdelare"}`,
            value: proposedCount,
            explanation: `Delar fri spännvidd i ${proposedCount + 1} fack och räknar om samtliga delar.`,
          },
        }),
  };
}

function stabilityRule(spec: DesignSpec): RuleEvaluation {
  const slenderness = spec.height_mm / Math.max(spec.depth_mm, 1);
  const needsAnchor = slenderness > 4 || spec.height_mm >= 1_800;
  const status: ValidationStatus = needsAnchor && !spec.wall_anchor_verified ? "WARNING" : "PASS";
  return {
    rule_id: "STAB-TIP-002",
    rule_version: "1.0.0",
    status,
    title: "Tipprisk och väggförankring",
    summary:
      status === "PASS"
        ? "Geometrin kräver ingen ytterligare varning i denna screening."
        : "Proportionerna kräver att väggunderlag och ett godkänt förankringssystem verifieras före release.",
    calculation: `H/D = ${round(slenderness, 2)}; varningsgräns 4,0`,
    calculated_value: round(slenderness, 2),
    allowed_value: 4,
    unit: "H/D",
    margin_percent: round(((4 - slenderness) / 4) * 100, 1),
    assumptions: ["Ingen dörr- eller lådlast i MVP", "Förankring får inte väljas utan känt väggunderlag och evidens"],
    affected_part_ids: ["side-left", "side-right"],
  };
}

function backRule(spec: DesignSpec): RuleEvaluation {
  return {
    rule_id: "STAB-RACK-001",
    rule_version: "1.0.0",
    status: spec.back_panel ? "PASS" : "WARNING",
    title: "Sidostabilitet",
    summary: spec.back_panel
      ? "Bakstycke finns och bidrar till att motverka skevning."
      : "Bakstycke saknas. En separat, verifierad stagning behöver dimensioneras.",
    calculation: spec.back_panel ? "Bakstycke: aktivt" : "Bakstycke: saknas",
    assumptions: ["Bakstyckets infästning behöver verifieras fysiskt"],
    affected_part_ids: spec.back_panel ? ["back-panel"] : ["side-left", "side-right"],
    ...(spec.back_panel
      ? {}
      : {
          suggestion: {
            action: "enable_back" as const,
            label: "Lägg till bakstycke",
            value: true,
            explanation: "Tillför ett bakstycke och genererar matchande noter.",
          },
        }),
  };
}

function machineRule(spec: DesignSpec, parts: ResolvedPart[]): RuleEvaluation {
  const machine = MACHINES.find((candidate) => candidate.id === spec.machine_profile_id);
  if (!machine) {
    return {
      rule_id: "DFM-MACHINE-001",
      rule_version: "1.0.0",
      status: "BLOCK",
      title: "Maskinens arbetsområde",
      summary: "Vald maskinprofil saknas eller är inte versionslåst.",
      calculation: "Profil måste finnas i maskinbiblioteket",
      assumptions: [],
      affected_part_ids: parts.map((part) => part.part_id),
    };
  }
  const outside = parts.filter((part) => {
    const long = Math.max(part.width_mm, part.depth_mm);
    const short = Math.min(part.width_mm, part.depth_mm);
    const fitsNormal = long <= machine.workAreaMm.x && short <= machine.workAreaMm.y;
    const fitsRotated = short <= machine.workAreaMm.x && long <= machine.workAreaMm.y;
    return !fitsNormal && !fitsRotated;
  });
  return {
    rule_id: "DFM-MACHINE-001",
    rule_version: "1.0.0",
    status: outside.length > 0 ? "BLOCK" : "PASS",
    title: "Maskinens arbetsområde",
    summary:
      outside.length > 0
        ? `${outside.length} ${outside.length === 1 ? "del ryms" : "delar ryms"} inte inom ${machine.workAreaMm.x} × ${machine.workAreaMm.y} mm.`
        : `Alla delar ryms i ${machine.name}.`,
    calculation: `Delrektangel ≤ ${machine.workAreaMm.x} × ${machine.workAreaMm.y} mm, 90° rotation tillåten`,
    assumptions: ["Fixtur- och vakuumzoner verifieras i produktionssteget"],
    affected_part_ids: outside.map((part) => part.part_id),
  };
}

function nestParts(spec: DesignSpec, parts: ResolvedPart[]): NestingResult {
  const sheetWidth = Math.max(spec.stock_width_mm, 1);
  const sheetHeight = Math.max(spec.stock_height_mm, 1);
  const ordered = [...parts].sort((left, right) => {
    const areaDifference = right.width_mm * right.depth_mm - left.width_mm * left.depth_mm;
    return areaDifference === 0 ? left.part_id.localeCompare(right.part_id) : areaDifference;
  });
  const placements: NestingPlacement[] = [];
  const overflow: string[] = [];
  let sheet = 1;
  let cursorX = NESTING_GAP_MM;
  let cursorY = NESTING_GAP_MM;
  let rowHeight = 0;

  for (const part of ordered) {
    const rawWidth = part.width_mm;
    const rawHeight = part.depth_mm;
    const normalFitsSheet = rawWidth + 2 * NESTING_GAP_MM <= sheetWidth && rawHeight + 2 * NESTING_GAP_MM <= sheetHeight;
    const rotatedFitsSheet = rawHeight + 2 * NESTING_GAP_MM <= sheetWidth && rawWidth + 2 * NESTING_GAP_MM <= sheetHeight;
    if (!normalFitsSheet && !rotatedFitsSheet) {
      overflow.push(part.part_id);
      continue;
    }

    let rotated = false;
    let placementWidth = rawWidth;
    let placementHeight = rawHeight;
    if (cursorX + placementWidth + NESTING_GAP_MM > sheetWidth && rotatedFitsSheet && cursorX + rawHeight + NESTING_GAP_MM <= sheetWidth) {
      rotated = true;
      placementWidth = rawHeight;
      placementHeight = rawWidth;
    }
    if (cursorX + placementWidth + NESTING_GAP_MM > sheetWidth) {
      cursorX = NESTING_GAP_MM;
      cursorY += rowHeight + NESTING_GAP_MM;
      rowHeight = 0;
      rotated = false;
      placementWidth = rawWidth;
      placementHeight = rawHeight;
      if (cursorX + placementWidth + NESTING_GAP_MM > sheetWidth && rotatedFitsSheet) {
        rotated = true;
        placementWidth = rawHeight;
        placementHeight = rawWidth;
      }
    }
    if (cursorY + placementHeight + NESTING_GAP_MM > sheetHeight) {
      sheet += 1;
      if (sheet > spec.stock_count) {
        overflow.push(part.part_id);
        continue;
      }
      cursorX = NESTING_GAP_MM;
      cursorY = NESTING_GAP_MM;
      rowHeight = 0;
      rotated = false;
      placementWidth = rawWidth;
      placementHeight = rawHeight;
      if (cursorX + placementWidth + NESTING_GAP_MM > sheetWidth && rotatedFitsSheet) {
        rotated = true;
        placementWidth = rawHeight;
        placementHeight = rawWidth;
      }
    }
    placements.push({
      part_id: part.part_id,
      sheet,
      x_mm: round(cursorX),
      y_mm: round(cursorY),
      width_mm: round(placementWidth),
      height_mm: round(placementHeight),
      rotated,
    });
    cursorX += placementWidth + NESTING_GAP_MM;
    rowHeight = Math.max(rowHeight, placementHeight);
  }
  const sheetCount = placements.length === 0 ? 0 : Math.max(...placements.map((placement) => placement.sheet));
  const usedArea = placements.reduce((sum, placement) => sum + placement.width_mm * placement.height_mm, 0);
  const totalArea = Math.max(sheetCount * sheetWidth * sheetHeight, 1);
  return {
    placements,
    sheet_count: sheetCount,
    utilization_percent: round((usedArea / totalArea) * 100, 1),
    overflow_part_ids: overflow,
  };
}

function nestingRule(nesting: NestingResult): RuleEvaluation {
  return {
    rule_id: "DFM-STOCK-001",
    rule_version: "1.0.0",
    status: nesting.overflow_part_ids.length > 0 ? "BLOCK" : "PASS",
    title: "Delar ryms i råmaterial",
    summary:
      nesting.overflow_part_ids.length > 0
        ? `${nesting.overflow_part_ids.length} delar ryms inte i valt skivformat.`
        : `${nesting.sheet_count} skivor används med ${nesting.utilization_percent} % materialutnyttjande.`,
    calculation: "Deterministisk radplacering med 8 mm bearbetningsmellanrum",
    calculated_value: nesting.utilization_percent,
    unit: "%",
    assumptions: ["Fiberriktning och visuell fanérmatchning behöver slutkontrolleras"],
    affected_part_ids: nesting.overflow_part_ids,
  };
}

function generateBom(spec: DesignSpec, parts: ResolvedPart[]): BomLine[] {
  const grouped = new Map<string, ResolvedPart[]>();
  for (const part of parts) {
    const key = `${part.kind}:${round(part.width_mm, 1)}:${round(part.depth_mm, 1)}:${round(part.thickness_mm, 1)}`;
    grouped.set(key, [...(grouped.get(key) ?? []), part]);
  }
  const lines: BomLine[] = [...grouped.values()].map((group, index) => {
    const representative = group[0];
    if (!representative) throw new Error("BOM group may not be empty");
    return {
      id: `BOM-P-${String(index + 1).padStart(3, "0")}`,
      category: "part",
      item: representative.name.replace(/ \d+(?:\.\d+)?$/, ""),
      quantity: group.length,
      unit: "st",
      part_ids: group.map((part) => part.part_id),
      dimensions: `${round(representative.width_mm, 1)} × ${round(representative.depth_mm, 1)} × ${round(representative.thickness_mm, 1)} mm`,
      material: spec.material_name,
    };
  });
  return lines;
}

function generateOperations(parts: ResolvedPart[]): CamOperation[] {
  let sequence = 1;
  return parts.flatMap((part) =>
    part.features.map((feature) => ({
      id: `OP-${String(sequence).padStart(4, "0")}`,
      part_id: part.part_id,
      sequence: sequence++,
      operation:
        feature.kind === "outline"
          ? "Konturfräsning"
          : feature.kind === "drill"
            ? "Borrning"
            : feature.kind === "groove"
              ? "Spårfräsning"
              : feature.kind === "pocket"
                ? "Fickfräsning"
                : "Märkning",
      side: feature.face,
      tool: feature.tool_diameter_mm ? `Ø${feature.tool_diameter_mm} mm` : "T1",
      depth_mm: feature.depth_mm,
      status: "PASS" as const,
    })),
  );
}

function overallStatus(rules: RuleEvaluation[]): ValidationStatus {
  if (rules.some((rule) => rule.status === "BLOCK")) return "BLOCK";
  if (rules.some((rule) => rule.status === "WARNING")) return "WARNING";
  return "PASS";
}

export function resolveDesign(spec: DesignSpec, changeDiff: ChangeDiff[] = []): ResolvedDesign {
  const parts = generateParts(spec);
  const nesting = nestParts(spec, parts);
  const rules = [
    invalidGeometryRule(spec),
    shelfRule(spec, parts),
    stabilityRule(spec),
    backRule(spec),
    machineRule(spec, parts),
    nestingRule(nesting),
  ];
  return {
    design_hash: localDesignHash(spec),
    spec,
    parts,
    bom: generateBom(spec, parts),
    operations: generateOperations(parts),
    nesting,
    rule_evaluations: rules,
    status: overallStatus(rules),
    change_diff: changeDiff,
    source: "local",
  };
}

export function applySuggestion(spec: DesignSpec, evaluation: RuleEvaluation): { spec: DesignSpec; diff: ChangeDiff[] } {
  const suggestion = evaluation.suggestion;
  if (!suggestion) return { spec, diff: [] };
  if (suggestion.action === "set_divider_count" && typeof suggestion.value === "number") {
    return {
      spec: { ...spec, divider_count: suggestion.value, reinforcement_mode: "auto" },
      diff: [
        {
          field: "divider_count",
          before: spec.divider_count,
          after: suggestion.value,
          reason: evaluation.summary,
        },
        {
          field: "reinforcement_mode",
          before: spec.reinforcement_mode,
          after: "auto",
          reason: "Förstärkningen godkändes manuellt i valideringspanelen.",
        },
      ],
    };
  }
  if (suggestion.action === "enable_back" && suggestion.value === true) {
    return {
      spec: { ...spec, back_panel: true },
      diff: [
        {
          field: "back_panel",
          before: spec.back_panel,
          after: true,
          reason: evaluation.summary,
        },
      ],
    };
  }
  return { spec, diff: [] };
}

export function partsDoNotOverlap(nesting: NestingResult): boolean {
  return nesting.placements.every((left, leftIndex) =>
    nesting.placements.every((right, rightIndex) => {
      if (leftIndex >= rightIndex || left.sheet !== right.sheet) return true;
      return (
        left.x_mm + left.width_mm <= right.x_mm ||
        right.x_mm + right.width_mm <= left.x_mm ||
        left.y_mm + left.height_mm <= right.y_mm ||
        right.y_mm + right.height_mm <= left.y_mm
      );
    }),
  );
}
