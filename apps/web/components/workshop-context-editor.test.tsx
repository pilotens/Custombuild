import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DEFAULT_DESIGN_SPEC, MACHINES } from "@/lib/design-types";
import {
  productionContextFromDesignSpec,
  type WorkshopProductionContext,
} from "@/lib/workshop-production-context";
import {
  WorkshopContextEditor,
  type WorkshopContextDraftState,
} from "./workshop-context-editor";

function fillProfiles(): void {
  const supplierIds = screen.getAllByRole("textbox", {
    name: "Leverantörens profil-ID (deklarerat)",
  });
  const versions = screen.getAllByRole("textbox", { name: "Profilversion eller batch" });
  const trims = screen.getAllByRole("textbox", { name: "Trimkant (mm)" });
  const kerfs = screen.getAllByRole("textbox", { name: "Kerf/verktygsspalt (mm)" });
  const grains = screen.getAllByRole("combobox", { name: "Fiberriktning i råskivan" });
  const rotations = screen.getAllByRole("combobox", { name: "Tillåt 90° rotation vid nesting" });
  for (const [index, supplierId] of supplierIds.entries()) {
    fireEvent.change(supplierId, { target: { value: `supplier-sheet-${index + 1}` } });
    fireEvent.change(versions[index]!, { target: { value: "batch-2026.09" } });
    fireEvent.change(trims[index]!, { target: { value: "10" } });
    fireEvent.change(kerfs[index]!, { target: { value: "6" } });
    fireEvent.change(grains[index]!, { target: { value: "X" } });
    fireEvent.change(rotations[index]!, { target: { value: "false" } });
  }
}

function boundContext(): WorkshopProductionContext {
  return {
    stock_profiles: [
      {
        role: "carcass",
        declaration_authority: "CLIENT_DECLARED",
        supplier_profile_id: "supplier-sheet-1",
        supplier_profile_version: "batch-2026.09",
        material_id: "birch-plywood",
        material_version: "screening-2026.1",
        sheet_width_um: 2_440_000,
        sheet_height_um: 1_220_000,
        thickness_um: 17_800,
        sheet_count: 4,
        trim_margin_um: 10_000,
        kerf_um: 6_000,
        grain_direction: "X",
        allow_rotation: false,
        defect_zones: [],
        fixture_keep_out_zones: [],
      },
      {
        role: "back",
        declaration_authority: "CLIENT_DECLARED",
        supplier_profile_id: "supplier-sheet-2",
        supplier_profile_version: "batch-2026.09",
        material_id: "birch-plywood-6",
        material_version: "screening-2026.1",
        sheet_width_um: 2_440_000,
        sheet_height_um: 1_220_000,
        thickness_um: 6_000,
        sheet_count: 2,
        trim_margin_um: 10_000,
        kerf_um: 6_000,
        grain_direction: "X",
        allow_rotation: false,
        defect_zones: [],
        fixture_keep_out_zones: [],
      },
    ],
  };
}

describe("WorkshopContextEditor", () => {
  it("offers both catalogued validation profiles with exact capacity and emits the selected ID", () => {
    const onChange = vi.fn();
    render(<WorkshopContextEditor spec={DEFAULT_DESIGN_SPEC} onChange={onChange} />);

    for (const machine of MACHINES) {
      const option = screen.getByRole("radio", {
        name: (accessibleName) => accessibleName.includes(machine.name),
      });
      expect(option).toBeEnabled();
      expect(screen.getByText(new RegExp(
        `X ${machine.workAreaMm.x} × Y ${machine.workAreaMm.y} × Z ${machine.workAreaMm.z} mm`,
      ))).toBeVisible();
    }
    expect(screen.getByText(/används endast för validering/i)).toBeVisible();
    expect(screen.getByRole("radio", {
      name: (accessibleName) => accessibleName.includes(MACHINES[0]!.name),
    })).toBeChecked();

    fireEvent.click(screen.getByRole("radio", {
      name: (accessibleName) => accessibleName.includes(MACHINES[1]!.name),
    }));
    expect(onChange).toHaveBeenCalledWith(undefined, {
      machine_profile_id: MACHINES[1]!.id,
    });
  });

  it("starts stockless and emits only complete client-declared profiles", () => {
    const onChange = vi.fn();
    const onValidityChange = vi.fn();
    render(
      <WorkshopContextEditor
        spec={DEFAULT_DESIGN_SPEC}
        onChange={onChange}
        onValidityChange={onValidityChange}
      />,
    );

    expect(screen.getByText("Lagerobundet granskningspaket")).toBeVisible();
    expect(screen.queryByRole("textbox", { name: /Leverantörens profil-ID/ })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", {
      name: "Bind leverantörsdeklarerad verkstadsprofil",
    }));
    fillProfiles();

    expect(onValidityChange).toHaveBeenLastCalledWith(true);
    expect(onChange).toHaveBeenLastCalledWith(boundContext(), {
      stock_width_mm: 2_440,
      stock_height_mm: 1_220,
      stock_count: 4,
      back_stock_width_mm: 2_440,
      back_stock_height_mm: 1_220,
      back_stock_count: 2,
    });
  });

  it("keeps exact micron input and rejects a fourth decimal without overwriting valid state", () => {
    const onChange = vi.fn();
    const onValidityChange = vi.fn();
    render(
      <WorkshopContextEditor
        spec={DEFAULT_DESIGN_SPEC}
        onChange={onChange}
        onValidityChange={onValidityChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", {
      name: "Bind leverantörsdeklarerad verkstadsprofil",
    }));
    fillProfiles();

    const widths = screen.getAllByRole("textbox", { name: "Skivbredd (mm)" });
    fireEvent.change(widths[0]!, { target: { value: "256.001" } });
    expect(onChange.mock.calls.at(-1)?.[0].stock_profiles[0].sheet_width_um).toBe(256_001);
    const validCallCount = onChange.mock.calls.length;

    fireEvent.change(widths[0]!, { target: { value: "256.0011" } });
    expect(onValidityChange).toHaveBeenLastCalledWith(false);
    expect(onChange).toHaveBeenCalledTimes(validCallCount);
    expect(screen.getByRole("alert")).toHaveTextContent(/högst tre decimaler/i);
  });

  it("captures a versioned registration for one physical sheet", () => {
    const onChange = vi.fn();
    render(<WorkshopContextEditor spec={DEFAULT_DESIGN_SPEC} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", {
      name: "Bind leverantörsdeklarerad verkstadsprofil",
    }));
    fillProfiles();
    fireEvent.click(screen.getByRole("button", { name: "Lägg till tvåsidig skiva" }));

    fireEvent.change(screen.getByRole("combobox", { name: "Råmaterialroll" }), {
      target: { value: "carcass" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Fysiskt skivnummer" }), {
      target: { value: "1" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Fixtur-/registreringsmetod-ID" }), {
      target: { value: "shop-pin-fixture" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Fixturmetodens version" }), {
      target: { value: "v1.2" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Registreringspinnens diameter (mm)" }), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Positionstolerans (mm)" }), {
      target: { value: "1" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Pinne 1, X (mm)" }), {
      target: { value: "80" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Pinne 1, Y (mm)" }), {
      target: { value: "30" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Pinne 2, X (mm)" }), {
      target: { value: "2360" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Pinne 2, Y (mm)" }), {
      target: { value: "30" },
    });

    expect(onChange.mock.calls.at(-1)?.[0].two_sided_registrations).toEqual([{
      stock_role: "carcass",
      sheet_index: 0,
      declaration_authority: "CLIENT_DECLARED",
      flip_axis: "X",
      fixture_method_id: "shop-pin-fixture",
      fixture_method_version: "v1.2",
      pin_diameter_um: 10_000,
      position_tolerance_um: 1_000,
      pins: [{ x_um: 80_000, y_um: 30_000 }, { x_um: 2_360_000, y_um: 30_000 }],
    }]);
  });

  it("preserves a new partial binding and explicit dirty/invalid state across unmount", () => {
    let retainedDraft: WorkshopContextDraftState | undefined;
    const props = {
      spec: DEFAULT_DESIGN_SPEC,
      onChange: vi.fn(),
      onValidityChange: vi.fn(),
      onDraftStateChange: (state: WorkshopContextDraftState) => { retainedDraft = state; },
    };
    const mounted = render(<WorkshopContextEditor {...props} />);
    fireEvent.click(screen.getByRole("button", {
      name: "Bind leverantörsdeklarerad verkstadsprofil",
    }));
    const input = screen.getAllByRole("textbox", {
      name: "Leverantörens profil-ID (deklarerat)",
    })[0]!;
    fireEvent.change(input, { target: { value: "partial-profile" } });

    expect(retainedDraft).toMatchObject({ dirty: true, valid: false, enabled: true });
    expect(props.onChange).not.toHaveBeenCalled();
    mounted.unmount();

    render(<WorkshopContextEditor {...props} draftState={retainedDraft} />);
    expect(screen.getAllByRole("textbox", {
      name: "Leverantörens profil-ID (deklarerat)",
    })[0]).toHaveValue("partial-profile");
    expect(screen.getByRole("alert")).toBeVisible();
    expect(props.onChange).not.toHaveBeenCalled();
  });

  it("does not restore the last valid supplier ID after an invalid deletion and remount", () => {
    const context = boundContext();
    const spec = { ...DEFAULT_DESIGN_SPEC, workshop_context: context };
    const onChange = vi.fn();
    let retainedDraft: WorkshopContextDraftState | undefined;
    const onDraftStateChange = (state: WorkshopContextDraftState) => { retainedDraft = state; };
    const mounted = render(
      <WorkshopContextEditor
        spec={spec}
        value={context}
        onChange={onChange}
        onDraftStateChange={onDraftStateChange}
      />,
    );

    fireEvent.change(screen.getAllByRole("textbox", {
      name: "Leverantörens profil-ID (deklarerat)",
    })[0]!, { target: { value: "" } });

    expect(retainedDraft).toMatchObject({ dirty: true, valid: false, enabled: true });
    expect(onChange).not.toHaveBeenCalled();
    mounted.unmount();

    render(
      <WorkshopContextEditor
        spec={spec}
        value={context}
        draftState={retainedDraft}
        onChange={onChange}
        onDraftStateChange={onDraftStateChange}
      />,
    );
    expect(screen.getAllByRole("textbox", {
      name: "Leverantörens profil-ID (deklarerat)",
    })[0]).toHaveValue("");
    expect(screen.getByRole("alert")).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("rebinds an existing back-stock profile when measured back thickness changes", async () => {
    const onChange = vi.fn();
    const onValidityChange = vi.fn();
    const context = boundContext();
    const { rerender } = render(
      <WorkshopContextEditor
        spec={{ ...DEFAULT_DESIGN_SPEC, workshop_context: context }}
        value={context}
        onChange={onChange}
        onValidityChange={onValidityChange}
      />,
    );
    onChange.mockClear();
    onValidityChange.mockClear();

    rerender(
      <WorkshopContextEditor
        spec={{
          ...DEFAULT_DESIGN_SPEC,
          measured_back_thickness_mm: 5.8,
          workshop_context: context,
        }}
        value={context}
        onChange={onChange}
        onValidityChange={onValidityChange}
      />,
    );

    await waitFor(() => expect(onChange).toHaveBeenCalledOnce());
    expect(onChange.mock.calls[0]?.[0].stock_profiles[1].thickness_um).toBe(5_800);
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
    expect(screen.getByText(/5.8 mm/)).toBeVisible();
  });

  it("returns to stockless UI when a binding edit clears the external context", async () => {
    const initialSpec = { ...DEFAULT_DESIGN_SPEC, workshop_context: boundContext() };
    const { rerender } = render(
      <WorkshopContextEditor
        spec={initialSpec}
        value={initialSpec.workshop_context}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getAllByRole("textbox", {
      name: "Leverantörens profil-ID (deklarerat)",
    })).toHaveLength(2);

    rerender(
      <WorkshopContextEditor
        spec={{ ...DEFAULT_DESIGN_SPEC, measured_thickness_mm: 18 }}
        value={undefined}
        onChange={vi.fn()}
      />,
    );
    expect(await screen.findByText("Lagerobundet granskningspaket")).toBeVisible();
    expect(screen.queryByRole("textbox", { name: /Leverantörens profil-ID/ }))
      .not.toBeInTheDocument();
  });

  it("labels the frozen snapshot as unverified client/shop truth", () => {
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      machine_profile_id: MACHINES[1]!.id,
      workshop_context: boundContext(),
    };
    render(
      <WorkshopContextEditor
        spec={spec}
        value={spec.workshop_context}
        frozenContext={productionContextFromDesignSpec(spec)}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByRole("region", { name: "Fryst verkstadskontext" }))
      .toHaveTextContent("Kund-/verkstadsdeklarerad (inte verifierad)");
    expect(screen.getByRole("region", { name: "Fryst verkstadskontext" }))
      .toHaveTextContent(MACHINES[1]!.id);
    expect(screen.getByRole("region", { name: "Fryst verkstadskontext" }))
      .toHaveTextContent("X 5100 × Y 2600 × Z 150 mm");
    expect(screen.getByText(/validation-only/)).toBeVisible();
  });
});
