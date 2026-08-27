import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RuleEvaluation } from "@/lib/design-types";

const transactionMock = vi.hoisted(() => ({ preview: vi.fn() }));
const apiMock = vi.hoisted(() => ({ calls: vi.fn() }));
const evaluationMock = vi.hoisted(() => ({ current: undefined as RuleEvaluation | undefined }));
const fixMock = vi.hoisted(() => ({ mode: "geometry" as "geometry" | "intent" | "noop" }));

vi.mock("next/dynamic", () => ({
  default: () => function MockFurnitureViewer(props: {
    parts: Array<{ kind: string }>;
    designSize: { widthMm: number };
    comparisonPreview?: {
      proposedParts: ReadonlyArray<{ kind: string }>;
      designSize: { widthMm: number };
    };
    semanticSpec?: {
      bay_sizing_mode: string;
      target_bay_width_mm: number;
    };
  }) {
    return (
      <div
        data-testid="furniture-viewer"
        data-current-width={props.designSize.widthMm}
        data-current-divider-count={props.parts.filter((part) => part.kind === "divider").length}
        data-comparison-active={Boolean(props.comparisonPreview)}
        data-proposed-width={props.comparisonPreview?.designSize.widthMm ?? ""}
        data-proposed-divider-count={props.comparisonPreview?.proposedParts.filter((part) => part.kind === "divider").length ?? ""}
        data-bay-sizing-mode={props.semanticSpec?.bay_sizing_mode ?? ""}
        data-target-bay-width={props.semanticSpec?.target_bay_width_mm ?? ""}
      />
    );
  },
}));

vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  class MockCustombuildApiClient {
    readonly configured = false;
    readonly authenticated = false;
    previewDesign() { apiMock.calls("previewDesign"); }
    autofixDesign() { apiMock.calls("autofixDesign"); }
    updateProjectDraft() { apiMock.calls("updateProjectDraft"); }
  }
  return { ...actual, CustombuildApiClient: MockCustombuildApiClient };
});

vi.mock("@/lib/design-engine", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/design-engine")>();
  return {
    ...actual,
    resolveDesign(spec: Parameters<typeof actual.resolveDesign>[0], diff?: Parameters<typeof actual.resolveDesign>[1]) {
      const design = actual.resolveDesign(spec, diff);
      const failing = spec.width_mm === 1_200;
      const evaluation = failing && evaluationMock.current
        ? evaluationMock.current
        : {
            rule_id: "TEST-GHOST-001",
            rule_version: "1.0.0",
            status: failing ? "WARNING" as const : "PASS" as const,
            title: "Lokal jämförelse",
            summary: failing ? "Konstruktionen behöver räknas om." : "Konstruktionen är omräknad.",
            calculation: "deterministisk testregel",
            assumptions: [],
            affected_part_ids: [],
            diagnostics: [{ label: "bredd", value: String(spec.width_mm), unit: "mm" }],
            ...(failing ? {
              suggestion: {
                action: "align_base_cabinets" as const,
                label: "Räkna om konstruktionen",
                value: 4,
                explanation: "Testa den normaliserade konstruktionen lokalt",
              },
            } : {}),
          } satisfies RuleEvaluation;
      if (failing) evaluationMock.current = evaluation;
      return {
        ...design,
        status: failing ? "WARNING" as const : "PASS" as const,
        rule_evaluations: [evaluation],
      };
    },
  };
});

vi.mock("@/lib/validation-guidance", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/validation-guidance")>();
  type PreviewSpec = Record<string, unknown> & {
    bay_sizing_mode: "count" | "target_width";
    target_bay_width_mm: number;
    width_mm: number;
    base_cabinet_count: number;
    reinforcement_mode: "auto" | "manual";
  };
  const fixFor = (spec: PreviewSpec) => {
    if (fixMock.mode === "intent") {
      return {
        label: "Byt indelningsavsikt",
        patch: {
          bay_sizing_mode: "target_width" as const,
          target_bay_width_mm: 800,
        },
        reason: "Spara samma geometri med explicit fackbreddsavsikt",
      };
    }
    if (fixMock.mode === "noop") {
      return {
        label: "Behåll nuvarande avsikt",
        patch: {
          bay_sizing_mode: spec.bay_sizing_mode,
          target_bay_width_mm: spec.target_bay_width_mm,
        },
        reason: "Behåll redan normaliserad avsikt",
      };
    }
    return {
      label: "Räkna om konstruktionen",
      patch: {
        width_mm: 4_200,
        base_cabinet_count: 4,
        reinforcement_mode: "auto" as const,
      },
      reason: "Testa den normaliserade konstruktionen lokalt",
    };
  };
  return {
    ...actual,
    automaticValidationFix: (_evaluation: unknown, spec: PreviewSpec) => fixFor(spec),
    automaticValidationFixPreview: (_evaluation: unknown, spec: PreviewSpec) => {
      const fix = fixFor(spec);
      return {
        ...fix,
        changes: Object.entries(fix.patch).map(([field, after]) => ({
          field,
          before: spec[field],
          after,
        })),
      };
    },
  };
});

vi.mock("@/lib/workspace-design-transaction", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/workspace-design-transaction")>();
  return {
    ...actual,
    previewWorkspaceDesignTransaction: (...args: Parameters<typeof actual.previewWorkspaceDesignTransaction>) => {
      transactionMock.preview(...args);
      return actual.previewWorkspaceDesignTransaction(...args);
    },
  };
});

import { DEFAULT_DESIGN_SPEC } from "@/lib/design-types";
import { ANONYMOUS_PROJECT_ID, writeWorkspaceDraft } from "@/lib/workspace-draft-storage";
import { CustombuildWorkspace, validationEvaluationSignature } from "./custombuild-workspace";

const sourceSpec = {
  ...DEFAULT_DESIGN_SPEC,
  design_id: "validation-ghost-test",
  furniture_type: "wall_library" as const,
  width_mm: 1_200,
  height_mm: 2_600,
  depth_mm: 320,
  divider_count: 0,
  base_cabinet_height_mm: 720,
  base_cabinet_depth_mm: 320,
  base_cabinet_count: 1,
  reinforcement_mode: "manual" as const,
};

async function renderWorkspace() {
  render(<CustombuildWorkspace />);
  const viewer = await screen.findByTestId("furniture-viewer");
  await waitFor(() => expect(viewer).toHaveAttribute("data-current-width", "1200"));
  return viewer;
}

const signatureEvaluation: RuleEvaluation = {
  rule_id: "SIG-001",
  rule_version: "1.2.3",
  status: "WARNING",
  title: "Signaturkontroll",
  summary: "Kontrollerar hela evalueringen.",
  calculation: "a + b",
  calculated_value: 12,
  allowed_value: 10,
  unit: "mm",
  margin_percent: -20,
  assumptions: ["första", "andra"],
  affected_part_ids: ["side-left", "side-right"],
  diagnostics: [{ label: "mått", value: "12", unit: "mm" }],
  suggestion: {
    action: "align_base_cabinets",
    label: "Justera",
    value: 2,
    explanation: "Justera lokalt",
  },
};

describe("validation evaluation identity", () => {
  it("canonicalizes every nested object key while preserving array order", () => {
    const reordered = {
      suggestion: {
        explanation: "Justera lokalt",
        value: 2,
        label: "Justera",
        action: "align_base_cabinets" as const,
      },
      diagnostics: [{ unit: "mm", value: "12", label: "mått" }],
      affected_part_ids: ["side-left", "side-right"],
      assumptions: ["första", "andra"],
      margin_percent: -20,
      unit: "mm",
      allowed_value: 10,
      calculated_value: 12,
      calculation: "a + b",
      summary: "Kontrollerar hela evalueringen.",
      title: "Signaturkontroll",
      status: "WARNING" as const,
      rule_version: "1.2.3",
      rule_id: "SIG-001",
    } satisfies RuleEvaluation;

    expect(validationEvaluationSignature(reordered)).toBe(validationEvaluationSignature(signatureEvaluation));
    expect(validationEvaluationSignature({
      ...signatureEvaluation,
      assumptions: [...signatureEvaluation.assumptions].reverse(),
    })).not.toBe(validationEvaluationSignature(signatureEvaluation));
  });

  it.each([
    ["rule_id", "SIG-002"],
    ["rule_version", "2.0.0"],
    ["status", "BLOCK"],
    ["title", "Ny titel"],
    ["summary", "Ny sammanfattning"],
    ["calculation", "a - b"],
    ["calculated_value", 11],
    ["allowed_value", 9],
    ["unit", "cm"],
    ["margin_percent", -10],
    ["assumptions", ["annat"]],
    ["affected_part_ids", ["top"]],
    ["diagnostics", [{ label: "annat", value: "9" }]],
    ["suggestion", { ...signatureEvaluation.suggestion!, label: "Ny åtgärd" }],
  ] as const)("invalidates when %s changes", (field, value) => {
    expect(validationEvaluationSignature({
      ...signatureEvaluation,
      [field]: value,
    })).not.toBe(validationEvaluationSignature(signatureEvaluation));
  });
});

describe("workspace validation ghost transaction", () => {
  beforeEach(() => {
    apiMock.calls.mockReset();
    transactionMock.preview.mockReset();
    evaluationMock.current = undefined;
    fixMock.mode = "geometry";
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/?mode=check");
    writeWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID, {
      spec: sourceSpec,
      templateId: "wall-library",
      workspaceSelected: true,
      uiState: {
        schemaVersion: 2,
        mode: "check",
        viewMode: "perspective",
        exploded: false,
        transparent: false,
        isolateSelection: false,
        panels: {
          componentLibraryOpen: true,
          contextPanelOpen: true,
          advancedPanelOpen: false,
        },
      },
    });
  });

  it("previews normalization without mutation, history or API calls, then cancels and invalidates on stage change", async () => {
    const viewer = await renderWorkspace();
    const undo = screen.getByRole("button", { name: "Ångra" });
    const trigger = await screen.findByRole("button", { name: /Förhandsgranska: Räkna om konstruktionen/i });
    expect(undo).toBeDisabled();

    fireEvent.click(trigger);
    const preview = await screen.findByRole("region", { name: "Kontrollera ändringen före tillämpning" });
    expect(transactionMock.preview).toHaveBeenCalledTimes(1);
    expect(within(preview).getByText("divider_count")).toBeVisible();
    expect(within(preview).getByText("0", { selector: "code" })).toBeVisible();
    expect(within(preview).getByText("4", { selector: "code" })).toBeVisible();
    expect(viewer).toHaveAttribute("data-current-width", "1200");
    expect(viewer).toHaveAttribute("data-comparison-active", "true");
    expect(viewer).toHaveAttribute("data-proposed-width", "4200");
    expect(viewer).toHaveAttribute("data-proposed-divider-count", "4");
    expect(undo).toBeDisabled();
    expect(apiMock.calls).not.toHaveBeenCalled();

    fireEvent.click(within(preview).getByRole("button", { name: "Avbryt" }));
    expect(trigger).toHaveFocus();
    expect(viewer).toHaveAttribute("data-comparison-active", "false");
    expect(screen.queryByRole("region", { name: "Kontrollera ändringen före tillämpning" })).not.toBeInTheDocument();

    fireEvent.click(trigger);
    expect(viewer).toHaveAttribute("data-comparison-active", "true");
    fireEvent.click(screen.getByRole("button", { name: /^Studio/ }));
    await waitFor(() => expect(viewer).toHaveAttribute("data-comparison-active", "false"));
    expect(screen.queryByRole("region", { name: "Kontrollera ändringen före tillämpning" })).not.toBeInTheDocument();
    expect(apiMock.calls).not.toHaveBeenCalled();
  });

  it("commits the exact stored normalized spec once and creates exactly one undo step", async () => {
    const viewer = await renderWorkspace();
    const undo = screen.getByRole("button", { name: "Ångra" });
    fireEvent.click(await screen.findByRole("button", { name: /Förhandsgranska: Räkna om konstruktionen/i }));
    const confirm = await screen.findByRole("button", { name: "Bekräfta och tillämpa" });

    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await waitFor(() => expect(viewer).toHaveAttribute("data-current-width", "4200"));
    expect(viewer).toHaveAttribute("data-current-divider-count", "4");
    expect(viewer).toHaveAttribute("data-comparison-active", "false");
    expect(transactionMock.preview).toHaveBeenCalledTimes(1);
    expect(undo).toBeEnabled();

    fireEvent.click(undo);
    await waitFor(() => expect(viewer).toHaveAttribute("data-current-width", "1200"));
    expect(viewer).toHaveAttribute("data-current-divider-count", "0");
    expect(undo).toBeDisabled();
  });

  it("invalidates an active preview when any evaluation evidence changes", async () => {
    const viewer = await renderWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: /Förhandsgranska:/i }));
    expect(viewer).toHaveAttribute("data-comparison-active", "true");
    expect(evaluationMock.current).toBeDefined();

    Object.assign(evaluationMock.current!, {
      status: "BLOCK",
      title: "Ny identitet",
      assumptions: ["nytt antagande"],
      affected_part_ids: ["side-left"],
    });
    fireEvent.click(screen.getByRole("button", { name: "Transparent" }));

    await waitFor(() => expect(viewer).toHaveAttribute("data-comparison-active", "false"));
    expect(screen.queryByRole("region", { name: "Kontrollera ändringen före tillämpning" })).not.toBeInTheDocument();
  });

  it("commits intent-only bay sizing exactly once even when geometry and local design hash stay unchanged", async () => {
    fixMock.mode = "intent";
    const viewer = await renderWorkspace();
    const undo = screen.getByRole("button", { name: "Ångra" });
    expect(viewer).toHaveAttribute("data-bay-sizing-mode", "count");
    expect(viewer).toHaveAttribute("data-target-bay-width", "300");

    fireEvent.click(await screen.findByRole("button", { name: /Förhandsgranska: Byt indelningsavsikt/i }));
    const preview = await screen.findByRole("region", { name: "Kontrollera ändringen före tillämpning" });
    expect(preview).toHaveTextContent("Ingen geometriförändring");
    const confirm = within(preview).getByRole("button", { name: "Bekräfta och tillämpa" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await waitFor(() => expect(viewer).toHaveAttribute("data-bay-sizing-mode", "target_width"));
    expect(viewer).toHaveAttribute("data-target-bay-width", "800");
    expect(viewer).toHaveAttribute("data-current-width", "1200");
    expect(viewer).toHaveAttribute("data-current-divider-count", "0");
    expect(screen.getByText(/Den lokala förhandsvisningen har nu tillämpats i utkastet/)).toBeVisible();
    expect(undo).toBeEnabled();

    fireEvent.click(undo);
    await waitFor(() => expect(viewer).toHaveAttribute("data-bay-sizing-mode", "count"));
    expect(viewer).toHaveAttribute("data-target-bay-width", "300");
    expect(undo).toBeDisabled();
  });

  it("does not claim success or create history when a confirmed preview is a normalized no-op", async () => {
    fixMock.mode = "noop";
    const viewer = await renderWorkspace();
    const undo = screen.getByRole("button", { name: "Ångra" });
    fireEvent.click(await screen.findByRole("button", { name: /Förhandsgranska: Behåll nuvarande avsikt/i }));
    const preview = await screen.findByRole("region", { name: "Kontrollera ändringen före tillämpning" });
    fireEvent.click(within(preview).getByRole("button", { name: "Bekräfta och tillämpa" }));

    expect(await screen.findByText("Ingen ändring tillämpades")).toBeVisible();
    expect(screen.queryByText(/Den lokala förhandsvisningen har nu tillämpats i utkastet/)).not.toBeInTheDocument();
    expect(viewer).toHaveAttribute("data-bay-sizing-mode", "count");
    expect(viewer).toHaveAttribute("data-target-bay-width", "300");
    expect(undo).toBeDisabled();
  });
});
