import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { RuleEvaluation } from "@/lib/design-types";
import { ValidationPanel } from "./validation-panel";

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
  suggestion: {
    action: "set_divider_count",
    label: "Inför 1 vertikal avdelare",
    value: 1,
    explanation: "Kortare spännvidd",
  },
};

describe("ValidationPanel", () => {
  it("applies a visible deterministic suggestion", () => {
    const onApply = vi.fn();
    render(
      <ValidationPanel
        evaluations={[evaluation]}
        status="WARNING"
        onApplySuggestion={onApply}
        onSelectPart={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /inför 1 vertikal avdelare/i }));
    expect(onApply).toHaveBeenCalledWith(evaluation);
  });

  it("selects the affected part from a rule", () => {
    const onSelect = vi.fn();
    render(
      <ValidationPanel
        evaluations={[evaluation]}
        status="WARNING"
        onApplySuggestion={vi.fn()}
        onSelectPart={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /visa i modell/i }));
    expect(onSelect).toHaveBeenCalledWith("shelf-1-bay-1");
  });
});
