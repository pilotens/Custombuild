import { describe, expect, it } from "vitest";
import {
  DEFAULT_WORKSPACE_UI_STATE,
  sanitizeWorkspaceUiState,
  workspaceModeFromLegacyStep,
} from "./workspace-ui-state";

describe("workspace UI state", () => {
  it("accepts the complete four-mode UI state without changing it", () => {
    expect(sanitizeWorkspaceUiState({
      schemaVersion: 2,
      mode: "build",
      viewMode: "top",
      exploded: true,
      transparent: true,
      isolateSelection: true,
      selectedPartId: "shelf-2-bay-3",
      panels: {
        componentLibraryOpen: false,
        contextPanelOpen: true,
        advancedPanelOpen: true,
      },
    })).toEqual({
      schemaVersion: 2,
      mode: "build",
      viewMode: "top",
      exploded: true,
      transparent: true,
      isolateSelection: true,
      selectedPartId: "shelf-2-bay-3",
      panels: {
        componentLibraryOpen: false,
        contextPanelOpen: true,
        advancedPanelOpen: true,
      },
    });
  });

  it.each([
    ["discovery", "explore"],
    ["constraints", "studio"],
    ["direction", "studio"],
    ["composition", "studio"],
    ["verification", "check"],
    ["deliverables", "build"],
  ] as const)("migrates legacy step %s to %s", (step, mode) => {
    expect(sanitizeWorkspaceUiState({
      schemaVersion: 1,
      step,
      editorMode: "guided",
    }).mode).toBe(mode);
    expect(workspaceModeFromLegacyStep(step)).toBe(mode);
  });

  it("prefers an explicit product mode over obsolete wizard fields", () => {
    expect(sanitizeWorkspaceUiState({
      schemaVersion: 2,
      mode: "check",
      step: "discovery",
      editorMode: "guided",
    })).toMatchObject({ schemaVersion: 2, mode: "check" });
  });

  it("rejects inherited object keys as legacy steps", () => {
    expect(workspaceModeFromLegacyStep("toString")).toBe("explore");
  });

  it("sanitizes every untrusted field independently", () => {
    expect(sanitizeWorkspaceUiState({
      schemaVersion: 999,
      mode: "unknown",
      step: "also-unknown",
      editorMode: "expert",
      viewMode: "xray",
      exploded: "yes",
      transparent: true,
      isolateSelection: 1,
      selectedPartId: "<script>alert(1)</script>",
      panels: {
        componentLibraryOpen: false,
        contextPanelOpen: "yes",
        advancedPanelOpen: true,
      },
      canonicalSpec: { width_mm: 9_999 },
    })).toEqual({
      ...DEFAULT_WORKSPACE_UI_STATE,
      transparent: true,
      panels: {
        componentLibraryOpen: false,
        contextPanelOpen: true,
        advancedPanelOpen: true,
      },
    });
  });

  it("returns a fresh default tree for absent or malformed values", () => {
    const first = sanitizeWorkspaceUiState(undefined);
    const second = sanitizeWorkspaceUiState([]);

    expect(first).toEqual(DEFAULT_WORKSPACE_UI_STATE);
    expect(second).toEqual(DEFAULT_WORKSPACE_UI_STATE);
    expect(first).not.toBe(DEFAULT_WORKSPACE_UI_STATE);
    expect(first.panels).not.toBe(DEFAULT_WORKSPACE_UI_STATE.panels);
  });

  it("does not retain wizard or editor-mode fields after migration", () => {
    const migrated = sanitizeWorkspaceUiState({
      schemaVersion: 1,
      step: "composition",
      editorMode: "free",
    });

    expect(migrated).not.toHaveProperty("step");
    expect(migrated).not.toHaveProperty("editorMode");
  });
});
