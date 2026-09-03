import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DEFAULT_DESIGN_SPEC } from "@/lib/design-types";
import { StudioInspector } from "./studio-inspector";

describe("StudioInspector", () => {
  it("shows one contextual furniture panel instead of an internal wizard", () => {
    render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="PASS" partCount={18} onChange={vi.fn()} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Möbel" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Yttermått" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Fack och hyllor" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: /konfigurationssteg/i })).not.toBeInTheDocument();
  });

  it("uses the canonical B1 envelope for furniture dimensions and shelf count", () => {
    render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="PASS" partCount={18} onChange={vi.fn()} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    expect(screen.getByLabelText("Bredd")).toHaveAttribute("min", "250");
    expect(screen.getByLabelText("Bredd")).toHaveAttribute("max", "6000");
    expect(screen.getByLabelText("Höjd")).toHaveAttribute("min", "300");
    expect(screen.getByLabelText("Höjd")).toHaveAttribute("max", "4000");
    expect(screen.getByLabelText("Djup")).toHaveAttribute("min", "100");
    expect(screen.getByLabelText("Djup")).toHaveAttribute("max", "1200");
    expect(screen.getByLabelText("Bredd")).toHaveAttribute("step", "0.001");
    expect(screen.getByText(/samma servermodell som skapar granskningsunderlaget/i)).toBeVisible();
  });

  it("preserves an exact millimetre value instead of snapping it to ten millimetres", () => {
    const onChange = vi.fn();
    render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="PASS" partCount={18} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    const depth = screen.getByLabelText("Djup");
    fireEvent.change(depth, { target: { value: "397.125" } });
    fireEvent.blur(depth);

    expect(onChange).toHaveBeenCalledWith(
      { depth_mm: 397.125 },
      "Djup ändrades",
    );
  });

  it("captures the measured material batch thickness at exact micrometre resolution", () => {
    const onChange = vi.fn();
    render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="PASS" partCount={18} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    const thickness = screen.getByLabelText("Faktisk uppmätt skivtjocklek");
    expect(thickness).toHaveAttribute("min", "17");
    expect(thickness).toHaveAttribute("max", "19");
    expect(thickness).toHaveAttribute("step", "0.001");

    fireEvent.change(thickness, { target: { value: "17.6" } });
    fireEvent.blur(thickness);
    expect(onChange).toHaveBeenCalledWith(
      { measured_thickness_mm: 17.6 },
      "Faktisk uppmätt skivtjocklek ändrades",
    );

    onChange.mockClear();
    fireEvent.change(thickness, { target: { value: "17.6005" } });
    fireEvent.blur(thickness);
    expect(thickness).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/steg om 0,001 mm/i)).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.change(thickness, { target: { value: "16.999" } });
    fireEvent.blur(thickness);
    expect(screen.getByText(/mellan 17 och 19 mm/i)).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();
  });

  it.each([0, 1, 1.2])("accepts exact %.3f mm front edge-band intent", (edgeBandMm) => {
    const onChange = vi.fn();
    render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="PASS" partCount={18} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    const edgeBand = screen.getByLabelText("Framkantlistens tjocklek");
    expect(edgeBand).toHaveAttribute("min", "0");
    expect(edgeBand).toHaveAttribute("max", "5");
    expect(edgeBand).toHaveAttribute("step", "0.001");
    fireEvent.change(edgeBand, { target: { value: String(edgeBandMm) } });
    fireEvent.blur(edgeBand);

    expect(onChange).toHaveBeenCalledWith(
      { edge_band_mm: edgeBandMm },
      expect.stringMatching(edgeBandMm === 0 ? /togs bort/i : /ändrades/i),
    );
    expect(screen.getByText(/separat SKU, infästningsmetod och kapmåttskompensation/i))
      .toBeVisible();
  });

  it("rejects lossy and out-of-range front edge-band input", () => {
    const onChange = vi.fn();
    render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="PASS" partCount={18} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);
    const edgeBand = screen.getByLabelText("Framkantlistens tjocklek");

    fireEvent.change(edgeBand, { target: { value: "1.0005" } });
    fireEvent.blur(edgeBand);
    expect(edgeBand).toHaveAttribute("aria-invalid", "true");
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.change(edgeBand, { target: { value: "5.001" } });
    fireEvent.blur(edgeBand);
    expect(screen.getByText(/mellan 0 och 5 mm/i)).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("shows concept scope before the user reaches package generation", () => {
    render(<StudioInspector
      spec={{
        ...DEFAULT_DESIGN_SPEC,
        furniture_type: "wall_library",
        base_cabinet_height_mm: 680,
        base_cabinet_depth_mm: DEFAULT_DESIGN_SPEC.depth_mm,
        base_cabinet_count: 1,
      }}
      status="WARNING"
      partCount={22}
      templateName="Väggbibliotek"
      productionLevel="concept"
      onChange={vi.fn()}
      onOpenExplore={vi.fn()}
      onOpenCheck={vi.fn()}
    />);

    expect(screen.getByText("Konceptdesign – underlag spärrat")).toBeVisible();
    expect(screen.getByText(/kan inte bli ett produktionsunderlag/i)).toBeVisible();
    expect(screen.getByText(/inte ett godkännande för skärande CNC/i)).toBeVisible();
  });

  it("maximizes equal bays from a requested clear width", () => {
    const onChange = vi.fn();
    render(<StudioInspector spec={{ ...DEFAULT_DESIGN_SPEC, width_mm: 4_200, measured_thickness_mm: 17.8 }} status="WARNING" partCount={42} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Minsta bredd"));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
      bay_sizing_mode: "target_width",
      target_bay_width_mm: 300,
      divider_count: 12,
      bay_width_ratios: [],
    }), expect.any(String));
  });

  it("exposes the authoritative load and reinforcement inputs through onChange", () => {
    const onChange = vi.fn();
    render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="PASS" partCount={18} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    const loadInput = screen.getByLabelText("Total planerad last per hyllrad");
    fireEvent.change(loadInput, { target: { value: "48" } });
    fireEvent.blur(loadInput);
    expect(onChange).toHaveBeenCalledWith({ load_per_shelf_kg: 48 }, expect.stringContaining("Total planerad last per hyllrad"));
    expect(screen.getByText(/summan för alla fack på samma hyllnivå/i)).toBeVisible();

    fireEvent.click(screen.getByLabelText(/Manuellt/));
    expect(onChange).toHaveBeenCalledWith({ reinforcement_mode: "manual" }, expect.stringContaining("Manuella"));
  });

  it("edits the exact sockel height and carries wall-anchor intent without claiming verification", () => {
    const onChange = vi.fn();
    const { rerender } = render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="PASS" partCount={18} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    const plinthHeight = screen.getByLabelText("Sockelhöjd");
    expect(plinthHeight).toHaveAttribute("step", "0.001");
    fireEvent.change(plinthHeight, { target: { value: "125.5" } });
    fireEvent.blur(plinthHeight);
    expect(onChange).toHaveBeenCalledWith(
      { plinth_height_mm: 125.5 },
      "Sockelhöjden ändrades",
    );

    fireEvent.click(screen.getByRole("checkbox", { name: /Planera väggförankring/ }));
    expect(onChange).toHaveBeenCalledWith(
      { wall_anchor_required: true },
      "Planera väggförankring ändrades",
    );

    rerender(<StudioInspector spec={{ ...DEFAULT_DESIGN_SPEC, wall_anchor_required: true }} status="PASS" partCount={18} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);
    expect(screen.getByText(/inte ett verifierat montagebevis/i)).toBeVisible();
  });

  it("keeps unsupported surface mounting unavailable as a new production choice", () => {
    const onChange = vi.fn();
    render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="PASS" partCount={18} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    expect(screen.getByRole("radio", { name: /Infällt i not/ })).toBeChecked();
    expect(screen.getByLabelText("Välj Björkplywood 6 mm för bakstycket")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/verkstadsunderlaget förblir spärrat/i)).toBeVisible();

    const surfaceMounted = screen.getByRole("radio", { name: /Utanpåliggande/ });
    expect(surfaceMounted).toBeDisabled();
    expect(screen.getByText(/saknar en implementerad autentiserad retentionklass/i)).toBeVisible();
    fireEvent.click(surfaceMounted);
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByLabelText("Välj MDF 6 mm för bakstycket"));
    expect(onChange).toHaveBeenCalledWith(
      { back_material_id: "mdf-6", measured_back_thickness_mm: 6 },
      expect.stringContaining("MDF 6 mm"),
    );
  });

  it("captures exact measured back thickness without rounding", () => {
    const onChange = vi.fn();
    render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="PASS" partCount={18} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    const thickness = screen.getByLabelText("Faktisk uppmätt bakstyckestjocklek");
    expect(thickness).toHaveAttribute("min", "5.5");
    expect(thickness).toHaveAttribute("max", "6.5");
    expect(thickness).toHaveAttribute("step", "0.001");

    fireEvent.change(thickness, { target: { value: "5.8" } });
    fireEvent.blur(thickness);
    expect(onChange).toHaveBeenCalledWith(
      { measured_back_thickness_mm: 5.8 },
      "Bakstyckets uppmätta tjocklek ändrades",
    );

    onChange.mockClear();
    fireEvent.change(thickness, { target: { value: "5.8005" } });
    fireEvent.blur(thickness);
    expect(thickness).toHaveAttribute("aria-invalid", "true");
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.change(thickness, { target: { value: "5.499" } });
    fireEvent.blur(thickness);
    expect(screen.getByText(/mellan 5,5 och 6,5 mm/i)).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("lets a hydrated surface-mounted design move to the supported inset back", () => {
    const onChange = vi.fn();
    render(<StudioInspector
      spec={{ ...DEFAULT_DESIGN_SPEC, back_panel_type: "surface_mounted" }}
      status="BLOCK"
      partCount={18}
      onChange={onChange}
      onOpenExplore={vi.fn()}
      onOpenCheck={vi.fn()}
    />);

    expect(screen.getByRole("radio", { name: /Utanpåliggande/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Utanpåliggande/ })).toBeDisabled();
    expect(screen.getByText(/Den inlästa designen använder ett äldre utanpåliggande bakstycke/i))
      .toBeVisible();
    const inset = screen.getByRole("radio", { name: /Infällt i not/ });
    expect(inset).toBeEnabled();
    fireEvent.click(inset);
    expect(onChange).toHaveBeenCalledWith(
      { back_panel_type: "inset_groove" },
      "Infällt bakstycke valdes",
    );
  });

  it("keeps dormant back choices hidden and preserves them when the back is re-enabled", () => {
    const onChange = vi.fn();
    render(<StudioInspector spec={{
      ...DEFAULT_DESIGN_SPEC,
      back_panel: false,
      back_panel_type: "surface_mounted",
      back_material_id: "mdf-6",
    }} status="PASS" partCount={17} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    expect(screen.queryByRole("radio", { name: /Utanpåliggande/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Välj MDF 6 mm för bakstycket")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /Bakstycke/ }));
    expect(onChange).toHaveBeenCalledWith(
      { back_panel: true },
      expect.stringContaining("Bakstycke"),
    );
  });

  it("shows real wall-library base dimensions and only edits module count in manual mode", () => {
    const onChange = vi.fn();
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library" as const,
      width_mm: 4_200,
      height_mm: 2_400,
      depth_mm: 350,
      divider_count: 4,
      base_cabinet_height_mm: 680,
      base_cabinet_depth_mm: 350,
      base_cabinet_count: 5,
      reinforcement_mode: "manual" as const,
    };
    render(<StudioInspector spec={spec} status="PASS" partCount={42} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Underskåp" })).toBeInTheDocument();
    expect(screen.getByText("350 mm")).toBeInTheDocument();
    const heightInput = screen.getByLabelText("Höjd underskåp");
    expect(heightInput).toHaveAttribute("min", "300");
    expect(heightInput).toHaveAttribute("max", "2000");
    fireEvent.change(heightInput, { target: { value: "720" } });
    fireEvent.blur(heightInput);
    expect(onChange).toHaveBeenCalledWith({ base_cabinet_height_mm: 720 }, expect.stringContaining("Underskåpshöjden"));

    fireEvent.click(screen.getByRole("button", { name: "Öka skåpsmoduler" }));
    expect(onChange).toHaveBeenCalledWith({ base_cabinet_count: 6 }, expect.stringContaining("skåpsmoduler"));
  });

  it("keeps individual bay widths and shelf levels in the existing DesignSpec ratios", () => {
    const onChange = vi.fn();
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      divider_count: 2,
      shelf_count: 2,
      symmetry_locked: false,
      reinforcement_mode: "manual" as const,
    };
    render(<StudioInspector spec={spec} status="PASS" partCount={24} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    fireEvent.click(screen.getByText("Detaljerad indelning"));
    fireEvent.change(screen.getByLabelText("Bredd för fack 1"), { target: { value: "20" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
      bay_sizing_mode: "count",
      reinforcement_mode: "manual",
      bay_width_ratios: expect.arrayContaining([0.2]),
    }), expect.stringContaining("Individuella fackbredder"));

    fireEvent.change(screen.getByLabelText("Höjd för hylla 1"), { target: { value: "30" } });
    expect(onChange).toHaveBeenCalledWith({ shelf_height_ratios: [0.3, expect.any(Number)] }, expect.stringContaining("Individuella hyllnivåer"));
  });

  it("does not materialize infeasible custom ratios at the full bay and shelf envelope", () => {
    const onChange = vi.fn();
    render(<StudioInspector spec={{
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 6_000,
      height_mm: 4_000,
      depth_mm: 1_200,
      divider_count: 16,
      shelf_count: 40,
      bay_width_ratios: [],
      shelf_height_ratios: [],
      reinforcement_mode: "manual",
    }} status="PASS" partCount={752} onChange={onChange} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    fireEvent.click(screen.getByText("Detaljerad indelning"));

    expect(screen.getByText(/Individuella fackbredder är avstängda vid 13–17 fack/)).toBeInTheDocument();
    expect(screen.getByText(/Individuella hyllnivåer är avstängda vid 20–40 nivåer/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Bredd för fack 1")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Höjd för hylla 1")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Öka hyllnivåer" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Likadana" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Fördela jämnt" })).toBeDisabled();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("uses measured thickness in the strict wall-library base-height maximum", () => {
    render(<StudioInspector spec={{
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      height_mm: 900,
      measured_thickness_mm: 18.5,
      base_cabinet_height_mm: 500,
      base_cabinet_depth_mm: DEFAULT_DESIGN_SPEC.depth_mm,
      base_cabinet_count: 1,
    }} status="PASS" partCount={20} onChange={vi.fn()} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    expect(screen.getByLabelText("Höjd underskåp")).toHaveAttribute("max", "681");
  });

  it("keeps automatically derived base modules read-only", () => {
    render(<StudioInspector spec={{
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      base_cabinet_height_mm: 680,
      base_cabinet_depth_mm: DEFAULT_DESIGN_SPEC.depth_mm,
      base_cabinet_count: 3,
      reinforcement_mode: "auto",
    }} status="PASS" partCount={30} onChange={vi.fn()} onOpenExplore={vi.fn()} onOpenCheck={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Minska skåpsmoduler" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Öka skåpsmoduler" })).toBeDisabled();
    expect(screen.getByText(/följer automatiskt de bärande centrumlinjerna/i)).toBeInTheDocument();
  });

  it("opens the real construction check from Studio", () => {
    const onOpenCheck = vi.fn();
    render(<StudioInspector spec={DEFAULT_DESIGN_SPEC} status="BLOCK" partCount={18} onChange={vi.fn()} onOpenExplore={vi.fn()} onOpenCheck={onOpenCheck} />);
    fireEvent.click(screen.getByRole("button", { name: /Kontrollera konstruktionen/ }));
    expect(onOpenCheck).toHaveBeenCalledTimes(1);
  });
});
