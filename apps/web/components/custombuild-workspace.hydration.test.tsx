import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({
  configured: true,
  drafts: new Map<string, Record<string, unknown> | null>(),
  draftWaits: new Map<string, Promise<void>>(),
  draftErrors: new Map<string, { status?: number; transportFailure?: boolean }>(),
  projects: [] as Array<{ id: string; name: string; archived: boolean }>,
  createProject: vi.fn(),
  updates: vi.fn(),
}));

vi.mock("next/dynamic", () => ({
  default: () => function MockFurnitureViewer() {
    return <div data-testid="furniture-viewer">Mockad möbelmodell</div>;
  },
}));

vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  class MockCustombuildApiClient {
    readonly baseUrl = "https://api.example.test";
    get configured() { return apiMock.configured; }
    get authenticated() { return apiMock.configured; }
    async getCurrentPrincipal() {
      return {
        user_id: "user-1",
        organization_id: "org-1",
        role: "owner",
        name: "Anders Nilsson",
        email: "anders@example.test",
      };
    }
    async listProjects() {
      return apiMock.projects;
    }
    async createProject(name: string) {
      return apiMock.createProject(name);
    }
    async getProjectDraft(projectId: string) {
      await apiMock.draftWaits.get(projectId);
      const failure = apiMock.draftErrors.get(projectId);
      if (failure) {
        throw new actual.ApiError(
          "Mockat utkastfel",
          failure.status,
          failure.transportFailure ? "API_TRANSPORT_FAILURE" : "INVALID_SERVER_RESPONSE",
          undefined,
          Boolean(failure.transportFailure),
        );
      }
      const draft = apiMock.drafts.get(projectId);
      if (!draft || draft.project_id !== projectId) {
        throw new actual.ApiError(
          "Serverutkastet tillhör inte det uttryckligen begärda projektet.",
          200,
          "INVALID_SERVER_DRAFT",
        );
      }
      return draft;
    }
    async updateProjectDraft(projectId: string, templateId: string, spec: unknown, revision: number) {
      apiMock.updates(projectId, templateId, spec, revision);
      const draft = apiMock.drafts.get(projectId);
      if (!draft) throw new Error("Missing mocked draft");
      const saved = { ...draft, draft_revision: revision + 1 };
      apiMock.drafts.set(projectId, saved);
      return saved;
    }
    previewDesign() { return new Promise<never>(() => undefined); }
    autofixDesign() { return new Promise<never>(() => undefined); }
  }
  return { ...actual, CustombuildApiClient: MockCustombuildApiClient };
});

import * as designEngine from "@/lib/design-engine";
import { CustombuildWorkspace } from "./custombuild-workspace";
import { ApiError, toPreviewRequest } from "@/lib/api-client";
import { DEFAULT_DESIGN_SPEC } from "@/lib/design-types";
import {
  readWorkspaceDraft,
  workspaceDraftKey,
  writeSelectedProject,
  writeWorkspaceDraft,
} from "@/lib/workspace-draft-storage";
import { workspaceIntentEnvelopeFromSpec } from "@/lib/workspace-design-envelope";

const principal = { organization_id: "org-1", user_id: "user-1" };

describe("fail-closed project hydration", () => {
  beforeEach(() => {
    apiMock.drafts.clear();
    apiMock.draftWaits.clear();
    apiMock.draftErrors.clear();
    apiMock.configured = true;
    apiMock.projects = [
      { id: "project-good", name: "Giltigt projekt", archived: false },
      { id: "project-corrupt", name: "Korrupt projekt", archived: false },
    ];
    apiMock.createProject.mockReset();
    apiMock.updates.mockReset();
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/?project=project-good&mode=studio");

    const goodSpec = { ...DEFAULT_DESIGN_SPEC, design_id: "project-good", revision: 1 };
    apiMock.drafts.set("project-good", {
      project_id: "project-good",
      draft_revision: 1,
      template_id: "shelving",
      design_hash: "a".repeat(64),
      spec_json: toPreviewRequest(goodSpec),
      workspace_spec_json: workspaceIntentEnvelopeFromSpec(goodSpec),
      result_json: {},
      updated_at: "2026-08-15T00:00:00Z",
    });
    apiMock.drafts.set("project-corrupt", {
      project_id: "project-corrupt",
      draft_revision: 3,
      template_id: "wall-library",
      design_hash: "b".repeat(64),
      spec_json: {
        ...toPreviewRequest(DEFAULT_DESIGN_SPEC),
        width_mm: "6000",
      },
      workspace_spec_json: workspaceIntentEnvelopeFromSpec(DEFAULT_DESIGN_SPEC),
      result_json: {},
      updated_at: "2026-08-15T00:00:00Z",
    });
    writeWorkspaceDraft(window.localStorage, principal, "project-good", {
      spec: goodSpec,
      templateId: "shelving",
      workspaceSelected: true,
      uiState: {
        schemaVersion: 2,
        mode: "studio",
        viewMode: "front",
        exploded: false,
        transparent: false,
        isolateSelection: false,
        selectedPartId: "shelf-1-bay-1",
        panels: {
          componentLibraryOpen: true,
          contextPanelOpen: true,
          advancedPanelOpen: false,
        },
      },
    });
  });

  it("hides the previous model, blocks autosave and still permits switching away", async () => {
    const resolveSpy = vi.spyOn(designEngine, "resolveDesign");
    render(<CustombuildWorkspace />);

    const projectSelect = await screen.findByRole("combobox", { name: "Aktivt projekt" });
    await waitFor(() => expect(projectSelect).toHaveValue("project-good"));
    expect(await screen.findByTestId("furniture-viewer")).toBeInTheDocument();
    expect(await screen.findByText("ID: shelf-1-bay-1")).toBeInTheDocument();
    const resolveCountBeforeCorruptHydration = resolveSpy.mock.calls.length;

    fireEvent.change(projectSelect, { target: { value: "project-corrupt" } });
    const blocker = await screen.findByTestId("server-draft-hydration-blocker");
    expect(blocker).toHaveTextContent("INVALID_SERVER_DRAFT");
    expect(projectSelect).toBeEnabled();
    expect(projectSelect).toHaveValue("project-corrupt");
    expect(screen.queryByTestId("furniture-viewer")).not.toBeInTheDocument();
    expect(screen.queryByText("ID: shelf-1-bay-1")).not.toBeInTheDocument();
    expect(screen.queryByText("Välj en startpunkt")).not.toBeInTheDocument();
    expect(resolveSpy).toHaveBeenCalledTimes(resolveCountBeforeCorruptHydration);
    expect(apiMock.updates).not.toHaveBeenCalledWith(
      "project-corrupt",
      expect.anything(),
      expect.anything(),
      expect.anything(),
    );

    apiMock.updates.mockClear();
    fireEvent.change(projectSelect, { target: { value: "project-good" } });
    await waitFor(() => expect(projectSelect).toHaveValue("project-good"));
    expect(await screen.findByTestId("furniture-viewer")).toBeInTheDocument();
    expect(apiMock.updates).not.toHaveBeenCalled();
    resolveSpy.mockRestore();
  });

  it("never falls back to a local model for HTTP, null or wrong-project responses", async () => {
    for (const project of [
      { id: "project-http", name: "HTTP-fel", archived: false },
      { id: "project-null", name: "Null-svar", archived: false },
      { id: "project-mismatch", name: "Fel projekt", archived: false },
    ]) apiMock.projects.push(project);
    apiMock.draftErrors.set("project-http", { status: 403 });
    apiMock.drafts.set("project-null", null);
    apiMock.drafts.set("project-mismatch", {
      ...(apiMock.drafts.get("project-good") ?? {}),
      project_id: "project-good",
    });

    for (const projectId of ["project-http", "project-null", "project-mismatch"]) {
      writeWorkspaceDraft(window.localStorage, principal, projectId, {
        spec: { ...DEFAULT_DESIGN_SPEC, design_id: projectId },
        templateId: "shelving",
        workspaceSelected: true,
      });
    }
    window.history.replaceState({}, "", "/?project=project-http&mode=studio");
    render(<CustombuildWorkspace />);

    const projectSelect = await screen.findByRole("combobox", { name: "Aktivt projekt" });
    expect(await screen.findByTestId("server-draft-hydration-blocker")).toBeInTheDocument();
    expect(screen.queryByTestId("furniture-viewer")).not.toBeInTheDocument();
    for (const projectId of ["project-null", "project-mismatch"]) {
      fireEvent.change(projectSelect, { target: { value: projectId } });
      await waitFor(() => expect(projectSelect).toHaveValue(projectId));
      expect(await screen.findByTestId("server-draft-hydration-blocker")).toBeInTheDocument();
      expect(screen.queryByTestId("furniture-viewer")).not.toBeInTheDocument();
    }
  });

  it("uses a valid scoped local draft only for an explicitly marked transport failure", async () => {
    apiMock.projects.push({ id: "project-offline", name: "Offline", archived: false });
    apiMock.draftErrors.set("project-offline", { transportFailure: true });
    writeWorkspaceDraft(window.localStorage, principal, "project-offline", {
      spec: { ...DEFAULT_DESIGN_SPEC, design_id: "project-offline" },
      templateId: "shelving",
      workspaceSelected: true,
    });
    window.history.replaceState({}, "", "/?project=project-offline&mode=studio");

    render(<CustombuildWorkspace />);
    expect(await screen.findByTestId("furniture-viewer")).toBeInTheDocument();
    expect(screen.queryByTestId("server-draft-hydration-blocker")).not.toBeInTheDocument();
  });

  it("blocks new-product click and submit until project hydration has completed", async () => {
    let releaseInitialDraft!: () => void;
    apiMock.draftWaits.set("project-good", new Promise<void>((resolve) => {
      releaseInitialDraft = resolve;
    }));

    render(<CustombuildWorkspace />);

    const createButton = await screen.findByRole("button", { name: "Skapa ny produkt" });
    expect(createButton).toBeDisabled();
    fireEvent.click(createButton);
    expect(screen.queryByLabelText("Nytt projekt")).not.toBeInTheDocument();
    expect(apiMock.createProject).not.toHaveBeenCalled();
    expect(apiMock.updates).not.toHaveBeenCalled();

    releaseInitialDraft();
    expect(await screen.findByTestId("furniture-viewer")).toBeInTheDocument();
    expect(createButton).toBeEnabled();

    fireEvent.click(createButton);
    const nameInput = screen.getByLabelText("Nytt projekt");
    fireEvent.change(nameInput, { target: { value: "Får inte skapas än" } });

    let releaseBlockedDraft!: () => void;
    apiMock.draftWaits.set("project-corrupt", new Promise<void>((resolve) => {
      releaseBlockedDraft = resolve;
    }));
    fireEvent.change(screen.getByRole("combobox", { name: "Aktivt projekt" }), {
      target: { value: "project-corrupt" },
    });
    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Aktivt projekt" })).toHaveValue("project-corrupt");
    });
    apiMock.updates.mockClear();

    expect(nameInput).toBeDisabled();
    expect(screen.getByRole("button", { name: "Skapa" })).toBeDisabled();
    fireEvent.submit(nameInput.closest("form")!);
    expect(apiMock.createProject).not.toHaveBeenCalled();
    expect(apiMock.updates).not.toHaveBeenCalled();

    releaseBlockedDraft();
    expect(await screen.findByTestId("server-draft-hydration-blocker")).toBeInTheDocument();
    expect(createButton).toBeEnabled();
    expect(screen.getByRole("button", { name: "Skapa" })).toBeEnabled();
  });

  it("opens a remembered project at Explore for a bare root URL without showing its model", async () => {
    writeSelectedProject(window.localStorage, principal, {
      id: "project-good",
      name: "Giltigt projekt",
    });
    window.history.replaceState({}, "", "/");

    render(<CustombuildWorkspace />);

    const projectSelect = await screen.findByRole("combobox", { name: "Aktivt projekt" });
    await waitFor(() => expect(projectSelect).toHaveValue("project-good"));
    expect(await screen.findByRole("heading", { name: "Vad vill du skapa?" })).toBeInTheDocument();
    expect(screen.queryByTestId("furniture-viewer")).not.toBeInTheDocument();
    expect(window.location.search).toBe("?project=project-good&mode=explore");
    expect(screen.getByLabelText("Befintligt produktutkast")).toHaveTextContent(
      "Välj Ny produkt ovan för att behålla modellen",
    );
  });

  it("preserves an explicit anonymous empty Studio deep link", async () => {
    apiMock.configured = false;
    window.history.replaceState({}, "", "/?mode=studio");

    render(<CustombuildWorkspace />);

    expect(await screen.findByTestId("furniture-viewer")).toBeInTheDocument();
    expect(window.location.search).toBe("?mode=studio");
    expect(screen.getByRole("button", { name: /Studio/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Underlag/ })).toBeEnabled();
  });

  it("creates a separate blank product from Control and preserves the previous project draft", async () => {
    window.history.replaceState({}, "", "/?project=project-good&mode=check");
    apiMock.createProject.mockImplementationOnce(async (name: string) => {
      const project = { id: "project-new", name, archived: false };
      apiMock.drafts.set(project.id, {
        project_id: project.id,
        draft_revision: 0,
        template_id: null,
        design_hash: null,
        spec_json: null,
        workspace_spec_json: null,
        result_json: null,
        updated_at: "2026-08-15T00:00:00Z",
      });
      return project;
    });

    render(<CustombuildWorkspace />);
    expect(await screen.findByTestId("furniture-viewer")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Skapa ny produkt" }));
    fireEvent.change(screen.getByLabelText("Nytt projekt"), {
      target: { value: "Separat produkt" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Skapa" }));

    const projectSelect = screen.getByRole("combobox", { name: "Aktivt projekt" });
    await waitFor(() => expect(projectSelect).toHaveValue("project-new"));
    expect(await screen.findByRole("heading", { name: "Vad vill du skapa?" })).toBeInTheDocument();
    expect(screen.queryByTestId("furniture-viewer")).not.toBeInTheDocument();
    expect(window.location.search).toBe("?project=project-new&mode=explore");

    await waitFor(() => {
      const freshDraft = readWorkspaceDraft(window.localStorage, principal, "project-new");
      expect(freshDraft?.workspaceSelected).toBe(false);
      expect(freshDraft?.spec.design_id).toBe("project-new");
    });
    expect(readWorkspaceDraft(window.localStorage, principal, "project-good")?.spec.design_id)
      .toBe("project-good");

    for (const name of [/^Studio/, /^Kontroll/, /^Underlag/]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    const underlayButton = screen.getByRole("button", { name: /^Underlag/ });
    underlayButton.removeAttribute("disabled");
    fireEvent.click(underlayButton);
    expect(screen.getByRole("heading", { name: "Vad vill du skapa?" })).toBeInTheDocument();
    expect(screen.queryByTestId("furniture-viewer")).not.toBeInTheDocument();
    expect(window.location.search).toBe("?project=project-new&mode=explore");

    fireEvent.click(screen.getByRole("button", { name: /Börja tomt/ }));
    expect(await screen.findByTestId("furniture-viewer")).toBeInTheDocument();
    await waitFor(() => {
      expect(apiMock.updates.mock.calls.some(([projectId]) => projectId === "project-new")).toBe(true);
    }, { timeout: 2_000 });
    for (const [updatedProjectId, , updatedSpec] of apiMock.updates.mock.calls) {
      expect((updatedSpec as { design_id?: string }).design_id).toBe(updatedProjectId);
    }
  });

  it("requires another name for project conflicts while preserving generic errors", async () => {
    apiMock.createProject.mockRejectedValueOnce(new ApiError("Namnkonflikt", 409));
    render(<CustombuildWorkspace />);

    const createButton = await screen.findByRole("button", { name: "Skapa ny produkt" });
    await waitFor(() => expect(createButton).toBeEnabled());
    fireEvent.click(createButton);
    fireEvent.change(screen.getByLabelText("Nytt projekt"), {
      target: { value: "Arkiverat projekt" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Skapa" }));

    const conflictAlert = await screen.findByRole("alert");
    expect(conflictAlert).toHaveTextContent(
      "Det finns redan ett projekt med det namnet. Ange ett annat namn.",
    );
    expect(conflictAlert).not.toHaveTextContent("Välj det i listan");

    apiMock.createProject.mockRejectedValueOnce(new Error("Servern svarade inte."));
    fireEvent.click(screen.getByRole("button", { name: "Skapa" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Servern svarade inte.");
  });

  it("preserves quarantined v3 raw across valid server hydration and autosave", async () => {
    const key = workspaceDraftKey(principal, "project-good");
    const raw = '{"version":3,"spec":{"width_mm":"unsafe"}}';
    window.localStorage.setItem(key, raw);

    render(<CustombuildWorkspace />);
    expect(await screen.findByTestId("furniture-viewer")).toBeInTheDocument();
    await new Promise((resolve) => window.setTimeout(resolve, 550));
    expect(window.localStorage.getItem(key)).toBe(raw);
  });

  it("preserves a failed monolithic legacy migration for explicit recovery", async () => {
    apiMock.configured = false;
    const raw = '{"schema_version":"1.0","width_mm":"unsafe"}';
    window.localStorage.setItem("custombuild:bookcase:demo", raw);
    window.localStorage.setItem("custombuild:furniture-template:demo", "wall-library");

    render(<CustombuildWorkspace />);
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(window.localStorage.getItem("custombuild:bookcase:demo")).toBe(raw);
    expect(window.localStorage.getItem("custombuild:furniture-template:demo")).toBe("wall-library");
  });
});
