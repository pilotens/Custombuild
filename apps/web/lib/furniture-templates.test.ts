import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import screenedTemplateDefaults from "../../../packages/contracts/screened-template-defaults.v1.json";
import { adaptStructuralSupports, balanceDesignSymmetry, resolveDesign } from "./design-engine";
import { DEFAULT_DESIGN_SPEC, type DesignSpec, type ResolvedPart } from "./design-types";
import {
  compatibleFurnitureTemplateId,
  FURNITURE_TEMPLATES,
  isReferenceImageDesign,
  templatePreviewGeometry,
  validateTemplatePreview,
} from "./furniture-templates";
import { referenceVerificationFingerprint } from "./reference-image";
import { DEFAULT_PLANNING_BRIEF, templateWithPlanningBrief } from "./furniture-planning";
import { parseLocalDesignSpec } from "./workspace-design-envelope";

const SCREENED_TEMPLATE_DEFAULTS_V1_3_SHA256 = "ec20a539e2bef2478d18a66519331ceb1067388551419ead9df337e72ecd2b71";

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(record[key])}`
    )).join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new TypeError("The defaults contract cannot contain undefined values.");
  return encoded;
}

function effectiveScreenedTemplateDefault(
  templateId: "shelving" | "wall-library",
  identity: Pick<DesignSpec, "design_id" | "revision"> = {
    design_id: DEFAULT_DESIGN_SPEC.design_id,
    revision: DEFAULT_DESIGN_SPEC.revision,
  },
) {
  const template = FURNITURE_TEMPLATES.find((candidate) => candidate.id === templateId)!;
  const planned = templateWithPlanningBrief(template, {
    ...DEFAULT_PLANNING_BRIEF,
    startMode: "template",
    selectedTemplateId: templateId,
  });
  const bounded = parseLocalDesignSpec({
    ...DEFAULT_DESIGN_SPEC,
    ...identity,
    ...planned.patch,
  });
  const adapted = adaptStructuralSupports(balanceDesignSymmetry(bounded));
  const normalized = parseLocalDesignSpec(balanceDesignSymmetry(adapted.spec));
  const { design_id: designId, revision, ...identityIndependentSpec } = normalized;
  return { adapted, designId, identityIndependentSpec, planned, revision };
}

function partBounds(part: ResolvedPart) {
  const size = part.orientation === "YZ"
    ? { x: part.thickness_mm, y: part.depth_mm, z: part.width_mm }
    : part.orientation === "XZ"
      ? { x: part.width_mm, y: part.thickness_mm, z: part.depth_mm }
      : { x: part.width_mm, y: part.depth_mm, z: part.thickness_mm };
  return {
    min: {
      x: part.position_mm.x - size.x / 2,
      y: part.position_mm.y - size.y / 2,
      z: part.position_mm.z - size.z / 2,
    },
    max: {
      x: part.position_mm.x + size.x / 2,
      y: part.position_mm.y + size.y / 2,
      z: part.position_mm.z + size.z / 2,
    },
  };
}

function hasVolumetricOverlap(left: ResolvedPart, right: ResolvedPart): boolean {
  const a = partBounds(left);
  const b = partBounds(right);
  const epsilon = 0.001;
  return (["x", "y", "z"] as const).every((axis) => (
    Math.min(a.max[axis], b.max[axis]) - Math.max(a.min[axis], b.min[axis]) > epsilon
  ));
}

function isModeledCapture(left: ResolvedPart, right: ResolvedPart): boolean {
  const parts = [left, right];
  const back = parts.find((part) => part.kind === "back");
  const backMate = parts.find((part) => part !== back);
  if (back && backMate && ["side", "divider", "top", "bottom"].includes(backMate.kind)) return true;

  const horizontal = parts.find((part) => ["top", "bottom", "shelf"].includes(part.kind));
  const vertical = parts.find((part) => ["side", "divider"].includes(part.kind));
  if (horizontal && vertical) return true;

  const baseBottom = parts.find((part) => part.kind === "base_bottom");
  const baseBottomSupport = parts.find((part) => part !== baseBottom);
  if (baseBottom && baseBottomSupport && ["side", "base_side"].includes(baseBottomSupport.kind)) return true;

  const baseSide = parts.find((part) => part.kind === "base_side");
  const upperBottom = parts.find((part) => part.part_id === "bottom");
  if (baseSide && upperBottom) return true;

  const plinth = parts.find((part) => part.part_id === "plinth-front");
  const plinthBottom = parts.find((part) => part.part_id === "bottom");
  return Boolean(plinth && plinthBottom);
}

describe("furniture template visual contracts", () => {
  it("keeps the selected template compatible when a semantic edit changes furniture family", () => {
    expect(compatibleFurnitureTemplateId("shelving", "bookcase")).toBe("shelving");
    expect(compatibleFurnitureTemplateId("room-divider", "bookcase")).toBe("room-divider");
    expect(compatibleFurnitureTemplateId("shelving", "wall_library")).toBe("wall-library");
    expect(compatibleFurnitureTemplateId("sideboard", "wall_library")).toBe("sideboard");
    expect(compatibleFurnitureTemplateId("cupboard", "bookcase")).toBe("shelving");
  });

  it("binds current screened UI defaults to the versioned non-release contract", () => {
    expect(screenedTemplateDefaults.schema_version).toBe("custombuild.screened-template-defaults.v1");
    expect(screenedTemplateDefaults.contract_version).toBe("1.3.0");
    expect(screenedTemplateDefaults.identity_policy).toEqual({
      design_id: "preserve_current_project",
      revision: "preserve_current_project",
    });
    expect(screenedTemplateDefaults.physical_cutting_authorized).toBe(false);
    expect(screenedTemplateDefaults.default_planning_brief).toEqual(DEFAULT_PLANNING_BRIEF);

    const { fingerprint, ...unsignedContract } = screenedTemplateDefaults;
    const digest = createHash("sha256").update(canonicalJson(unsignedContract), "utf8").digest("hex");
    expect(fingerprint).toEqual({
      algorithm: "sha256",
      canonicalization: "UTF-8 JSON with recursively sorted object keys, compact separators, ensure_ascii=false, and the top-level fingerprint member omitted",
      value: SCREENED_TEMPLATE_DEFAULTS_V1_3_SHA256,
    });
    expect(digest).toBe(SCREENED_TEMPLATE_DEFAULTS_V1_3_SHA256);

    expect(screenedTemplateDefaults.templates.map((entry) => entry.template_id)).toEqual(["shelving"]);
    for (const entry of screenedTemplateDefaults.templates) {
      const templateId = entry.template_id;
      if (templateId !== "shelving") {
        throw new TypeError(`Unknown screened template default: ${templateId}`);
      }
      const result = effectiveScreenedTemplateDefault(templateId);
      expect(entry.production_level).toBe("screened");
      expect(entry.planning_selection).toEqual({
        startMode: "template",
        selectedTemplateId: templateId,
      });
      expect(entry.effective_design_spec).not.toHaveProperty("design_id");
      expect(entry.effective_design_spec).not.toHaveProperty("revision");
      expect(result.identityIndependentSpec).toEqual(entry.effective_design_spec);
    }
  });

  it("pins structural adaptation and preserves project identity for screened defaults", () => {
    const identity = { design_id: "project-screened-default-parity", revision: 7 };
    const shelving = effectiveScreenedTemplateDefault("shelving", identity);
    const wallLibrary = effectiveScreenedTemplateDefault("wall-library", identity);

    expect(shelving.adapted.diff).toEqual([]);
    expect(shelving.designId).toBe(identity.design_id);
    expect(shelving.revision).toBe(identity.revision);
    expect(wallLibrary.planned.patch).toMatchObject({ divider_count: 4, base_cabinet_count: 4 });
    expect(wallLibrary.adapted.diff.map(({ field, before, after }) => ({ field, before, after }))).toEqual([
      { field: "divider_count", before: 4, after: 2 },
      { field: "base_cabinet_count", before: 4, after: 3 },
    ]);
    expect(wallLibrary.identityIndependentSpec).toMatchObject({
      divider_count: 2,
      base_cabinet_height_mm: 680,
      base_cabinet_depth_mm: 340,
      base_cabinet_count: 3,
    });
    expect(wallLibrary.designId).toBe(identity.design_id);
    expect(wallLibrary.revision).toBe(identity.revision);
  });

  it.each(FURNITURE_TEMPLATES)("validates $name against its actual design parameters", (template) => {
    expect(validateTemplatePreview(template)).toEqual([]);

    const geometry = templatePreviewGeometry(template);
    expect(geometry.openColumns).toBe((template.patch.divider_count ?? 0) + 1);
    expect(geometry.shelfLines).toBe(template.patch.shelf_count);
    expect(geometry.hasBase).toBe(template.patch.furniture_type === "wall_library");
    expect(geometry.baseColumns).toBe(template.patch.base_cabinet_count);
  });

  it.each(FURNITURE_TEMPLATES)("keeps every $name part inside its declared outer dimensions", (template) => {
    const result = resolveDesign({ ...DEFAULT_DESIGN_SPEC, ...template.patch });
    const limits = {
      x: result.spec.width_mm,
      y: result.spec.depth_mm,
      z: result.spec.height_mm,
    };
    const tolerance = 0.001;

    result.parts.forEach((part) => {
      const bounds = partBounds(part);
      (["x", "y", "z"] as const).forEach((axis) => {
        expect(bounds.min[axis], `${template.id}/${part.part_id} ${axis}-minimum`).toBeGreaterThanOrEqual(-tolerance);
        expect(bounds.max[axis], `${template.id}/${part.part_id} ${axis}-maximum`).toBeLessThanOrEqual(limits[axis] + tolerance);
      });
    });

    for (let leftIndex = 0; leftIndex < result.parts.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < result.parts.length; rightIndex += 1) {
        const left = result.parts[leftIndex]!;
        const right = result.parts[rightIndex]!;
        if (!hasVolumetricOverlap(left, right)) continue;
        expect(
          isModeledCapture(left, right),
          `${template.id}/${left.part_id} overlaps ${right.part_id} without a modeled capture`,
        ).toBe(true);
      }
    }

    if (result.spec.furniture_type === "wall_library") {
      expect(result.spec.base_cabinet_depth_mm).toBe(result.spec.depth_mm);
      const fronts = result.parts.filter((part) => part.kind === "cabinet_front");
      const horizontalSolids = result.parts.filter((part) => part.kind === "base_bottom" || part.part_id === "bottom");
      fronts.forEach((front) => {
        horizontalSolids.forEach((solid) => {
          expect(hasVolumetricOverlap(front, solid), `${template.id}/${front.part_id} vs ${solid.part_id}`).toBe(false);
        });
      });
      const plinth = result.parts.find((part) => part.part_id === "plinth-front");
      if (plinth) expect(partBounds(plinth).min.y).toBeCloseTo(0, 3);
    }
  });

  it.each(FURNITURE_TEMPLATES)("keeps the live default $name selection inside its outer dimensions", (template) => {
    const planned = templateWithPlanningBrief(template, {
      ...DEFAULT_PLANNING_BRIEF,
      startMode: "template",
      selectedTemplateId: template.id,
    });
    const result = resolveDesign({ ...DEFAULT_DESIGN_SPEC, ...planned.patch });
    const limits = { x: result.spec.width_mm, y: result.spec.depth_mm, z: result.spec.height_mm };
    const tolerance = 0.001;

    result.parts.forEach((part) => {
      const bounds = partBounds(part);
      (["x", "y", "z"] as const).forEach((axis) => {
        expect(bounds.min[axis], `${template.id}/planned/${part.part_id} ${axis}-minimum`).toBeGreaterThanOrEqual(-tolerance);
        expect(bounds.max[axis], `${template.id}/planned/${part.part_id} ${axis}-maximum`).toBeLessThanOrEqual(limits[axis] + tolerance);
      });
    });

    for (let leftIndex = 0; leftIndex < result.parts.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < result.parts.length; rightIndex += 1) {
        const left = result.parts[leftIndex]!;
        const right = result.parts[rightIndex]!;
        if (!hasVolumetricOverlap(left, right)) continue;
        expect(
          isModeledCapture(left, right),
          `${template.id}/planned/${left.part_id} overlaps ${right.part_id} without a modeled capture`,
        ).toBe(true);
      }
    }
  });

  it("keeps an explicit, reviewable signature for every preview", () => {
    const signatures = Object.fromEntries(FURNITURE_TEMPLATES.map((template) => {
      const geometry = templatePreviewGeometry(template);
      return [template.id, {
        preview: template.preview,
        openColumns: geometry.openColumns,
        shelfLines: geometry.shelfLines,
        hasBase: geometry.hasBase,
        baseColumns: geometry.baseColumns,
        showBackEdge: geometry.showBackEdge,
      }];
    }));

    expect(signatures).toEqual({
      shelving: { preview: "shelf", openColumns: 3, shelfLines: 5, hasBase: false, baseColumns: 0, showBackEdge: true },
      "wall-library": { preview: "library", openColumns: 5, shelfLines: 5, hasBase: true, baseColumns: 4, showBackEdge: true },
      sideboard: { preview: "low", openColumns: 3, shelfLines: 1, hasBase: true, baseColumns: 3, showBackEdge: true },
      "room-divider": { preview: "divider", openColumns: 4, shelfLines: 4, hasBase: false, baseColumns: 0, showBackEdge: false },
      "hanging-shelf": { preview: "hanging", openColumns: 1, shelfLines: 3, hasBase: false, baseColumns: 0, showBackEdge: true },
      cupboard: { preview: "cupboard", openColumns: 3, shelfLines: 3, hasBase: true, baseColumns: 3, showBackEdge: true },
    });
  });

  it("only advertises archetypes backed by complete production evidence as screened", () => {
    expect(FURNITURE_TEMPLATES.filter((template) => template.productionLevel === "screened").map((template) => template.id)).toEqual(["shelving"]);
    expect(FURNITURE_TEMPLATES.filter((template) => template.productionLevel === "concept").every((template) => Boolean(template.limitation))).toBe(true);
    expect(FURNITURE_TEMPLATES.find((template) => template.id === "wall-library")).toMatchObject({
      productionLevel: "concept",
      archetypeLabel: "Väggbiblioteksstomme · koncept",
      limitation: expect.stringMatching(/gångjärn.*borrbilder.*retention/i),
    });
  });

  it("recognizes persisted reference-image provenance as concept geometry", () => {
    expect(isReferenceImageDesign({
      ...DEFAULT_DESIGN_SPEC,
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
    })).toBe(true);
  });

  it("only promotes a reference image while its confirmations match the exact model", () => {
    const base = {
      ...DEFAULT_DESIGN_SPEC,
      reference_image_import: {
        source: "reference_image" as const,
        import_id: "11111111-1111-4111-8111-111111111111",
        image_sha256: "a".repeat(64),
        file_name: "verifierad.jpg",
        image_width_px: 1200,
        image_height_px: 800,
        confidence: 0.84,
        detected_shelves: 5,
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
    const verified = {
      ...base,
      reference_image_import: {
        ...base.reference_image_import,
        verified_model_fingerprint: referenceVerificationFingerprint(base),
      },
    };

    expect(isReferenceImageDesign(verified)).toBe(false);
    expect(isReferenceImageDesign({ ...verified, shelf_count: 6 })).toBe(true);
  });
});
