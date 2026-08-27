import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { DEFAULT_DESIGN_SPEC, type RuleEvaluation } from "@/lib/design-types";
import { automaticValidationFixPreview } from "@/lib/validation-guidance";
import { ValidationPanel, type ActiveValidationFixPreview } from "./validation-panel";

const evaluation: RuleEvaluation = {
  rule_id: "STR-DEF-001",
  rule_version: "1.1.0",
  status: "WARNING",
  title: "Hyllnedböjning",
  summary: "Nedböjningen överskrider gränsen.",
  calculation: "δ = 5wL⁴ / (384EI)",
  calculated_value: 6.2,
  allowed_value: 5,
  unit: "mm",
  margin_percent: -24,
  assumptions: ["Jämnt fördelad last"],
  affected_part_ids: ["shelf-1-bay-1"],
  diagnostics: [
    { label: "fri spännvidd", value: "900", unit: "mm" },
    { label: "beräkningsmodell", value: "screening" },
  ],
  suggestion: {
    action: "set_divider_count",
    label: "Inför 1 vertikal avdelare",
    value: 1,
    explanation: "Kortare spännvidd",
  },
};

function renderPanel(
  evaluations: RuleEvaluation[],
  options: {
    onRequest?: (item: RuleEvaluation) => void;
    onCancel?: () => void;
    onConfirm?: () => void;
    onSelect?: (partId: string) => void;
    onNavigate?: (step: 0 | 1 | 2 | 3) => void;
    onOpenProduction?: () => void;
  } = {},
) {
  const status = evaluations.some((item) => item.status === "BLOCK")
    ? "BLOCK"
    : evaluations.some((item) => item.status === "WARNING")
      ? "WARNING"
      : "PASS";
  function ControlledPanel() {
    const [activePreview, setActivePreview] = useState<ActiveValidationFixPreview>();
    return (
      <ValidationPanel
        evaluations={evaluations}
        status={status}
        spec={DEFAULT_DESIGN_SPEC}
        activePreview={activePreview}
        onRequestPreview={(item) => {
          options.onRequest?.(item);
          const preview = automaticValidationFixPreview(item, DEFAULT_DESIGN_SPEC);
          if (!preview) return;
          setActivePreview({
            ruleId: item.rule_id,
            ruleVersion: item.rule_version,
            label: preview.label,
            reason: preview.reason,
            changes: preview.changes,
            noGeometryChange: false,
          });
        }}
        onCancelPreview={() => {
          setActivePreview(undefined);
          options.onCancel?.();
        }}
        onConfirmPreview={() => {
          setActivePreview(undefined);
          options.onConfirm?.();
        }}
        onSelectPart={options.onSelect ?? vi.fn()}
        onNavigateToStep={options.onNavigate}
        onOpenProduction={options.onOpenProduction}
      />
    );
  }
  return render(<ControlledPanel />);
}

describe("ValidationPanel", () => {
  it("shows BLOCK, WARNING and PASS truthfully, with source values only when present", () => {
    const blocked: RuleEvaluation = {
      ...evaluation,
      rule_id: "GEO-001",
      status: "BLOCK",
      title: "Ogiltig geometri",
      summary: "Djupet kan inte lösas.",
      calculated_value: undefined,
      allowed_value: undefined,
      assumptions: [],
      diagnostics: undefined,
      suggestion: undefined,
      affected_part_ids: [],
    };
    const passed: RuleEvaluation = {
      ...blocked,
      rule_id: "PASS-001",
      status: "PASS",
      title: "Skivtjocklek",
      summary: "Skivtjockleken ligger inom gränsen.",
      calculated_value: 18,
      allowed_value: 20,
      unit: "mm",
    };

    renderPanel([passed, evaluation, blocked]);

    const cards = screen.getAllByRole("article");
    expect(within(cards[0]!).getByText("Ogiltig geometri")).toBeVisible();
    expect(within(cards[1]!).getByText("Hyllnedböjning")).toBeVisible();
    expect(within(cards[2]!).getByText("Skivtjocklek")).toBeVisible();
    expect(screen.getAllByText("Måste lösas").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Behöver beslut").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Godkänt").length).toBeGreaterThan(0);

    expect(within(cards[0]!).queryByText("Aktuellt verifierat värde")).not.toBeInTheDocument();
    expect(within(cards[0]!).queryByText("Gränsvärde")).not.toBeInTheDocument();
    const warningEvidence = cards[1]!.querySelector<HTMLElement>(".validation-evidence");
    expect(warningEvidence).not.toBeNull();
    expect(within(warningEvidence!).getByText("6.2 mm")).toBeVisible();
    expect(within(warningEvidence!).getByText("5 mm")).toBeVisible();
    expect(within(warningEvidence!).getByText("900 mm")).toBeVisible();
    expect(within(warningEvidence!).getByText("Jämnt fördelad last")).toBeVisible();
    expect(within(cards[2]!).getByText("Verifierat resultat")).toBeVisible();
    expect(within(cards[2]!).getByText(/Ingen ändring krävs/)).toBeVisible();
    expect(within(cards[2]!).queryByRole("button", { name: /förhandsgranska/i })).not.toBeInTheDocument();
  });

  it("labels maximum, minimum and trigger thresholds with their real direction", () => {
    const maximum = { ...evaluation, rule_id: "CB-DEFLECTION-001" };
    const minimum = { ...evaluation, rule_id: "CB-TIP-001", title: "Tippsäkerhet" };
    const trigger = { ...evaluation, rule_id: "STAB-TIP-002", title: "Förankringskrav" };

    renderPanel([maximum, minimum, trigger]);

    expect(screen.getAllByText("Högsta tillåtna").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Minimikrav").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Förankring krävs från").length).toBeGreaterThan(0);
    expect(screen.queryByText("Tillåtet")).not.toBeInTheDocument();
  });

  it("requires preview and confirmation, supports cancel, and applies the exact fix once", () => {
    const onRequest = vi.fn();
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    renderPanel([evaluation], { onRequest, onCancel, onConfirm });

    const previewButton = screen.getByRole("button", { name: /förhandsgranska: inför 1 vertikal avdelare/i });
    fireEvent.click(previewButton);
    expect(onRequest).toHaveBeenCalledWith(evaluation);
    expect(onConfirm).not.toHaveBeenCalled();

    const preview = screen.getByRole("region", { name: "Kontrollera ändringen före tillämpning" });
    expect(within(preview).getByText("divider_count")).toBeVisible();
    expect(within(preview).getByText("reinforcement_mode")).toBeVisible();
    expect(within(preview).getByText("0", { selector: "code" })).toBeVisible();
    expect(within(preview).getByText("1", { selector: "code" })).toBeVisible();
    expect(within(preview).getByText("auto", { selector: "code" })).toBeVisible();
    expect(within(preview).getByText("manual", { selector: "code" })).toBeVisible();
    expect(preview).toHaveTextContent("lokalt beräknad");
    expect(preview).toHaveTextContent("inte serververifierad eller tillämpad");

    fireEvent.click(within(preview).getByRole("button", { name: "Avbryt" }));
    expect(screen.queryByRole("region", { name: "Kontrollera ändringen före tillämpning" })).not.toBeInTheDocument();
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(previewButton).toHaveFocus();
    expect(onConfirm).not.toHaveBeenCalled();

    fireEvent.click(previewButton);
    fireEvent.click(screen.getByRole("button", { name: "Bekräfta och tillämpa" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Bekräfta och tillämpa" })).not.toBeInTheDocument();
  });

  it("opens stockless Underlag without previewing a stock mutation", () => {
    const ruleId = "DFM-STOCK-001";
    const title = "Delar ryms i råmaterial";
    const summary = "Fyra delar ryms inte i valt skivformat.";
    const onRequest = vi.fn();
    const onOpenProduction = vi.fn();
    const blockingEvaluation: RuleEvaluation = {
      ...evaluation,
      rule_id: ruleId,
      status: "BLOCK",
      title,
      summary,
      affected_part_ids: ["side-left"],
      suggestion: {
        action: "create_stockless_review_package",
        label: "Skapa lagerobundet granskningsunderlag",
        value: true,
        explanation: "Behåll verkliga mått och blockera nesting och CAM.",
      },
    };
    renderPanel([blockingEvaluation], { onRequest, onOpenProduction });

    const repair = screen.getByRole("button", {
      name: `Åtgärda problem för ${title}: öppna Lagerobundet granskningspaket`,
    });
    expect(repair).toHaveTextContent("Åtgärda problem");
    fireEvent.click(repair);

    expect(onOpenProduction).toHaveBeenCalledTimes(1);
    expect(onRequest).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", {
      name: "Kontrollera ändringen före tillämpning",
    })).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("5000");
    expect(document.body).not.toHaveTextContent("5125");
  });

  it("does not offer stockless navigation for a capacity-only stock shortage", () => {
    const onOpenProduction = vi.fn();
    const capacityOnly: RuleEvaluation = {
      ...evaluation,
      rule_id: "DFM-STOCK-001",
      status: "BLOCK",
      title: "Delar ryms i råmaterial",
      summary: "Det finns för få skivor för alla delar.",
      affected_part_ids: ["side-left"],
      suggestion: undefined,
    };

    renderPanel([capacityOnly], { onOpenProduction });

    expect(screen.queryByRole("button", { name: /Åtgärda problem/i })).not.toBeInTheDocument();
    expect(onOpenProduction).not.toHaveBeenCalled();
  });

  it("does not offer stockless navigation when only the machine rule is blocked", () => {
    const onOpenProduction = vi.fn();
    const machineOnly: RuleEvaluation = {
      ...evaluation,
      rule_id: "DFM-MACHINE-001",
      status: "BLOCK",
      title: "Maskinens arbetsområde",
      summary: "En del ryms inte i vald maskinprofil.",
      affected_part_ids: ["side-left"],
      suggestion: {
        action: "create_stockless_review_package",
        label: "Skapa lagerobundet granskningsunderlag",
        value: true,
        explanation: "Behåll verkliga mått och blockera nesting och CAM.",
      },
    };

    renderPanel([machineOnly], { onOpenProduction });

    expect(screen.queryByRole("button", { name: /Åtgärda problem/i })).not.toBeInTheDocument();
    expect(onOpenProduction).not.toHaveBeenCalled();
  });

  it("keeps unsupported and non-numeric checks free from invented values", () => {
    const onOpenProduction = vi.fn();
    const externalCheck: RuleEvaluation = {
      ...evaluation,
      rule_id: "STAB-TIP-002",
      status: "BLOCK",
      title: "Tipprisk och väggförankring",
      summary: "Infästningen är inte verifierad.",
      calculated_value: undefined,
      allowed_value: undefined,
      unit: undefined,
      margin_percent: undefined,
      assumptions: [],
      diagnostics: [{ label: "väggtyp", value: "saknas" }],
      suggestion: {
        action: "manual_review",
        label: "Registrera väggförankring",
        value: false,
        explanation: "Externt underlag krävs.",
      },
      affected_part_ids: [],
    };
    renderPanel([externalCheck], { onOpenProduction });

    expect(screen.queryByText("Aktuellt verifierat värde")).not.toBeInTheDocument();
    expect(screen.queryByText("Gräns")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /förhandsgranska/i })).not.toBeInTheDocument();
    const technical = screen.getByText("Tekniska detaljer").closest("details");
    expect(technical).not.toHaveAttribute("open");
    expect(screen.getByText("saknas")).not.toBeVisible();
    const repair = screen.getByRole("button", { name: /Åtgärda problem för .*: öppna/i });
    expect(repair).toHaveTextContent("Åtgärda problem");
    fireEvent.click(repair);
    expect(onOpenProduction).toHaveBeenCalledTimes(1);
  });

  it("states explicitly when a controlled preview has no geometry change", () => {
    render(
      <ValidationPanel
        evaluations={[evaluation]}
        status="WARNING"
        spec={DEFAULT_DESIGN_SPEC}
        activePreview={{
          ruleId: evaluation.rule_id,
          ruleVersion: evaluation.rule_version,
          label: "Ändra kontrollvärde",
          reason: "Kontrollera underlaget",
          changes: [{ field: "stock_count", before: 8, after: 10 }],
          noGeometryChange: true,
        }}
        onRequestPreview={vi.fn()}
        onCancelPreview={vi.fn()}
        onConfirmPreview={vi.fn()}
        onSelectPart={vi.fn()}
      />,
    );

    const preview = screen.getByRole("region", { name: "Kontrollera ändringen före tillämpning" });
    expect(preview).toHaveTextContent("Ingen geometriförändring");
    expect(preview).toHaveTextContent("endast specifikations- eller regelunderlag ändras");
  });

  it("makes every unique affected part a keyboard-focusable exact focus action", () => {
    const onSelect = vi.fn();
    renderPanel([{ ...evaluation, affected_part_ids: ["side-left", "side-right", "side-left", ""] }], { onSelect });

    const left = screen.getByRole("button", { name: "Fokusera delen side-left i modellen" });
    const right = screen.getByRole("button", { name: "Fokusera delen side-right i modellen" });
    expect(left.tagName).toBe("BUTTON");
    expect(right.tagName).toBe("BUTTON");
    expect(left).toHaveAttribute("type", "button");
    left.focus();
    expect(left).toHaveFocus();
    fireEvent.click(right);
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("side-right");
  });

  it("does not fake a repair route when dry retention has no product control", () => {
    const onNavigate = vi.fn();
    const block: RuleEvaluation = {
      ...evaluation,
      rule_id: "CB-JOINT-001",
      status: "BLOCK",
      title: "Lokalt upplag i hyllspår och hyllbärare",
      summary: "Förbandets lokala upplag är inte verifierat.",
      diagnostics: undefined,
      suggestion: undefined,
    };
    renderPanel([block], { onNavigate });

    expect(screen.getByText(/Välj och versionslås ett självlåsande torrförband/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Åtgärda problem/i })).not.toBeInTheDocument();
    expect(onNavigate).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /ignorera|fortsätt ändå/i })).not.toBeInTheDocument();
  });
});
