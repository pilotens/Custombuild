import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { resolveDesign } from "@/lib/design-engine";
import { DEFAULT_DESIGN_SPEC } from "@/lib/design-types";
import { SelectedPartInspector } from "./selected-part-inspector";

describe("SelectedPartInspector", () => {
  const part = resolveDesign(DEFAULT_DESIGN_SPEC).parts.find((candidate) => candidate.part_id === "side-left");
  if (!part) throw new Error("Expected side-left in the default design");

  it("edits only connected carcass dimensions instead of detached coordinates", () => {
    const onChange = vi.fn();
    render(
      <SelectedPartInspector
        part={part}
        spec={DEFAULT_DESIGN_SPEC}
        onChange={onChange}
        onRemove={vi.fn()}
        onReset={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("region", { name: `Redigera fysisk del ${part.name}` })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Vänster gavel" })).toBeVisible();
    expect(screen.getByText("Vald fysisk del")).toBeVisible();
    expect(screen.getByText("Deltyp: Sidostycke")).toBeVisible();
    expect(screen.getByText(`ID: ${part.part_id}`)).toBeVisible();
    expect(screen.getByText(/Fysisk del · .* mm/)).toBeVisible();
    const heightField = screen.getByLabelText("Konstruktionshöjd");
    const depthField = screen.getByLabelText("Konstruktionsdjup");
    expect(heightField).toHaveAttribute("min", "300");
    expect(heightField).toHaveAttribute("max", "4000");
    expect(depthField).toHaveAttribute("min", "100");
    expect(depthField).toHaveAttribute("max", "1200");
    fireEvent.change(heightField, { target: { value: "2050" } });
    expect(onChange).toHaveBeenCalledWith({ width_mm: 2050 });
    expect(screen.queryByLabelText("Från vänster (X)")).not.toBeInTheDocument();
  });

  it("maps top-panel fields to the complete canonical outer envelope", () => {
    const top = resolveDesign(DEFAULT_DESIGN_SPEC).parts.find((candidate) => candidate.part_id === "top");
    if (!top) throw new Error("Expected top");
    const onChange = vi.fn();
    render(
      <SelectedPartInspector
        part={top}
        spec={DEFAULT_DESIGN_SPEC}
        onChange={onChange}
        onRemove={vi.fn()}
        onReset={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    const widthField = screen.getByLabelText("Möbelbredd via delen");
    const depthField = screen.getByLabelText("Konstruktionsdjup");
    const positionField = screen.getByLabelText("Nivå från golv (Z)");
    expect(widthField).toHaveAttribute("min", String(250 - 2 * DEFAULT_DESIGN_SPEC.measured_thickness_mm));
    expect(widthField).toHaveAttribute("max", String(6_000 - 2 * DEFAULT_DESIGN_SPEC.measured_thickness_mm));
    expect(depthField).toHaveAttribute("min", "100");
    expect(depthField).toHaveAttribute("max", "1200");
    expect(positionField).toHaveAttribute("min", String(300 - top.thickness_mm / 2));
    expect(positionField).toHaveAttribute("max", String(4_000 - top.thickness_mm / 2));

    fireEvent.change(widthField, { target: { value: "10000" } });
    expect(onChange).toHaveBeenCalledWith({ width_mm: 6_000 - 2 * DEFAULT_DESIGN_SPEC.measured_thickness_mm });
  });

  it("offers reversible reset, removal and close actions", () => {
    const onRemove = vi.fn();
    const onReset = vi.fn();
    const onClose = vi.fn();
    const { rerender } = render(
      <SelectedPartInspector
        part={part}
        spec={DEFAULT_DESIGN_SPEC}
        onChange={vi.fn()}
        onRemove={onRemove}
        onReset={onReset}
        onClose={onClose}
      />,
    );

    expect(screen.getByRole("button", { name: "Återställ del" })).toBeDisabled();
    rerender(
      <SelectedPartInspector
        part={{ ...part, width_mm: 2050 }}
        spec={DEFAULT_DESIGN_SPEC}
        override={{ width_mm: 2050 }}
        onChange={vi.fn()}
        onRemove={onRemove}
        onReset={onReset}
        onClose={onClose}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Återställ del" }));
    fireEvent.click(screen.getByRole("button", { name: "Ta bort del" }));
    fireEvent.click(screen.getByRole("button", { name: `Avmarkera ${part.name}` }));
    expect(onReset).toHaveBeenCalledOnce();
    expect(onRemove).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("explains that removing a divider rebuilds the surrounding grid", () => {
    const dividerSpec = { ...DEFAULT_DESIGN_SPEC, divider_count: 1, reinforcement_mode: "manual" as const };
    const divider = resolveDesign(dividerSpec).parts.find((candidate) => candidate.part_id === "divider-1");
    if (!divider) throw new Error("Expected divider-1");
    render(
      <SelectedPartInspector
        part={divider}
        spec={dividerSpec}
        onChange={vi.fn()}
        onRemove={vi.fn()}
        onReset={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText(/angränsande facken slås ihop/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Ta bort och bygg om" })).toBeVisible();
  });

  it("shows exact clear distances around a selected shelf", () => {
    const shelf = resolveDesign(DEFAULT_DESIGN_SPEC).parts.find((candidate) => candidate.part_id === "shelf-2-bay-1");
    if (!shelf) throw new Error("Expected shelf-2-bay-1");
    const onShelfOpeningChange = vi.fn();
    render(
      <SelectedPartInspector
        part={shelf}
        spec={DEFAULT_DESIGN_SPEC}
        onChange={vi.fn()}
        onShelfOpeningChange={onShelfOpeningChange}
        onRemove={vi.fn()}
        onReset={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Fria avstånd runt hyllplanet")).toBeVisible();
    expect(screen.getByLabelText("Fritt under")).toHaveAttribute("min", "40");
    expect(screen.getByLabelText("Fritt över")).toHaveAttribute("min", "40");
    fireEvent.change(screen.getByLabelText("Fritt under"), { target: { value: "20" } });
    expect(onShelfOpeningChange).toHaveBeenCalledWith(1, 40);
    fireEvent.change(screen.getByLabelText("Fritt över"), { target: { value: "360" } });
    expect(onShelfOpeningChange).toHaveBeenCalledWith(2, 360);
  });

  it("falls back to the canonical 40 mm opening instead of a hidden lower value", () => {
    const shelf = resolveDesign(DEFAULT_DESIGN_SPEC).parts.find((candidate) => candidate.part_id === "shelf-2-bay-1");
    if (!shelf) throw new Error("Expected shelf-2-bay-1");
    render(
      <SelectedPartInspector
        part={{ ...shelf, part_id: "shelf-40-bay-1" }}
        spec={DEFAULT_DESIGN_SPEC}
        onChange={vi.fn()}
        onShelfOpeningChange={vi.fn()}
        onRemove={vi.fn()}
        onReset={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Fritt under")).toHaveValue(40);
    expect(screen.getByLabelText("Fritt över")).toHaveValue(40);
  });

  it("treats only generated internal base sides as topology-removal controls", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      divider_count: 2,
      base_cabinet_count: 3,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 520,
      reinforcement_mode: "manual" as const,
    };
    const parts = resolveDesign(spec).parts;
    const internalSide = parts.find((candidate) => candidate.part_id === "base-side-2");
    if (!internalSide) throw new Error("Expected the first internal base side");
    const onChange = vi.fn();

    render(
      <SelectedPartInspector
        part={internalSide}
        spec={spec}
        onChange={onChange}
        onRemove={vi.fn()}
        onReset={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(parts.some((candidate) => candidate.part_id === "base-side-1")).toBe(false);
    expect(parts.some((candidate) => candidate.part_id === "base-side-4")).toBe(false);
    expect(screen.getByText(/Angränsande skåpsmoduler slås ihop/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Ta bort och bygg om" })).toBeVisible();
    fireEvent.change(screen.getByLabelText("Konstruktionshöjd"), {
      target: { value: internalSide.width_mm + 20 },
    });
    expect(onChange).toHaveBeenCalledWith({ width_mm: internalSide.width_mm + 20 });
  });
});
