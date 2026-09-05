import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkshopContextDraftState } from "./workshop-context-editor";

const apiMock = vi.hoisted(() => ({
  drafts: new Map<string, Record<string, unknown>>(),
  getDraft: vi.fn(),
  updateDraft: vi.fn(),
}));

vi.mock("next/dynamic", () => ({
  default: () => function MockFurnitureViewer() {
    return <div data-testid="furniture-viewer" />;
  },
}));

vi.mock("./production-drawer", () => ({
  ProductionDrawer(props: {
    workshopContextDraftState?: WorkshopContextDraftState;
    onWorkshopContextDraftStateChange?: (state: WorkshopContextDraftState) => void;
  }) {
    const partialDraft: WorkshopContextDraftState = {
      enabled: true,
      dirty: true,
      valid: false,
      validationMessage: "Profilversion eller batch måste anges.",
      sourceValueSignature: "undefined",
      sourceBindingSignature: "test-binding",
      pendingValueSignature: undefined,
      draft: {
        profiles: [{
          role: "carcass",
          supplierProfileId: "partial-profile",
          supplierProfileVersion: "",
          sheetWidthMm: "2440",
          sheetHeightMm: "1220",
          sheetCount: "4",
          trimMarginMm: "",
          kerfMm: "",
          grainDirection: "",
          allowRotation: "",
          defectZones: [],
          keepOutZones: [],
        }],
        registrations: [],
      },
    };
    return (
      <div data-testid="production-drawer">
        <output aria-label="Rått leverantörs-ID">
          {props.workshopContextDraftState?.draft.profiles[0]?.supplierProfileId ?? "inget-utkast"}
        </output>
        <button
          type="button"
          onClick={() => props.onWorkshopContextDraftStateChange?.(partialDraft)}
        >
          Starta partiell verkstadsprofil
        </button>
      </div>
    );
  },
}));

vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  class MockCustombuildApiClient {
    readonly baseUrl = "https://api.example.test";
    readonly configured = true;
    readonly authenticated = true;

    async getCurrentPrincipal() {
      return {
        user_id: "designer-1",
        organization_id: "org-1",
        role: "designer",
        name: "Designer",
        email: "designer@example.test",
      };
    }

    async listProjects() {
      return [
        { id: "project-one", name: "Projekt ett", archived: false },
        { id: "project-two", name: "Projekt två", archived: false },
      ];
    }

    async getProjectDraft(projectId: string) {
      apiMock.getDraft(projectId);
      const draft = apiMock.drafts.get(projectId);
      if (!draft) throw new Error("Mockutkast saknas");
      return draft;
    }

    async updateProjectDraft(projectId: string, templateId: string, spec: unknown, revision: number) {
      apiMock.updateDraft(projectId, templateId, spec, revision);
      const draft = apiMock.drafts.get(projectId);
      if (!draft) throw new Error("Mockutkast saknas");
      const saved = { ...draft, draft_revision: revision + 1 };
      apiMock.drafts.set(projectId, saved);
      return saved;
    }

    previewDesign() { return new Promise<never>(() => undefined); }
    autofixDesign() { return new Promise<never>(() => undefined); }
  }
  return { ...actual, CustombuildApiClient: MockCustombuildApiClient };
});

import { toPreviewRequest } from "@/lib/api-client";
import { DEFAULT_DESIGN_SPEC } from "@/lib/design-types";
import { workspaceIntentEnvelopeFromSpec } from "@/lib/workspace-design-envelope";
import { CustombuildWorkspace } from "./custombuild-workspace";

function serverDraft(projectId: string) {
  const spec = { ...DEFAULT_DESIGN_SPEC, design_id: projectId, revision: 1 };
  return {
    project_id: projectId,
    draft_revision: 1,
    template_id: "shelving",
    design_hash: "a".repeat(64),
    spec_json: toPreviewRequest(spec),
    workspace_spec_json: workspaceIntentEnvelopeFromSpec(spec),
    result_json: {},
    updated_at: "2026-09-03T08:00:00Z",
  };
}

describe("workspace workshop draft boundary", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    apiMock.drafts.clear();
    apiMock.drafts.set("project-one", serverDraft("project-one"));
    apiMock.drafts.set("project-two", serverDraft("project-two"));
    apiMock.getDraft.mockReset();
    apiMock.updateDraft.mockReset();
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/?project=project-one&mode=build");
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("blocks scene, project and save transitions while retaining the raw partial draft", async () => {
    render(<CustombuildWorkspace />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(screen.getByTestId("production-drawer")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Aktivt projekt" })).toHaveValue("project-one");
    apiMock.updateDraft.mockClear();
    apiMock.getDraft.mockClear();

    fireEvent.click(screen.getByRole("button", {
      name: "Starta partiell verkstadsprofil",
    }));

    expect(screen.getByTestId("workshop-draft-blocker")).toHaveTextContent(
      "Gå till Underlag och slutför alla fält",
    );
    expect(screen.getByLabelText("Rått leverantörs-ID")).toHaveTextContent("partial-profile");
    expect(screen.getByRole("button", { name: "Spara utkast" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Aktivt projekt" })).toBeDisabled();
    expect(screen.getByText("Verkstadsprofilen måste slutföras")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /Studio/ }));
    expect(screen.getByTestId("production-drawer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Underlag/ })).toHaveAttribute("aria-current", "page");
    expect(screen.getByLabelText("Rått leverantörs-ID")).toHaveTextContent("partial-profile");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(apiMock.updateDraft).not.toHaveBeenCalled();
    expect(apiMock.getDraft).not.toHaveBeenCalled();
  });
});
