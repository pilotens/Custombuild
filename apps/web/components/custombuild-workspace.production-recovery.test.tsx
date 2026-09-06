import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DesignSpec, ResolvedDesign } from "@/lib/design-types";

const apiMock = vi.hoisted(() => ({
  previewDesign: vi.fn(),
  autofixDesign: vi.fn(),
  previewResult: undefined as ResolvedDesign | undefined,
  previewTransportFailures: 0,
  draft: undefined as Record<string, unknown> | undefined,
  principalRole: "owner",
  retentionEvidenceId: undefined as string | undefined,
}));

vi.mock("next/dynamic", () => ({
  default: () => function MockFurnitureViewer() {
    return <div data-testid="furniture-viewer" />;
  },
}));

vi.mock("./production-drawer", () => ({
  ProductionDrawer(props: {
    design: ResolvedDesign;
    onRequestServerPreviewRetry?: () => void;
    projectId?: string;
    apiClient?: { setJointRetentionEvidence(projectId: string, evidenceId?: string): void };
  }) {
    return (
      <div data-testid="production-drawer" data-design-source={props.design.source}>
        <button type="button" onClick={props.onRequestServerPreviewRetry}>
          Hämta om serverpreview
        </button>
        <button type="button" onClick={() => {
          if (props.projectId) {
            props.apiClient?.setJointRetentionEvidence(
              props.projectId,
              "22222222-2222-4222-8222-222222222222",
            );
          }
          props.onRequestServerPreviewRetry?.();
        }}>
          Bind retention och hämta om
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
        user_id: "user-1",
        organization_id: "org-1",
        role: apiMock.principalRole,
        name: "Anders Nilsson",
        email: "anders@example.test",
      };
    }

    async listProjects() {
      return [{ id: "project-retry", name: "Retryprojekt", archived: false }];
    }

    async getProjectDraft() {
      if (!apiMock.draft) throw new Error("Mockutkast saknas");
      return apiMock.draft;
    }

    async updateProjectDraft() {
      if (!apiMock.draft) throw new Error("Mockutkast saknas");
      return apiMock.draft;
    }

    async previewDesign(spec: DesignSpec, signal?: AbortSignal, projectId?: string) {
      apiMock.previewDesign(spec, signal, projectId, apiMock.retentionEvidenceId);
      if (apiMock.previewTransportFailures > 0) {
        apiMock.previewTransportFailures -= 1;
        throw new actual.ApiError(
          "Kunde inte nå konstruktions-API:t. Lokal förhandsvisning är fortfarande aktiv.",
          undefined,
          "API_TRANSPORT_FAILURE",
          undefined,
          true,
        );
      }
      if (!apiMock.previewResult) throw new Error("Mockpreview saknas");
      return apiMock.previewResult;
    }

    async autofixDesign(spec: DesignSpec, signal?: AbortSignal, projectId?: string) {
      apiMock.autofixDesign(spec, signal, projectId);
      return this.previewDesign(spec, signal, projectId);
    }

    setJointRetentionEvidence(_projectId: string, evidenceId?: string) {
      apiMock.retentionEvidenceId = evidenceId;
    }
  }
  return { ...actual, CustombuildApiClient: MockCustombuildApiClient };
});

import { toPreviewRequest } from "@/lib/api-client";
import { resolveDesign } from "@/lib/design-engine";
import { DEFAULT_DESIGN_SPEC } from "@/lib/design-types";
import { workspaceIntentEnvelopeFromSpec } from "@/lib/workspace-design-envelope";
import { CustombuildWorkspace } from "./custombuild-workspace";

const sourceSpec: DesignSpec = {
  ...DEFAULT_DESIGN_SPEC,
  design_id: "project-retry",
  revision: 1,
  reinforcement_mode: "manual",
};

describe("workspace production preview recovery", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  beforeEach(() => {
    apiMock.previewDesign.mockReset();
    apiMock.autofixDesign.mockReset();
    apiMock.previewTransportFailures = 0;
    apiMock.principalRole = "owner";
    apiMock.retentionEvidenceId = undefined;
    apiMock.previewResult = { ...resolveDesign(sourceSpec), source: "server-preview" };
    apiMock.draft = {
      project_id: "project-retry",
      draft_revision: 1,
      template_id: "shelving",
      design_hash: apiMock.previewResult.design_hash,
      spec_json: toPreviewRequest(sourceSpec),
      workspace_spec_json: workspaceIntentEnvelopeFromSpec(sourceSpec),
      result_json: {},
      updated_at: "2026-08-15T00:00:00Z",
    };
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/?project=project-retry&mode=build");
  });

  it("clears the cached preview and issues one retry for an otherwise unchanged spec", async () => {
    vi.useFakeTimers();
    render(<CustombuildWorkspace />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const retry = screen.getByRole("button", { name: "Hämta om serverpreview" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(apiMock.previewDesign).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("production-drawer")).toHaveAttribute(
      "data-design-source",
      "server-preview",
    );
    const firstSpec = apiMock.previewDesign.mock.calls[0]?.[0];

    fireEvent.click(retry);
    expect(screen.getByTestId("production-drawer")).toHaveAttribute("data-design-source", "local");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(apiMock.previewDesign).toHaveBeenCalledTimes(2);
    expect(apiMock.previewDesign.mock.calls[1]?.[0]).toEqual(firstSpec);
    expect(screen.getByTestId("production-drawer")).toHaveAttribute(
      "data-design-source",
      "server-preview",
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(650);
    });
    expect(apiMock.previewDesign).toHaveBeenCalledTimes(2);
  });

  it("recovers automatically after a transient server restart", async () => {
    vi.useFakeTimers();
    apiMock.previewTransportFailures = 1;
    render(<CustombuildWorkspace />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(screen.getByText("Servermodellen är inte tillgänglig.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Försök igen" })).toBeInTheDocument();
    expect(apiMock.previewDesign).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(750);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(apiMock.previewDesign).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("Servermodellen är inte tillgänglig.")).not.toBeInTheDocument();
    expect(screen.getByTestId("production-drawer")).toHaveAttribute(
      "data-design-source",
      "server-preview",
    );
  });

  it("uses read-only preview for an auto-mode reviewer and its retention-bound retry", async () => {
    vi.useFakeTimers();
    apiMock.principalRole = "reviewer";
    const reviewerSpec: DesignSpec = { ...sourceSpec, reinforcement_mode: "auto" };
    apiMock.previewResult = { ...resolveDesign(reviewerSpec), source: "server-preview" };
    apiMock.draft = {
      project_id: "project-retry",
      draft_revision: 1,
      template_id: "shelving",
      design_hash: apiMock.previewResult.design_hash,
      spec_json: toPreviewRequest(reviewerSpec),
      workspace_spec_json: workspaceIntentEnvelopeFromSpec(reviewerSpec),
      result_json: {},
      updated_at: "2026-08-15T00:00:00Z",
    };
    render(<CustombuildWorkspace />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const retentionRetry = screen.getByRole("button", {
      name: "Bind retention och hämta om",
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(apiMock.previewDesign).toHaveBeenCalledTimes(1);
    expect(apiMock.autofixDesign).not.toHaveBeenCalled();

    fireEvent.click(retentionRetry);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });

    expect(apiMock.previewDesign).toHaveBeenCalledTimes(2);
    expect(apiMock.previewDesign.mock.calls[1]?.[3]).toBe(
      "22222222-2222-4222-8222-222222222222",
    );
    expect(apiMock.autofixDesign).not.toHaveBeenCalled();
  });
});
