import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CustombuildApiClient } from "@/lib/api-client";
import { newFurnitureWorkspace, type FurniturePreview, type FurnitureShelfSuggestion } from "@/lib/furniture-workspace";
import { FurnitureShelfSuggestionPanel } from "./furniture-shelf-suggestion";
import { FurnitureRuleValues } from "./furniture-rule-values";

function proposal(): FurnitureShelfSuggestion {
  const workspace = newFurnitureWorkspace("shelving");
  workspace.design.intent.width_um = 4_340_000;
  const current: FurniturePreview = {
    schema_version: "custombuild.furniture-preview.v1", workspace,
    design: { design_hash: "a".repeat(64), intent_hash: "b".repeat(64), parts: [], moving_groups: [], total_weight_g: 10_000 },
    rules: { overall_status: "BLOCK", evaluations: [{ rule_id: "CB-DEFLECTION-001", title: "Hyllans nedböjning",
      status: "BLOCK", detail: "Jämn last inklusive egenvikt.", values: { calculated: 49_233, allowed: 3_000, unit: "µm" } }] },
    manufacturing: { state: "not_selected", geometry_compatible: null, issues: [] },
    physical_cutting_authorized: false, production_qualified: false,
  };
  const proposed = structuredClone(current);
  proposed.workspace.design.intent.divider_count = 4;
  proposed.rules.evaluations[0]!.values.calculated = 1_217;
  proposed.rules.evaluations[0]!.status = "PASS";
  return {
    schema_version: "custombuild.furniture-shelf-suggestion.v1", state: "available", code: "SHELF_BAYS_AVAILABLE",
    message: "Förslag: 5 fack.", scope: "Förband och materialbatch behöver granskas.", current, proposed,
    changed_fields: [{ field: "design.intent.divider_count", before: 1, after: 4 }],
    current_bays: { divider_count: 1, bay_count: 2, clear_widths_um: [2_143_000, 2_143_000] },
    proposed_bays: { divider_count: 4, bay_count: 5, clear_widths_um: [846_400, 846_400, 846_400, 846_400, 846_400] },
    screening_checks: { current: [], proposed: [{ rule_id: "CB-DEFLECTION-001", status: "PASS", numeric_status: "PASS",
      calculated: 1_217, allowed: 3_000, unit: "µm" }] },
    search: { max_divider_count: 16, attempted_candidate_count: 3, evaluated_candidate_count: 3 },
    remaining_issues: { rules: [{ rule_id: "CB-JOINT-001", title: "Fogarnas hållning", status: "WARNING", detail: "Behöver provning.", values: {} }],
      manufacturing: [{ code: "PART_EXCEEDS_STOCK", message: "Toppstycket ryms inte på vald råskiva." }] },
    can_apply: true, production_qualified: false, physical_cutting_authorized: false,
  };
}

describe("förslag till hyllfack", () => {
  it("visar före/efter med måttenheter och behåller designen tills användaren tillämpar", async () => {
    const response = proposal();
    const api = new CustombuildApiClient("https://api.example.test");
    vi.spyOn(api, "suggestFurnitureShelfBays").mockResolvedValue(response);
    const onApply = vi.fn();
    render(<FurnitureShelfSuggestionPanel api={api} workspace={response.current.workspace}
      designHash={response.current.design.design_hash} disabled={false} onApply={onApply} />);
    fireEvent.click(screen.getByRole("button", { name: "Föreslå fackindelning" }));
    const table = await screen.findByRole("table");
    expect(within(table).getByText("49,233 mm")).toBeVisible();
    expect(within(table).getByText("1,217 mm")).toBeVisible();
    expect(within(table).getByText("3 mm")).toBeVisible();
    expect(screen.getByText("Toppstycket ryms inte på vald råskiva.")).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
    expect(response.current.workspace.design.intent.divider_count).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Använd föreslagen fackindelning" }));
    expect(onApply).toHaveBeenCalledWith(response.proposed!.workspace);
    expect(api.suggestFurnitureShelfBays).toHaveBeenCalledTimes(1);
  });

  it("erbjuder ingen tillämpning när befintlig indelning redan klarar kontrollerna", async () => {
    const response = { ...proposal(), state: "already_pass" as const, can_apply: false, proposed: null, proposed_bays: null,
      message: "Nuvarande fack klarar den numeriska screeningen." };
    const api = new CustombuildApiClient("https://api.example.test");
    vi.spyOn(api, "suggestFurnitureShelfBays").mockResolvedValue(response);
    render(<FurnitureShelfSuggestionPanel api={api} workspace={response.current.workspace}
      designHash={response.current.design.design_hash} disabled={false} onApply={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Föreslå fackindelning" }));
    await screen.findByText(response.message);
    expect(screen.queryByRole("button", { name: "Använd föreslagen fackindelning" })).toBeNull();
  });

  it("avvisar förslag för annan designidentitet", async () => {
    const response = proposal();
    const api = new CustombuildApiClient("https://api.example.test");
    vi.spyOn(api, "suggestFurnitureShelfBays").mockResolvedValue(response);
    render(<FurnitureShelfSuggestionPanel api={api} workspace={response.current.workspace}
      designHash={"c".repeat(64)} disabled={false} onApply={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Föreslå fackindelning" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Designen har ändrats");
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("ignorerar ett sent svar efter att en annan arbetsyta har öppnats", async () => {
    const response = proposal();
    const api = new CustombuildApiClient("https://api.example.test");
    let resolve!: (value: FurnitureShelfSuggestion) => void;
    vi.spyOn(api, "suggestFurnitureShelfBays").mockReturnValue(new Promise(done => { resolve = done; }));
    const onApply = vi.fn();
    const view = render(<FurnitureShelfSuggestionPanel key="old" api={api} workspace={response.current.workspace}
      designHash={response.current.design.design_hash} disabled={false} onApply={onApply} />);
    fireEvent.click(screen.getByRole("button", { name: "Föreslå fackindelning" }));
    view.rerender(<FurnitureShelfSuggestionPanel key="new" api={api} workspace={newFurnitureWorkspace("shelving")}
      designHash={"d".repeat(64)} disabled={false} onApply={onApply} />);
    await act(async () => { resolve(response); });
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByRole("button", { name: "Föreslå fackindelning" })).toBeEnabled();
    expect(onApply).not.toHaveBeenCalled();
  });

  it("visar bordets och byråns kanoniska beräkningsvärden", () => {
    const view = render(<FurnitureRuleValues rule={{ rule_id: "table", title: "Bord", status: "BLOCK", detail: "",
      values: { calculated_mm: 4.51, allowed_mm: 3, span_um: 810_000 } }} />);
    expect(screen.getByText(/Beräknat: 4,51 mm/)).toBeVisible();
    expect(screen.getByText("Fri spännvidd: 810 mm")).toBeVisible();
    view.rerender(<FurnitureRuleValues rule={{ rule_id: "chest", title: "Byrå", status: "BLOCK", detail: "",
      values: { residual_moment_nm: -20, open_drawers: 3 } }} />);
    expect(screen.getByText(/Återstående stabiliserande moment: −?[-]?20 Nm/)).toBeVisible();
  });
});
