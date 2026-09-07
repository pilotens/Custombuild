// @vitest-environment node
import { describe, expect, it } from "vitest";
import { viewerPartTransform } from "../components/furniture-viewer";
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
    expect(furnitureViewerParts(source)[0]).toMatchObject({ orientation: "YZ", width_mm: 200,
      depth_mm: 450, thickness_mm: 18, position_mm: { x: 39, y: 245, z: 140 } });
    expect(furnitureViewerParts(source, true)[0]!.position_mm.y).toBe(-205);
    expect(JSON.stringify(source)).toBe(original);
  });
  it.each([
    ["left_side", [18_000, 320_000, 1_800_000]],
    ["right_side", [18_000, 500_000, 900_000]],
    ["divider", [18_000, 300_000, 1_740_000]],
    ["table_end", [18_000, 580_000, 722_000]],
    ["drawer_side", [18_000, 450_000, 200_000]],
    ["back", [800_000, 6_000, 900_000]],
    ["table_stretcher", [840_000, 18_000, 100_000]],
    ["drawer_front", [760_000, 18_000, 260_000]],
    ["top", [1_000_000, 600_000, 18_000]],
    ["shelf", [850_000, 300_000, 18_000]],
    ["drawer_bottom", [720_000, 450_000, 6_000]],
  ] as const)("renderar %s med samma verkliga utbredning som CAD-underlaget", (role, [x, y, z]) => {
    const source = fixture();
    const part = source.design.parts[0]!;
    part.role = role;
    part.finished_size = { width_um: x, depth_um: y, height_um: z };
    const size = { widthMm: 1_000, heightMm: 1_800, depthMm: 600 };
    const rendered = viewerPartTransform(furnitureViewerParts(source)[0]!, size, false);
    // Three.js uses X, Z, -Y relative to the design centre, in metres.
    expect(rendered.scale).toEqual([x / 1_000_000, z / 1_000_000, y / 1_000_000]);
    const expectedCentre = [
      (part.placement.x_um + x / 2) / 1_000_000 - size.widthMm / 2_000,
      (part.placement.z_um + z / 2) / 1_000_000 - size.heightMm / 2_000,
      -((part.placement.y_um + y / 2) / 1_000_000 - size.depthMm / 2_000),
    ];
    rendered.position.forEach((position, axis) => expect(position).toBeCloseTo(expectedCentre[axis]!, 12));
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
