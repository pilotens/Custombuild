import { beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_DESIGN_SPEC } from "./design-types";
import { DEFAULT_PLANNING_BRIEF } from "./furniture-planning";
import {
  ANONYMOUS_PROJECT_ID,
  legacyWorkspaceDraftKey,
  projectSelectionKey,
  readSelectedProject,
  readWorkspaceDraft,
  replaceQuarantinedWorkspaceDraft,
  workspaceDraftKey,
  writeSelectedProject,
  writeWorkspaceDraft,
} from "./workspace-draft-storage";
import { DEFAULT_WORKSPACE_UI_STATE } from "./workspace-ui-state";
import { MAX_LOCAL_DRAFT_BYTES } from "./workspace-design-envelope";

const userA = { organization_id: "org-a", user_id: "user-a" };
const userB = { organization_id: "org-b", user_id: "user-b" };

beforeEach(() => window.localStorage.clear());

describe("scoped workspace drafts", () => {
  it("keeps the A -> logout -> B flow isolated by identity and project", () => {
    writeWorkspaceDraft(window.localStorage, userA, "project-a", {
      spec: { ...DEFAULT_DESIGN_SPEC, width_mm: 3_100 },
      templateId: "wall-library",
      workspaceSelected: true,
      planningBrief: DEFAULT_PLANNING_BRIEF,
    });

    // Logout changes the scope to the explicitly anonymous local workspace.
    expect(readWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID)).toBeUndefined();
    writeWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID, {
      spec: { ...DEFAULT_DESIGN_SPEC, width_mm: 900 },
      templateId: "shelving",
      workspaceSelected: true,
    });

    // A subsequent login as B cannot discover or import A's local draft.
    expect(readWorkspaceDraft(window.localStorage, userB, "project-b")).toBeUndefined();
    expect(readWorkspaceDraft(window.localStorage, userA, "project-a")?.spec.width_mm).toBe(3_100);
    expect(readWorkspaceDraft(window.localStorage, userA, "project-a")?.planningBrief).toEqual(DEFAULT_PLANNING_BRIEF);
    expect(readWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID)?.spec.width_mm).toBe(900);
    expect(workspaceDraftKey(userA, "project-a")).not.toBe(workspaceDraftKey(userB, "project-b"));
  });

  it("isolates two projects owned by the same identity", () => {
    writeWorkspaceDraft(window.localStorage, userA, "project-1", {
      spec: { ...DEFAULT_DESIGN_SPEC, width_mm: 1_200 },
      templateId: "shelving",
      workspaceSelected: true,
    });
    writeWorkspaceDraft(window.localStorage, userA, "project-2", {
      spec: { ...DEFAULT_DESIGN_SPEC, width_mm: 2_400 },
      templateId: "wall-library",
      workspaceSelected: true,
    });

    expect(readWorkspaceDraft(window.localStorage, userA, "project-1")?.spec.width_mm).toBe(1_200);
    expect(readWorkspaceDraft(window.localStorage, userA, "project-2")?.spec.width_mm).toBe(2_400);
  });

  it("persists the desired bay-width intent across a reload", () => {
    writeWorkspaceDraft(window.localStorage, userA, "project-width-layout", {
      spec: {
        ...DEFAULT_DESIGN_SPEC,
        width_mm: 4_200,
        bay_sizing_mode: "target_width",
        target_bay_width_mm: 300,
        divider_count: 12,
        bay_width_ratios: [],
        reinforcement_mode: "manual",
      },
      templateId: "wall-library",
      workspaceSelected: true,
    });

    const reloaded = readWorkspaceDraft(window.localStorage, userA, "project-width-layout");
    expect(reloaded?.spec).toMatchObject({
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 300,
      divider_count: 12,
      bay_width_ratios: [],
    });
  });

  it("persists an explicit back material and migrates a legacy missing value", () => {
    writeWorkspaceDraft(window.localStorage, userA, "project-back-material", {
      spec: {
        ...DEFAULT_DESIGN_SPEC,
        back_material_id: "mdf-6",
        back_panel_type: "surface_mounted",
      },
      templateId: "shelving",
      workspaceSelected: true,
    });
    const stored = JSON.parse(
      window.localStorage.getItem(workspaceDraftKey(userA, "project-back-material")) ?? "{}",
    ) as { spec?: Record<string, unknown> };
    expect(stored.spec?.back_material_id).toBe("mdf-6");
    expect(stored.spec?.back_panel_type).toBe("surface_mounted");
    expect(readWorkspaceDraft(
      window.localStorage,
      userA,
      "project-back-material",
    )?.spec.back_material_id).toBe("mdf-6");
    expect(readWorkspaceDraft(
      window.localStorage,
      userA,
      "project-back-material",
    )?.spec.back_panel_type).toBe("surface_mounted");

    const legacyKey = legacyWorkspaceDraftKey(userA, "legacy-back-material");
    const legacySpec = {
      ...DEFAULT_DESIGN_SPEC,
      material_id: "mdf",
      material_name: "MDF",
    } as Record<string, unknown>;
    delete legacySpec.back_material_id;
    window.localStorage.setItem(legacyKey, JSON.stringify({
      version: 2,
      spec: legacySpec,
      templateId: "shelving",
      workspaceSelected: true,
      updatedAt: "2026-08-12T20:00:00.000Z",
    }));
    expect(readWorkspaceDraft(
      window.localStorage,
      userA,
      "legacy-back-material",
    )?.spec.back_material_id).toBe("mdf-6");
    expect(readWorkspaceDraft(
      window.localStorage,
      userA,
      "legacy-back-material",
    )?.spec.back_panel_type).toBe("inset_groove");
  });

  it("persists UI-only context in v3 without merging it into DesignSpec", () => {
    writeWorkspaceDraft(window.localStorage, userA, "project-ui", {
      spec: { ...DEFAULT_DESIGN_SPEC, width_mm: 2_700 },
      templateId: "wall-library",
      workspaceSelected: true,
      uiState: {
        schemaVersion: 2,
        mode: "studio",
        viewMode: "front",
        exploded: true,
        transparent: false,
        isolateSelection: true,
        selectedPartId: "shelf-2-bay-1",
        panels: {
          componentLibraryOpen: false,
          contextPanelOpen: true,
          advancedPanelOpen: true,
        },
      },
    });

    const stored = JSON.parse(window.localStorage.getItem(workspaceDraftKey(userA, "project-ui")) ?? "{}") as Record<string, unknown>;
    const reloaded = readWorkspaceDraft(window.localStorage, userA, "project-ui");
    expect(stored.version).toBe(3);
    expect(reloaded?.version).toBe(3);
    expect(reloaded?.uiState).toMatchObject({
      mode: "studio",
      viewMode: "front",
      selectedPartId: "shelf-2-bay-1",
    });
    expect(reloaded?.spec).not.toHaveProperty("uiState");
    expect(reloaded?.spec).not.toHaveProperty("editorMode");
  });

  it("reads a scoped v2 draft as v3 with safe UI defaults", () => {
    const legacyKey = legacyWorkspaceDraftKey(userA, "legacy-project");
    window.localStorage.setItem(legacyKey, JSON.stringify({
      version: 2,
      spec: { ...DEFAULT_DESIGN_SPEC, width_mm: 1_650 },
      templateId: "shelving",
      workspaceSelected: true,
      planningBrief: DEFAULT_PLANNING_BRIEF,
      updatedAt: "2026-08-12T20:00:00.000Z",
    }));

    const migrated = readWorkspaceDraft(window.localStorage, userA, "legacy-project");
    expect(migrated).toMatchObject({
      version: 3,
      templateId: "shelving",
      workspaceSelected: true,
      uiState: DEFAULT_WORKSPACE_UI_STATE,
    });
    expect(migrated?.spec.width_mm).toBe(1_650);
  });

  it("keeps a valid draft while sanitizing malformed UI-only fields", () => {
    window.localStorage.setItem(workspaceDraftKey(userA, "partially-invalid-ui"), JSON.stringify({
      version: 3,
      spec: DEFAULT_DESIGN_SPEC,
      templateId: "shelving",
      workspaceSelected: true,
      uiState: {
        mode: "not-a-mode",
        viewMode: "side",
        exploded: "true",
        selectedPartId: "../../invalid",
        panels: { contextPanelOpen: false },
      },
      updatedAt: "2026-08-13T00:00:00.000Z",
    }));

    expect(readWorkspaceDraft(window.localStorage, userA, "partially-invalid-ui")?.uiState).toEqual({
      ...DEFAULT_WORKSPACE_UI_STATE,
      viewMode: "side",
      panels: {
        ...DEFAULT_WORKSPACE_UI_STATE.panels,
        contextPanelOpen: false,
      },
    });
  });

  it("preserves an invalid raw draft while refusing to hydrate it", () => {
    const key = workspaceDraftKey(userA, "invalid-project");
    const raw = JSON.stringify({
      version: 3,
      spec: { ...DEFAULT_DESIGN_SPEC, width_mm: "2400" },
      templateId: "wall-library",
      workspaceSelected: true,
      updatedAt: "2026-08-13T00:00:00.000Z",
    });
    window.localStorage.setItem(key, raw);

    expect(readWorkspaceDraft(window.localStorage, userA, "invalid-project")).toBeUndefined();
    expect(window.localStorage.getItem(key)).toBe(raw);

    expect(() => writeWorkspaceDraft(window.localStorage, userA, "invalid-project", {
      spec: DEFAULT_DESIGN_SPEC,
      templateId: "shelving",
      workspaceSelected: true,
    })).toThrow(/karantän/i);
    expect(window.localStorage.getItem(key)).toBe(raw);

    replaceQuarantinedWorkspaceDraft(window.localStorage, userA, "invalid-project", {
      spec: DEFAULT_DESIGN_SPEC,
      templateId: "shelving",
      workspaceSelected: true,
    });
    expect(readWorkspaceDraft(window.localStorage, userA, "invalid-project")?.spec.width_mm)
      .toBe(DEFAULT_DESIGN_SPEC.width_mm);
  });

  it("rejects oversized raw storage before JSON parsing and preserves it", () => {
    const key = workspaceDraftKey(userA, "oversized-project");
    const raw = "x".repeat(MAX_LOCAL_DRAFT_BYTES + 1);
    window.localStorage.setItem(key, raw);
    const parseSpy = vi.spyOn(JSON, "parse");

    expect(readWorkspaceDraft(window.localStorage, userA, "oversized-project")).toBeUndefined();
    expect(parseSpy).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(key)).toBe(raw);
    parseSpy.mockRestore();
  });

  it("rejects invalid local writes before persistence", () => {
    expect(() => writeWorkspaceDraft(window.localStorage, userA, "invalid-write", {
      spec: { ...DEFAULT_DESIGN_SPEC, shelf_count: Number.NaN },
      templateId: "shelving",
      workspaceSelected: true,
    })).toThrow(/shelf_count/);
    expect(window.localStorage.getItem(workspaceDraftKey(userA, "invalid-write"))).toBeNull();
  });

  it("stores the selected project within the authenticated identity", () => {
    writeSelectedProject(window.localStorage, userA, { id: "project-a", name: "A" });
    expect(readSelectedProject(window.localStorage, userA)).toEqual({ id: "project-a", name: "A" });
    expect(readSelectedProject(window.localStorage, userB)).toBeUndefined();
    expect(projectSelectionKey(userA)).not.toBe(projectSelectionKey(userB));
  });

  it("retains the selected project from the identity-scoped v2 key", () => {
    const legacySelectionKey = projectSelectionKey(userA).replace(
      "custombuild:workspace:v3",
      "custombuild:workspace:v2",
    );
    window.localStorage.setItem(legacySelectionKey, JSON.stringify({
      id: "legacy-project",
      name: "Tidigare projekt",
    }));

    expect(readSelectedProject(window.localStorage, userA)).toEqual({
      id: "legacy-project",
      name: "Tidigare projekt",
    });
  });
});
