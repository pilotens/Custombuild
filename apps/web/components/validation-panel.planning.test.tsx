import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { resolveDesign } from "@/lib/design-engine";
import { DEFAULT_DESIGN_SPEC, type DesignSpec, type ResolvedDesign } from "@/lib/design-types";
import { resolveSemanticRemoval } from "@/lib/semantic-design";
import { ValidationPanel } from "./validation-panel";

const library: DesignSpec = {
  ...DEFAULT_DESIGN_SPEC,
  furniture_type: "wall_library",
  width_mm: 2_400,
  height_mm: 2_400,
  depth_mm: 340,
  divider_count: 3,
  base_cabinet_count: 4,
  base_cabinet_height_mm: 680,
  base_cabinet_depth_mm: 340,
};

function callbacks() {
  return {
    onRequestPreview: vi.fn(),
    onCancelPreview: vi.fn(),
    onConfirmPreview: vi.fn(),
    onSelectPart: vi.fn(),
    onNavigateToStep: vi.fn(),
    onOpenProduction: vi.fn(),
  };
}

function panel(design: ResolvedDesign, actions = callbacks()) {
  return (
    <ValidationPanel
      spec={design.spec}
      parts={design.parts}
      source={design.source}
      evaluations={design.rule_evaluations}
      status={design.status}
      {...actions}
    />
  );
}

function stockCard() {
  return screen.getByRole("article", { name: "Delar ryms i råmaterial" });
}

describe("ValidationPanel automatic planning", () => {
  it("calculates a concise material quantity while retaining the original blocking evidence", () => {
    const design = resolveDesign(library);
    const original = structuredClone(design);
    const actions = callbacks();
    render(panel(design, actions));

    const stock = stockCard();
    expect(within(stock).getByText("Beräknat behov: 8 skivor")).toBeVisible();
    expect(within(stock).getByText("6 till stommen + 2 till bakstycket. 2 fler än angivet antal behöver ordnas. Tillgången är inte verifierad.")).toBeVisible();
    expect(within(stock).getByText("Måste lösas")).toBeVisible();
    expect(screen.getByLabelText("1 måste lösas och 3 behöver beslut")).toBeVisible();
    expect(screen.getByText("Tillverkning är spärrad tills kraven är lösta.")).toBeVisible();
    const evidence = within(stock).getByRole("region", { name: "Automatiskt beräknat materialbehov" });
    expect(evidence).not.toBeVisible();
    fireEvent.click(within(stock).getByText("Visa detaljer"));
    expect(evidence).toBeVisible();
    expect(within(evidence).getByText("6 skivor à 2440 × 1220 mm. Angivet antal: 4.")).toBeVisible();
    expect(within(evidence).getByText("2 skivor à 2440 × 1220 mm. Angivet antal: 2.")).toBeVisible();
    expect(evidence).toHaveTextContent("Geometrisk uppskattning, inte verifierat lager eller färdig kapplan");
    expect(stock).toHaveTextContent(design.rule_evaluations.find((rule) => rule.rule_id === "DFM-STOCK-001")!.summary);
    expect(design).toEqual(original);
    for (const action of Object.values(actions)) expect(action).not.toHaveBeenCalled();
  });

  it("recalculates quantities immediately when the rendered geometry changes", () => {
    const design = resolveDesign(library);
    const { rerender } = render(panel(design));
    expect(within(stockCard()).getByText("Beräknat behov: 8 skivor")).toBeVisible();

    const withoutBack = resolveDesign(resolveSemanticRemoval(library, "back_panel").spec);
    rerender(panel(withoutBack));

    const stock = stockCard();
    expect(within(stock).getByText("Beräknat behov: 6 skivor")).toBeVisible();
    expect(within(stock).getByText("6 till stommen. 2 fler än angivet antal behöver ordnas. Tillgången är inte verifierad.")).toBeVisible();
    expect(within(stock).queryByText(/till bakstycket/)).not.toBeInTheDocument();
    expect(withoutBack.spec.stock_count).toBe(4);
    expect(withoutBack.spec.back_stock_count).toBe(2);
    expect(withoutBack.rule_evaluations.find((rule) => rule.rule_id === "DFM-STOCK-001")?.status).toBe("BLOCK");
  });

  it("never changes exact workshop quantities, context, or rule status while showing a plan", () => {
    const spec: DesignSpec = {
      ...library,
      back_panel: false,
      workshop_context: {
        stock_profiles: [{
          role: "carcass",
          declaration_authority: "CLIENT_DECLARED",
          supplier_profile_id: "supplier-birch-18",
          supplier_profile_version: "batch-2026-09",
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
          fixture_keep_out_zones: [{ x_um: 0, y_um: 0, width_um: 40_000, height_um: 1_220_000 }],
        }],
      },
    };
    const design = resolveDesign(spec);
    const originalSpec = structuredClone(spec);
    const originalDesign = structuredClone(design);
    const actions = callbacks();
    render(panel(design, actions));
    fireEvent.click(within(stockCard()).getByText("Visa detaljer"));

    expect(within(stockCard()).getByText("Beräknat behov: 6 skivor")).toBeVisible();
    expect(screen.getByText(/Inga lageruppgifter ändras/)).toBeVisible();
    expect(spec).toEqual(originalSpec);
    expect(design).toEqual(originalDesign);
    expect(design.spec.workshop_context?.stock_profiles[0]?.sheet_count).toBe(4);
    expect(design.status).toBe("BLOCK");
    for (const action of Object.values(actions)) expect(action).not.toHaveBeenCalled();
  });

  it("does not substitute a local stock solution or implementation-gap diagnosis for server decisions", () => {
    const design: ResolvedDesign = { ...resolveDesign(library), source: "server-preview" };
    const original = structuredClone(design);
    const actions = callbacks();
    render(panel(design, actions));

    expect(within(stockCard()).queryByText(/Beräknat behov:/)).not.toBeInTheDocument();
    expect(within(stockCard()).getByText("Materialet räcker inte")).toBeVisible();
    expect(within(stockCard()).getByText("Måste lösas")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Tillverkningsstödet är inte färdigt" })).not.toBeInTheDocument();
    for (const ruleId of ["CB-JOINT-001", "CB-HARDWARE-001"]) {
      const rule = design.rule_evaluations.find((item) => item.rule_id === ruleId)!;
      expect(screen.getByRole("article", { name: rule.title })).toBeVisible();
    }
    expect(screen.queryByRole("button", { name: /Visa förslag|Bekräfta och tillämpa|Öppna underlag/ })).not.toBeInTheDocument();
    expect(design).toEqual(original);
    for (const action of Object.values(actions)) expect(action).not.toHaveBeenCalled();
  });

  it("does not claim a complete sheet quantity when some parts exceed the selected format", () => {
    const design = resolveDesign({ ...library, width_mm: 4_200 });
    render(panel(design));

    const stock = stockCard();
    expect(within(stock).queryByText(/Beräknat behov:/)).not.toBeInTheDocument();
    expect(within(stock).getByText("Måste lösas")).toBeVisible();
    fireEvent.click(within(stock).getByText("Visa detaljer"));
    const evidence = within(stock).getByRole("region", { name: "Automatiskt beräknat materialbehov" });
    expect(within(evidence).getByText("Ofullständig materialplan")).toBeVisible();
    expect(within(evidence).getByText("Alla delar ryms inte i beräkningen. Fler skivor löser inte ett för litet skivformat.")).toBeVisible();
    expect(within(evidence).queryByText("Preliminär materialplan")).not.toBeInTheDocument();
    expect(design.rule_evaluations.find((rule) => rule.rule_id === "DFM-STOCK-001")?.status).toBe("BLOCK");
  });

  it("groups known local capability gaps without dropping their warnings or exact evidence", () => {
    const design = resolveDesign(library);
    const original = structuredClone(design.rule_evaluations);
    const actions = callbacks();
    render(panel(design, actions));

    const capability = screen.getByRole("region", { name: "Tillverkningsstödet är inte färdigt" });
    expect(capability).toBeVisible();
    expect(capability).toHaveTextContent("foglåsning samt gångjärn och borrhål");
    expect(capability).toHaveTextContent("Det kräver utveckling av appen, inte fler inställningar från dig.");
    const disclosure = within(capability).getByText("Visa 2 kvarstående krav");
    expect(disclosure.closest("details")).not.toHaveAttribute("open");
    const cards = ["CB-JOINT-001", "CB-HARDWARE-001"].map((ruleId) => {
      const rule = design.rule_evaluations.find((item) => item.rule_id === ruleId)!;
      const card = within(capability).getByRole("article", { name: rule.title });
      expect(card).not.toBeVisible();
      return { rule, card };
    });
    expect(screen.getByLabelText("1 måste lösas och 3 behöver beslut")).toBeVisible();
    fireEvent.click(disclosure);
    for (const { rule, card } of cards) {
      expect(card).toBeVisible();
      expect(within(card).getByText("Behöver beslut")).toBeVisible();
      expect(within(card).getByText(rule.calculation)).not.toBeVisible();
      fireEvent.click(within(card).getByText("Visa detaljer"));
      expect(within(card).getByText(rule.calculation)).toBeVisible();
      for (const assumption of rule.assumptions) expect(within(card).getByText(assumption)).toBeVisible();
      expect(within(card).getByText(`Regel ${rule.rule_id} · version ${rule.rule_version}`)).toBeVisible();
    }
    expect(design.rule_evaluations).toEqual(original);
    expect(screen.getByLabelText("1 måste lösas och 3 behöver beslut")).toBeVisible();
    for (const action of Object.values(actions)) expect(action).not.toHaveBeenCalled();
  });

  it.each(["divider", "shelf_row"] as const)("does not ask to restore an ordinary semantic %s removal", (kind) => {
    const removed = resolveSemanticRemoval(library, kind);
    const design = resolveDesign(removed.spec);
    render(panel(design));

    expect(design.spec.topology_baseline).toBeDefined();
    expect(screen.queryByText("Kontrollera ändrade delar")).not.toBeInTheDocument();
    expect(screen.queryByText("Återställ saknade delar")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Visa förslag:.*återställ/i })).not.toBeInTheDocument();
    expect(design.rule_evaluations.some((rule) => rule.rule_id === "PART-CUSTOM-001")).toBe(false);
    expect(design.rule_evaluations.find((rule) => rule.rule_id === "STR-TOPO-001")?.status).toBe("PASS");
  });
});
