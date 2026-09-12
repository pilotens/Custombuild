import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { FurnitureTrialReadiness } from "@/lib/furniture-workspace";
import { FurnitureTrialReadinessPanel } from "./furniture-trial-readiness";

const designHash = "a".repeat(64);
function report(): FurnitureTrialReadiness {
  return {
    schema_version: "custombuild.furniture-trial-readiness.v1", scope: "design_review_preparation",
    design_hash: designHash, workspace_sha256: "b".repeat(64), report_sha256: "c".repeat(64),
    state: "requires_design_change", blocker_count: 1, evidence_required_count: 1,
    next_action: "Dela möbeln i transportbara moduler och kontrollera fogarna.",
    checks: [
      { code: "STOCK_AND_WORK_AREA", title: "Råformat och arbetsyta", state: "blocked",
        detail: "Toppstycket ryms inte på råskivan.", action: "Ändra råformat eller konstruktion." },
      { code: "DIMENSION_CHAIN", title: "Måttkedja", state: "checked",
        detail: "Angivna mått går ihop i modellen.", action: "Mät installationsplatsen." },
      { code: "ACTUAL_MATERIAL", title: "Verkligt material", state: "requires_evidence",
        detail: "Materialets egenskaper behöver styrkas.", action: "Dokumentera materialbatch och tjocklek." },
    ],
    production_qualified: false, physical_cutting_authorized: false,
  };
}

describe("beredning inför verkstadsprov", () => {
  it("prioriterar aktuell blockerare och visar skillnaden mellan modellkontroll och verkliga belägg", () => {
    render(<FurnitureTrialReadinessPanel report={report()} designHash={designHash} dirty={true} />);
    const panel = screen.getByRole("region", { name: "Inför verkstadsprov" });
    expect(within(panel).getByText("Designen behöver åtgärdas före provet.")).toBeVisible();
    expect(within(panel).getByText("1 punkt blockerar provet · 1 punkt behöver styrkas.")).toBeVisible();
    expect(within(panel).getByText(/Dela möbeln i transportbara moduler/)).toBeVisible();
    expect(within(panel).getByText("Toppstycket ryms inte på råskivan.")).toBeVisible();
    expect(within(panel).getByText("Råformat och arbetsyta").closest("details")).toHaveAttribute("open");
    expect(within(panel).getByText("Måttkedja").closest("details")).not.toHaveAttribute("open");
    expect(within(panel).getByText("Kontrollerat i modellen")).toBeVisible();
    expect(within(panel).getByText("Behöver styrkas")).toBeVisible();
    expect(within(panel).getByText(/aktuella arbetskopia/)).toBeVisible();
    expect(within(panel).getByText(/ger inget tillstånd att köra maskinen/)).toBeVisible();
  });

  it("visar fortsatt behov av verklig provning när designen saknar blockerare", () => {
    const current = report();
    current.state = "requires_workshop_evidence";
    current.blocker_count = 0;
    current.checks = current.checks.filter(check => check.state !== "blocked");
    current.next_action = "Dokumentera materialbatch och tjocklek.";
    render(<FurnitureTrialReadinessPanel report={current} designHash={designHash} dirty={false} />);
    expect(screen.getByText("Verkstadsunderlag och praktisk provning återstår.")).toBeVisible();
    expect(screen.getByText(/Bedömningen gäller den aktuella förhandsgranskningen/)).toBeVisible();
    expect(screen.getByText(/ger inget tillstånd att köra maskinen/)).toBeVisible();
    expect(screen.queryByText("Designen behöver åtgärdas före provet.")).toBeNull();
  });

  it("visar inga tidigare godkända kontroller när rapporten saknas eller gäller en annan design", () => {
    const view = render(<FurnitureTrialReadinessPanel report={report()} designHash={"d".repeat(64)} dirty={false} />);
    expect(screen.getByText(/En aktuell provbedömning saknas/)).toBeVisible();
    expect(screen.queryByText("Kontrollerat i modellen")).toBeNull();
    expect(screen.queryByText(/Dela möbeln i transportbara moduler/)).toBeNull();
    view.rerender(<FurnitureTrialReadinessPanel designHash={designHash} dirty={false} />);
    expect(screen.getByText(/En aktuell provbedömning saknas/)).toBeVisible();
    expect(screen.queryByText("Verkstadsunderlag och praktisk provning återstår.")).toBeNull();
  });
});
