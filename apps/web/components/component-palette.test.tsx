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
        onRemove={vi.fn()}
        onDragStartKind={vi.fn()}
        onDragEnd={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Hantera delar" })).toBeVisible();
    expect(screen.getAllByRole("group")).toHaveLength(SEMANTIC_COMPONENTS.length);
    expect(screen.getAllByRole("button")).toHaveLength(SEMANTIC_COMPONENTS.length * 2);
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
        onRemove={vi.fn()}
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
        onRemove={vi.fn()}
        onDragStartKind={vi.fn()}
        onDragEnd={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: /Bakstycke/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Sockel/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ta bort bakstycke" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Ta bort sockel" })).toBeEnabled();
  });

  it("removes every component through its own control without inserting it", () => {
    const onInsert = vi.fn();
    const onRemove = vi.fn();
    const { container } = render(
      <ComponentPalette
        spec={{ ...DEFAULT_DESIGN_SPEC, shelf_count: 2, divider_count: 2, base_cabinet_count: 3 }}
        onInsert={onInsert}
        onRemove={onRemove}
        onDragStartKind={vi.fn()}
        onDragEnd={vi.fn()}
      />,
    );
    const labels = ["hyllplan", "avdelare", "underskåpsrad", "bakstycke", "sockel"];
    SEMANTIC_COMPONENTS.forEach((component, index) => {
      fireEvent.click(screen.getByRole("button", { name: `Ta bort ${labels[index]}` }));
      expect(onRemove).toHaveBeenNthCalledWith(index + 1, component.kind);
    });
    expect(onInsert).not.toHaveBeenCalled();
    expect(container.querySelector("button button")).toBeNull();
  });

  it("disables removal for absent components but leaves insertion available", () => {
    const onRemove = vi.fn();
    render(
      <ComponentPalette
        spec={{ ...DEFAULT_DESIGN_SPEC, shelf_count: 0, divider_count: 0, base_cabinet_count: 0, back_panel: false, plinth: false }}
        onInsert={vi.fn()}
        onRemove={onRemove}
        onDragStartKind={vi.fn()}
        onDragEnd={vi.fn()}
      />,
    );
    for (const button of screen.getAllByRole("button", { name: /^Ta bort / })) {
      expect(button).toBeDisabled();
      fireEvent.click(button);
    }
    expect(onRemove).not.toHaveBeenCalled();
    for (const component of SEMANTIC_COMPONENTS) {
      expect(screen.getByRole("button", { name: new RegExp(component.label) })).toBeEnabled();
    }
  });

  it("allows removal when the shelf and divider insertion limits are reached", () => {
    render(
      <ComponentPalette
        spec={{ ...DEFAULT_DESIGN_SPEC, shelf_count: 40, divider_count: 16 }}
        onInsert={vi.fn()}
        onRemove={vi.fn()}
        onDragStartKind={vi.fn()}
        onDragEnd={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /Hyllplan/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Avdelare/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ta bort hyllplan" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Ta bort avdelare" })).toBeEnabled();
  });
});
