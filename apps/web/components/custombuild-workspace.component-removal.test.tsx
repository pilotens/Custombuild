import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DesignSpec, ResolvedPart } from "@/lib/design-types";

interface ViewerSnapshot {
  parts: ResolvedPart[];
  semanticSpec: DesignSpec;
  selectedPartId?: string;
  onSelectPart: (partId?: string) => void;
  onResizeStart: () => void;
  onResize: (patch: Partial<Pick<DesignSpec, "width_mm" | "height_mm" | "depth_mm">>) => void;
  onResizeEnd: () => void;
  onPartHorizontalMoveStart: () => void;
  onPartHorizontalMove: (partId: string, x: number) => void;
  onPartHorizontalMoveEnd: () => void;
}

const viewer = vi.hoisted(() => ({ current: undefined as ViewerSnapshot | undefined }));

vi.mock("next/dynamic", () => ({
  default: () => function MockFurnitureViewer(props: ViewerSnapshot) {
    viewer.current = props;
    return (
      <div data-testid="furniture-viewer" data-selected-part-id={props.selectedPartId ?? ""}>
        <button type="button" onClick={() => props.onSelectPart("divider-1")}>
          Välj första avdelaren i modellen
        </button>
      </div>
    );
  },
}));

vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  class MockCustombuildApiClient {
    readonly baseUrl = "";
    readonly configured = false;
    readonly authenticated = false;
  }
  return { ...actual, CustombuildApiClient: MockCustombuildApiClient };
});

import { DEFAULT_DESIGN_SPEC } from "@/lib/design-types";
import { resolveDesign } from "@/lib/design-engine";
import {
  ANONYMOUS_PROJECT_ID,
  readWorkspaceDraft,
  writeWorkspaceDraft,
} from "@/lib/workspace-draft-storage";
import { DEFAULT_WORKSPACE_UI_STATE } from "@/lib/workspace-ui-state";
import { CustombuildWorkspace } from "./custombuild-workspace";

function currentViewer(): ViewerSnapshot {
  if (!viewer.current) throw new Error("The workspace has not rendered its model.");
  return viewer.current;
}

function dividerPositions(): number[] {
  return currentViewer().parts.filter((part) => part.kind === "divider")
    .map((part) => part.position_mm.x);
}

async function renderDraft(patch: Partial<DesignSpec> = {}) {
  const spec = {
    ...DEFAULT_DESIGN_SPEC,
    divider_count: 3,
    bay_width_ratios: [0.2, 0.25, 0.25, 0.3],
    shelf_count: 2,
    shelf_height_ratios: [0.25, 0.6],
    symmetry_locked: false,
    reinforcement_mode: "manual" as const,
    ...patch,
  };
  writeWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID, {
    spec,
    templateId: spec.furniture_type === "wall_library" ? "wall-library" : "shelving",
    workspaceSelected: true,
    uiState: { ...DEFAULT_WORKSPACE_UI_STATE, mode: "studio" },
  });
  const result = render(<CustombuildWorkspace />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByTestId("furniture-viewer")).toBeInTheDocument();
  return result;
}

describe("component palette removal in the offline workspace", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    viewer.current = undefined;
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/?mode=studio");
  });

  afterEach(() => { vi.useRealTimers(); });

  it("repairs the saved library's planned material quantity before showing or saving it", async () => {
    await renderDraft({
      furniture_type: "wall_library", width_mm: 2400, height_mm: 2400, depth_mm: 340,
      divider_count: 3, shelf_count: 5, bay_width_ratios: [], shelf_height_ratios: [],
      base_cabinet_count: 4, base_cabinet_height_mm: 680, base_cabinet_depth_mm: 340,
    });
    expect(currentViewer().semanticSpec.stock_count).toBe(6);
    expect(resolveDesign(currentViewer().semanticSpec).rule_evaluations.filter((rule) => rule.status === "BLOCK")).toEqual([]);
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(readWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID)?.spec.stock_count).toBe(6);
    expect(currentViewer().semanticSpec.wall_anchor_verified).toBe(false);
  });

  it("rejects oversize typed dimensions without persisting them or adding a history step", async () => {
    await renderDraft();
    const before = currentViewer().semanticSpec;
    const width = screen.getByRole("spinbutton", { name: "Bredd" });
    fireEvent.change(width, { target: { value: "5000" } });
    fireEvent.keyDown(width, { key: "Enter" });
    expect(width).toHaveValue(before.width_mm);
    expect(width).toHaveAttribute("aria-invalid", "true");
    expect(currentViewer().semanticSpec).toEqual(before);
    expect(screen.getByRole("button", { name: "Ångra" })).toBeDisabled();
    fireEvent.change(width, { target: { value: "100000" } });
    fireEvent.keyDown(width, { key: "Enter" });
    expect(currentViewer().semanticSpec).toEqual(before);
    expect(width).toHaveAttribute("aria-invalid", "true");
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(readWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID)?.spec).toEqual(before);
    fireEvent.change(width, { target: { value: "1300" } });
    fireEvent.keyDown(width, { key: "Enter" });
    expect(currentViewer().semanticSpec.width_mm).toBe(1300);
    expect(width).not.toHaveAttribute("aria-invalid", "true");
    expect(screen.queryByText("Ändringen stoppades")).not.toBeInTheDocument();
  });

  it("guards canvas resizing while keeping one exact undo/redo transaction for valid frames", async () => {
    await renderDraft({ width_mm: 600, divider_count: 0, bay_width_ratios: [], shelf_height_ratios: [] });
    const before = currentViewer().semanticSpec;
    act(() => currentViewer().onResizeStart());
    act(() => currentViewer().onResize({ width_mm: 2200 }));
    const resized = currentViewer().semanticSpec;
    expect(resized.width_mm).toBe(2200);
    expect(resized.divider_count).toBeGreaterThan(0);
    expect(resolveDesign(resized).rule_evaluations.filter((rule) => rule.status === "BLOCK")).toEqual([]);
    act(() => currentViewer().onResize({ width_mm: 5000 }));
    expect(currentViewer().semanticSpec).toEqual(resized);
    act(() => currentViewer().onResizeEnd());
    fireEvent.keyDown(document.body, { key: "z", ctrlKey: true });
    expect(currentViewer().semanticSpec).toEqual(before);
    expect(screen.getByRole("button", { name: "Ångra" })).toBeDisabled();
    fireEvent.keyDown(document.body, { key: "y", ctrlKey: true });
    expect(currentViewer().semanticSpec).toEqual(resized);
  });

  it("keeps necessary support and bracing disabled and rejects an unsafe divider drag", async () => {
    await renderDraft({ divider_count: 1, bay_width_ratios: [], shelf_height_ratios: [], shelf_count: 5 });
    const before = currentViewer().semanticSpec;
    expect(screen.getByRole("button", { name: "Ta bort avdelare" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ta bort bakstycke" })).toBeDisabled();
    act(() => currentViewer().onPartHorizontalMoveStart());
    act(() => currentViewer().onPartHorizontalMove("divider-1", 100));
    act(() => currentViewer().onPartHorizontalMoveEnd());
    expect(currentViewer().semanticSpec).toEqual(before);
    expect(screen.getByRole("button", { name: "Ångra" })).toBeDisabled();
  });

  it("rebuilds divider and shelf geometry, with exact undo and redo", async () => {
    await renderDraft();
    const originalSpec = currentViewer().semanticSpec;
    const originalParts = currentViewer().parts;
    const originalPositions = dividerPositions();

    fireEvent.click(screen.getByRole("button", { name: "Ta bort avdelare" }));

    const removedSpec = currentViewer().semanticSpec;
    const removedParts = currentViewer().parts;
    expect(removedSpec.divider_count).toBe(2);
    expect(dividerPositions()).toHaveLength(2);
    dividerPositions().forEach((position, index) => {
      expect(position).toBeCloseTo(originalPositions[index]!, 2);
    });
    expect(removedParts.filter((part) => part.kind === "shelf")).toHaveLength(6);
    expect(removedSpec.removed_part_ids).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Ångra" }));
    expect(currentViewer().semanticSpec).toEqual(originalSpec);
    expect(currentViewer().parts).toEqual(originalParts);

    fireEvent.click(screen.getByRole("button", { name: "Gör om" }));
    expect(currentViewer().semanticSpec).toEqual(removedSpec);
    expect(currentViewer().parts).toEqual(removedParts);
  });

  it("removes the selected divider instead of the last one and clears selection", async () => {
    await renderDraft();
    const originalPositions = dividerPositions();
    fireEvent.click(screen.getByRole("button", { name: "Välj första avdelaren i modellen" }));
    expect(screen.getByTestId("furniture-viewer")).toHaveAttribute("data-selected-part-id", "divider-1");

    fireEvent.click(screen.getByRole("button", { name: "Ta bort avdelare" }));

    expect(currentViewer().semanticSpec.divider_count).toBe(2);
    expect(dividerPositions()).toHaveLength(2);
    dividerPositions().forEach((position, index) => {
      expect(position).toBeCloseTo(originalPositions[index + 1]!, 2);
    });
    expect(screen.getByTestId("furniture-viewer")).toHaveAttribute("data-selected-part-id", "");
  });

  it("disables removal at zero while allowing both kinds to be added again", async () => {
    await renderDraft({ divider_count: 0, bay_width_ratios: [], shelf_count: 0, shelf_height_ratios: [] });
    for (const [removeName, addName, field] of [
      ["Ta bort avdelare", /Avdelare/, "divider_count"],
      ["Ta bort hyllplan", /Hyllplan/, "shelf_count"],
    ] as const) {
      expect(screen.getByRole("button", { name: removeName })).toBeDisabled();
      fireEvent.click(screen.getByRole("button", { name: addName }));
      expect(currentViewer().semanticSpec[field]).toBe(1);
      expect(screen.getByRole("button", { name: removeName })).toBeEnabled();
      fireEvent.click(screen.getByRole("button", { name: removeName }));
      expect(currentViewer().semanticSpec[field]).toBe(0);
      expect(screen.getByRole("button", { name: removeName })).toBeDisabled();
    }
  });

  it("persists a removed shelf row and restores the same geometry after reopening", async () => {
    const workspace = await renderDraft();
    fireEvent.click(screen.getByRole("button", { name: "Ta bort hyllplan" }));
    const removedSpec = currentViewer().semanticSpec;
    const removedParts = currentViewer().parts;
    expect(removedSpec.shelf_count).toBe(1);
    expect(removedParts.filter((part) => part.kind === "shelf")).toHaveLength(4);

    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(readWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID)?.spec)
      .toEqual(removedSpec);

    workspace.unmount();
    render(<CustombuildWorkspace />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(currentViewer().semanticSpec).toEqual(removedSpec);
    expect(currentViewer().parts).toEqual(removedParts);
  });

  it("keeps the base-row family and saved template in sync through both history controls", async () => {
    await renderDraft({
      furniture_type: "wall_library",
      base_cabinet_count: 2,
      base_cabinet_height_mm: 650,
      base_cabinet_depth_mm: DEFAULT_DESIGN_SPEC.depth_mm,
    });
    const originalSpec = currentViewer().semanticSpec;
    const originalParts = currentViewer().parts;
    const baseParts = () => currentViewer().parts.filter((part) => (
      part.kind.startsWith("base_") || part.kind === "cabinet_front"
    ));
    expect(baseParts().length).toBeGreaterThan(0);

    const expectSavedFamily = async (family: DesignSpec["furniture_type"], templateId: string) => {
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
      const saved = readWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID);
      expect(saved?.spec.furniture_type).toBe(family);
      expect(saved?.templateId).toBe(templateId);
      expect(saved?.spec).toEqual(currentViewer().semanticSpec);
    };

    fireEvent.click(screen.getByRole("button", { name: "Ta bort underskåpsrad" }));
    const removedSpec = currentViewer().semanticSpec;
    expect(removedSpec).toMatchObject({
      furniture_type: "bookcase",
      base_cabinet_count: 0,
      base_cabinet_height_mm: 0,
      base_cabinet_depth_mm: 0,
    });
    expect(baseParts()).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Ta bort underskåpsrad" })).toBeDisabled();
    await expectSavedFamily("bookcase", "shelving");

    fireEvent.keyDown(document.body, { key: "z", ctrlKey: true });
    expect(currentViewer().semanticSpec).toEqual(originalSpec);
    expect(currentViewer().parts).toEqual(originalParts);
    await expectSavedFamily("wall_library", "wall-library");

    fireEvent.keyDown(document.body, { key: "y", ctrlKey: true });
    expect(currentViewer().semanticSpec).toEqual(removedSpec);
    await expectSavedFamily("bookcase", "shelving");

    fireEvent.click(screen.getByRole("button", { name: "Ångra" }));
    expect(currentViewer().parts).toEqual(originalParts);
    await expectSavedFamily("wall_library", "wall-library");

    fireEvent.click(screen.getByRole("button", { name: "Gör om" }));
    expect(currentViewer().semanticSpec).toEqual(removedSpec);
    await expectSavedFamily("bookcase", "shelving");
  });
});
