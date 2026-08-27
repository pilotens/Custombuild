import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DEFAULT_DESIGN_SPEC } from "@/lib/design-types";
import {
  SEMANTIC_COMPONENTS,
  SEMANTIC_COMPONENT_MIME,
} from "@/lib/semantic-design";
import { ComponentPalette } from "./component-palette";

describe("ComponentPalette", () => {
  it("shows only the five real semantic components as parametric controls", () => {
    render(
      <ComponentPalette
        spec={DEFAULT_DESIGN_SPEC}
        onInsert={vi.fn()}
        onDragStartKind={vi.fn()}
        onDragEnd={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Lägg till delar" })).toBeVisible();
    expect(screen.getAllByRole("button")).toHaveLength(SEMANTIC_COMPONENTS.length);
    for (const component of SEMANTIC_COMPONENTS) {
      expect(screen.getByRole("button", { name: new RegExp(component.label) })).toBeVisible();
    }
    expect(screen.getAllByText("Parametrisk")).toHaveLength(SEMANTIC_COMPONENTS.length);
  });

  it("keeps click insertion and semantic drag payloads connected", () => {
    const onInsert = vi.fn();
    const onDragStartKind = vi.fn();
    const onDragEnd = vi.fn();
    render(
      <ComponentPalette
        spec={DEFAULT_DESIGN_SPEC}
        onInsert={onInsert}
        onDragStartKind={onDragStartKind}
        onDragEnd={onDragEnd}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Hyllplan/ }));
    expect(onInsert).toHaveBeenCalledWith("shelf_row");

    const dataTransfer = {
      effectAllowed: "none",
      setData: vi.fn(),
      getData: vi.fn(),
    } as unknown as DataTransfer;
    const divider = screen.getByRole("button", { name: /Avdelare/ });
    fireEvent.dragStart(divider, { dataTransfer });
    expect(onDragStartKind).toHaveBeenCalledWith("divider");
    expect(dataTransfer.setData).toHaveBeenCalledWith(SEMANTIC_COMPONENT_MIME, "divider");
    expect(dataTransfer.setData).toHaveBeenCalledWith("text/plain", "divider");
    expect(dataTransfer.effectAllowed).toBe("copy");

    fireEvent.dragEnd(divider);
    expect(onDragEnd).toHaveBeenCalledOnce();
  });

  it("marks components already present in the model as unavailable", () => {
    render(
      <ComponentPalette
        spec={DEFAULT_DESIGN_SPEC}
        onInsert={vi.fn()}
        onDragStartKind={vi.fn()}
        onDragEnd={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: /Bakstycke/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Sockel/ })).toBeDisabled();
  });
});
