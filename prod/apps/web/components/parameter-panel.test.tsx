import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DEFAULT_DESIGN_SPEC, MATERIALS } from "@/lib/design-types";
import { ParameterPanel } from "./parameter-panel";

describe("ParameterPanel", () => {
  it("emits millimetres through the shared DesignSpec contract", () => {
    const onChange = vi.fn();
    render(<ParameterPanel spec={DEFAULT_DESIGN_SPEC} mode="expert" onChange={onChange} />);

    fireEvent.change(screen.getByLabelText(/^Bredd/), { target: { value: "1450" } });
    expect(onChange).toHaveBeenCalledWith({ width_mm: 1450 }, "Bredd ändrades");
  });

  it("updates the material identity and both thickness values together", () => {
    const onChange = vi.fn();
    const target = MATERIALS[1]!;
    render(<ParameterPanel spec={DEFAULT_DESIGN_SPEC} mode="expert" onChange={onChange} />);

    fireEvent.change(screen.getByLabelText("Skivmaterial"), { target: { value: target.id } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        material_id: target.id,
        nominal_thickness_mm: target.nominalThicknessMm,
        measured_thickness_mm: target.measuredThicknessMm,
      }),
      "Materialversion ändrades",
    );
  });

  it("exposes DADO as the only production-supported MVP joint", () => {
    render(<ParameterPanel spec={DEFAULT_DESIGN_SPEC} mode="expert" onChange={vi.fn()} />);

    expect(DEFAULT_DESIGN_SPEC.joint_system).toBe("dado");
    expect(screen.getByText("Not/spår (enda produktionsstödda MVP-fog)")).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Fogsystem" })).not.toBeInTheDocument();
  });
});
