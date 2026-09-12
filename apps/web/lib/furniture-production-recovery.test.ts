import { describe, expect, it } from "vitest";
import fixture from "../components/fixtures/furniture-production-preview.json";
import { designSpecFromServer, type CurrentPrincipal } from "./api-client";
import { DEFAULT_DESIGN_SPEC } from "./design-types";
import type { FurnitureProductionPreview } from "./furniture-workspace";
import { furnitureProductionRecovery, furnitureProductionRecoveryKey, restoreFurnitureProductionRecovery,
  restoreFurnitureProductionState, serializeFurnitureProductionRecovery } from "./furniture-production-recovery";
import { createWorkshopContextDraftState, restoreWorkshopContextDraftState } from "../components/workshop-context-editor";

const source = (fixture as unknown as FurnitureProductionPreview).source_furniture;
const principal: CurrentPrincipal = {
  user_id: "user", organization_id: "org", role: "owner", name: "Designer", email: "fixture@example.test",
};
const spec = designSpecFromServer(fixture.preview.spec, DEFAULT_DESIGN_SPEC);

describe("beredningskopia som strikt avgränsad användarinmatning", () => {
  it("binder lagringen till API, organisation, användare, projekt, revision, geometri och materialkälla", () => {
    const key = (api = "https://api.example.test", actor = principal, binding = source) => (
      furnitureProductionRecoveryKey(api, actor, binding)
    );
    const variants = [key(), key("https://other-api.example.test"), key(undefined, { ...principal, user_id: "other" }),
      key(undefined, { ...principal, organization_id: "other" }),
      key(undefined, principal, { ...source, furniture_design_hash: "b".repeat(64) }),
      key(undefined, principal, { ...source, workspace_sha256: "c".repeat(64) }),
      key(undefined, principal, { ...source, workspace: { ...source.workspace,
        design: { ...source.workspace.design, revision: source.workspace.design.revision + 1 } } }),
      key(undefined, principal, { ...source, workspace: { ...source.workspace,
        design: { ...source.workspace.design, design_id: "other-project" } } }),
    ];
    expect(new Set(variants).size).toBe(variants.length);
  });

  it("återställer enbart deklarerade verkstadsuppgifter och behåller färska servermått", () => {
    const draft = furnitureProductionRecovery({ ...spec, machine_profile_id: "custombuild-router-5125-linuxcnc",
      stock_width_mm: 2500, stock_count: 3 }, source);
    const restored = restoreFurnitureProductionRecovery(JSON.stringify(draft), source, spec);
    expect(restored).toMatchObject({ width_mm: 700.001, measured_thickness_mm: 17.801,
      machine_profile_id: "custombuild-router-5125-linuxcnc", stock_width_mm: 2500, stock_count: 3 });
    expect(Object.keys(draft)).not.toContain("spec");
    expect(Object.keys(draft)).not.toContain("approvals");
  });

  it("avvisar ändrad källa, godkännandefält och felaktiga kontexter", () => {
    const draft = furnitureProductionRecovery(spec, source);
    for (const corrupted of [
      { ...draft, source_workspace_sha256: "f".repeat(64) },
      { ...draft, physical_cutting_authorized: true },
      { ...draft, context: { ...draft.context, approvals: ["approved"] } },
      { ...draft, context: { ...draft.context, stock_count: 0 } },
      { ...draft, context: { ...draft.context, machine_profile_id: "unregistered-machine" } },
    ]) {
      expect(() => restoreFurnitureProductionRecovery(JSON.stringify(corrupted), source, spec)).toThrow();
    }
    expect(() => restoreFurnitureProductionRecovery("å".repeat(140_000), source, spec)).toThrow("för stor");
  });

  it("bevarar ofullständiga råfält utan att lagra giltighet, signaturer eller godkännanden", () => {
    const editor = createWorkshopContextDraftState(spec);
    editor.enabled = editor.dirty = true;
    editor.valid = false;
    editor.draft.profiles[0]!.supplierProfileId = "delvis angiven batch";
    editor.draft.profiles[0]!.sheetWidthMm = "12,fel";
    editor.draft.registrations = [{ stockRole: "", sheetIndex: "", fixtureMethodId: "min fixtur",
      fixtureMethodVersion: "", pinDiameterMm: "", positionToleranceMm: "0,",
      pins: [{ xMm: "10,", yMm: "" }, { xMm: "", yMm: "" }] }];
    const saved = furnitureProductionRecovery(spec, source, editor);
    expect(Object.keys(saved.editor_draft!)).toEqual(["enabled", "draft"]);
    const restored = restoreFurnitureProductionState(serializeFurnitureProductionRecovery(saved), source, spec);
    expect(restored.editorDraft).toEqual(editor.draft);
    expect(restored.spec.workshop_context).toBeUndefined();
    const state = restoreWorkshopContextDraftState(restored.spec, restored.editorDraft!);
    expect(state).toMatchObject({ dirty: true, valid: false, pendingValueSignature: undefined });
    expect(state.sourceBindingSignature).toBe(createWorkshopContextDraftState(restored.spec).sourceBindingSignature);
  });

  it("avvisar förfalskad formulärstatus, oväntade fält och överstora samlingar", () => {
    const editor = { ...createWorkshopContextDraftState(spec), enabled: true, dirty: true, valid: false };
    const saved = furnitureProductionRecovery(spec, source, editor);
    const draft = saved.editor_draft!.draft;
    const profile = draft.profiles[0]!;
    const variants = [
      { ...saved.editor_draft, valid: true },
      { ...saved.editor_draft, pendingValueSignature: "undefined" },
      { ...saved.editor_draft, enabled: false },
      { enabled: true, draft: { ...draft, approved: true } },
      { enabled: true, draft: { ...draft, profiles: [{ ...profile, role: "untrusted" }] } },
      { enabled: true, draft: { ...draft, profiles: draft.profiles.map(row => ({ ...row, sheetCount: 1 })) } },
      { enabled: true, draft: { ...draft, profiles: draft.profiles.map(row => ({ ...row, supplierProfileId: "x".repeat(4097) })) } },
      { enabled: true, draft: { ...draft, profiles: draft.profiles.map(row => ({ ...row, defectZones: Array(101).fill({ xMm: "", yMm: "", widthMm: "", heightMm: "" }) })) } },
      { enabled: true, draft: { ...draft, registrations: Array(201).fill({}) } },
      { enabled: true, draft: { ...draft, registrations: [{ stockRole: "", sheetIndex: "", fixtureMethodId: "",
        fixtureMethodVersion: "", pinDiameterMm: "", positionToleranceMm: "", pins: Array(17).fill({ xMm: "", yMm: "" }) }] } },
    ];
    for (const editor_draft of variants) {
      expect(() => restoreFurnitureProductionState(JSON.stringify({ ...saved, editor_draft }), source, spec)).toThrow();
    }
    const huge = structuredClone(saved);
    huge.editor_draft!.draft.profiles[0]!.defectZones = Array(100).fill({ xMm: "å".repeat(4096), yMm: "", widthMm: "", heightMm: "" });
    expect(() => serializeFurnitureProductionRecovery(huge)).toThrow("för stor");
    expect(() => restoreFurnitureProductionState(JSON.stringify(huge), source, spec)).toThrow("för stor");
  });
});
