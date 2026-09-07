// @vitest-environment node
import { describe, expect, it } from "vitest";
import { assertFurniturePreview, furnitureViewerParts, newFurnitureWorkspace,
  verifiedFurnitureDownload, type FurniturePreview } from "./furniture-workspace";

function fixture(): FurniturePreview {
  return {
    schema_version: "custombuild.furniture-preview.v1", workspace: newFurnitureWorkspace("chest_of_drawers"),
    design: { design_hash: "a".repeat(64), intent_hash: "b".repeat(64), total_weight_g: 200,
      parts: [{ part_id: "drawer-side", semantic_key: "drawer-side", role: "drawer_side",
        actual_thickness_um: 18_000, material_id: "birch-plywood", weight_g: 200,
        finished_size: { width_um: 18_000, depth_um: 450_000, height_um: 200_000 },
        placement: { x_um: 30_000, y_um: 20_000, z_um: 40_000 } }],
      moving_groups: [{ group_id: "drawer", part_ids: ["drawer-side"], travel_um: 450_000 }] },
    rules: { overall_status: "BLOCK", evaluations: [] },
    manufacturing: { state: "not_selected", geometry_compatible: null, issues: [] },
    physical_cutting_authorized: false, production_qualified: false,
  };
}
describe("servergeometri och verifierad nedladdning", () => {
  it("visar exakt skivorientering och flyttar bara vyn när lådan öppnas", () => {
    const source = fixture();
    const original = JSON.stringify(source);
    expect(furnitureViewerParts(source)[0]).toMatchObject({ orientation: "YZ", width_mm: 450,
      depth_mm: 200, thickness_mm: 18, position_mm: { x: 39, y: 245, z: 140 } });
    expect(furnitureViewerParts(source, true)[0]!.position_mm.y).toBe(-205);
    expect(JSON.stringify(source)).toBe(original);
  });
  it("avvisar dubbletter, saknade låddelar och felaktiga produktionsanspråk", () => {
    const source = fixture();
    expect(assertFurniturePreview(source)).toBe(source);
    source.design.parts.push(source.design.parts[0]!);
    expect(() => assertFurniturePreview(source)).toThrow("ogiltiga delar");
    const missing = fixture();
    missing.design.moving_groups[0]!.part_ids = ["missing"];
    expect(() => assertFurniturePreview(missing)).toThrow("Lådrörelsen");
    const unsafe = { ...fixture(), production_qualified: true } as unknown as FurniturePreview;
    expect(() => assertFurniturePreview(unsafe)).toThrow("ogiltigt möbelunderlag");
  });
  it("avvisar ändrade exportbytes även när nedladdningen har lyckad status", async () => {
    const content = new TextEncoder().encode("review-package");
    const hash = Buffer.from(await crypto.subtle.digest("SHA-256", content)).toString("hex");
    const result = { state: "succeeded" as const, content_base64: Buffer.from(content).toString("base64"), sha256: hash };
    expect(await verifiedFurnitureDownload(result)).toEqual(content);
    await expect(verifiedFurnitureDownload({ ...result, content_base64: btoa("changed") })).rejects.toThrow("kontrollsumma");
  });
});
