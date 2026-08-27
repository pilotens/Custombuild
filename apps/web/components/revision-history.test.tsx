import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import type { DesignVersionRead } from "@/lib/api-client";
import { RevisionHistory } from "./revision-history";

function revision(
  number: number,
  status: DesignVersionRead["status"],
  overrides: Partial<DesignVersionRead> = {},
): DesignVersionRead {
  return {
    id: `version-${number}`,
    project_id: "project-1",
    revision: number,
    status,
    immutable: false,
    design_hash: String(number).repeat(64),
    context_hash: "c".repeat(64),
    engine_version: "1.4.0",
    template_version: "2.0.0",
    template_id: "wall-library",
    template_capability_fingerprint: "f".repeat(64),
    rule_version: "1.3.0",
    spec_json: {},
    source_provenance_json: {},
    source_import_id: null,
    result_json: {},
    created_at: `2026-08-${String(number).padStart(2, "0")}T08:00:00Z`,
    ...overrides,
  };
}

describe("RevisionHistory", () => {
  it("preserves the API receiver when loading versions", async () => {
    const receiverMarker = Symbol("receiver");
    const api = {
      configured: true,
      receiverMarker,
      async listVersions(this: { receiverMarker: symbol }, requestedProjectId: string) {
        if (this.receiverMarker !== receiverMarker) throw new Error("API receiver was lost");
        if (requestedProjectId !== "project-1") throw new Error("Unexpected project");
        return [revision(1, "draft")];
      },
    };

    render(
      <RevisionHistory
        active
        api={api}
        projectId="project-1"
        localDesignHash={"1".repeat(64)}
      />,
    );

    expect(await screen.findByRole("list", { name: "Serverrevisioner" })).toBeVisible();
  });

  it("refreshes after a new revision and after validation changes the same revision", async () => {
    const responses = [
      [revision(1, "draft")],
      [revision(2, "draft")],
      [revision(2, "design_validated")],
    ];
    const listVersions = vi.fn(async () => responses.shift() ?? []);
    const api = { configured: true, listVersions };
    const view = render(
      <RevisionHistory
        active
        api={api}
        projectId="project-1"
        localDesignHash={"1".repeat(64)}
        currentRevision={1}
        revisionRefreshKey="version-1:draft:0"
      />,
    );

    expect(await screen.findByText("Revision R1")).toBeVisible();

    view.rerender(
      <RevisionHistory
        active
        api={api}
        projectId="project-1"
        localDesignHash={"2".repeat(64)}
        currentRevision={2}
        revisionRefreshKey="version-2:draft:0"
      />,
    );
    expect(await screen.findByText("Revision R2")).toBeVisible();
    expect(screen.getByText("Revision R2").closest("li")).toHaveTextContent("Serverutkast");

    view.rerender(
      <RevisionHistory
        active
        api={api}
        projectId="project-1"
        localDesignHash={"2".repeat(64)}
        currentRevision={2}
        revisionRefreshKey="version-2:design_validated:0"
      />,
    );
    await waitFor(() => {
      expect(screen.getByText("Revision R2").closest("li")).toHaveTextContent("Designvaliderad");
    });
    expect(listVersions).toHaveBeenCalledTimes(3);
  });

  it("shows real server revisions, local matching and fail-closed physical status", async () => {
    const versions = [
      revision(3, "design_validated", { immutable: true, design_hash: "a".repeat(64) }),
      revision(2, "draft", { design_hash: "b".repeat(64) }),
    ];
    const listVersions = vi.fn(async () => [...versions].reverse());

    render(
      <RevisionHistory
        active
        api={{ configured: true, listVersions }}
        projectId="project-1"
        localDesignHash={"a".repeat(64)}
        currentRevision={3}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Laddar versionshistorik");
    const list = await screen.findByRole("list", { name: "Serverrevisioner" });
    expect(listVersions).toHaveBeenCalledWith("project-1");
    expect(within(list).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      expect.stringContaining("Revision R3"),
      expect.stringContaining("Revision R2"),
    ]);

    const current = within(list).getByText("Revision R3").closest("li");
    expect(current).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("status", { name: "Lokal modell och serverrevision" })).toHaveTextContent(
      "Matchar serverrevision R3",
    );
    expect(current).toHaveTextContent("Designvaliderad");
    expect(current).toHaveTextContent("Oföränderlig designrevision");
    expect(current).toHaveTextContent("Ej frisläppt för fysisk kapning");
    expect(screen.getByText("Revision R2").closest("li")).toHaveTextContent("Serverutkast");
    expect(screen.getByText("Revision R2").closest("li")).toHaveTextContent("Ändringsbar serverrevision");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();

    const details = within(current!).getByText("Tekniska detaljer").closest("details");
    expect(details).not.toHaveAttribute("open");
    expect(within(details!).getByText("design_validated")).toBeInTheDocument();
    expect(within(details!).getByText("a".repeat(64))).toBeInTheDocument();
    expect(within(details!).getByText("wall-library@2.0.0")).toBeInTheDocument();
  });

  it("keeps an unmatched local model distinct from the server history", async () => {
    render(
      <RevisionHistory
        active
        api={{ configured: true, listVersions: vi.fn(async () => [revision(1, "draft")]) }}
        projectId="project-1"
        localDesignHash={"z".repeat(64)}
        currentRevision={1}
      />,
    );

    expect(await screen.findByRole("status", { name: "Lokal modell och serverrevision" })).toHaveTextContent(
      "Saknar motsvarande serverrevision",
    );
  });

  it("renders an empty server history without suggesting an action", async () => {
    render(
      <RevisionHistory
        active
        api={{ configured: true, listVersions: vi.fn(async () => []) }}
        projectId="project-1"
        localDesignHash={"a".repeat(64)}
      />,
    );

    expect(await screen.findByRole("status")).toHaveTextContent("Inga serverrevisioner finns ännu");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("fails gracefully when the read-only request fails", async () => {
    render(
      <RevisionHistory
        active
        api={{ configured: true, listVersions: vi.fn(async () => { throw new Error("offline"); }) }}
        projectId="project-1"
        localDesignHash={"a".repeat(64)}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Versionshistoriken kunde inte hämtas");
    expect(screen.getByRole("alert")).toHaveTextContent("Hämtningen påverkar inte din arbetsmodell");
    expect(within(screen.getByRole("alert")).getByRole("button", { name: "Försök igen" })).toBeEnabled();
  });

  it("retries the same project once, announces loading and blocks duplicate concurrent retries", async () => {
    let attempt = 0;
    let resolveRetry!: (versions: DesignVersionRead[]) => void;
    const retryRequest = new Promise<DesignVersionRead[]>((resolve) => { resolveRetry = resolve; });
    const listVersions = vi.fn((requestedProjectId: string): Promise<DesignVersionRead[]> => {
      if (requestedProjectId !== "project-1") {
        return Promise.reject(new Error(`unexpected project: ${requestedProjectId}`));
      }
      attempt += 1;
      return attempt === 1 ? Promise.reject(new Error("offline")) : retryRequest;
    });

    render(
      <RevisionHistory
        active
        api={{ configured: true, listVersions }}
        projectId="project-1"
        localDesignHash={"a".repeat(64)}
      />,
    );

    const error = await screen.findByRole("alert");
    fireEvent.click(within(error).getByRole("button", { name: "Försök igen" }));

    const loading = screen.getByRole("status");
    expect(loading).toHaveTextContent("Laddar versionshistorik");
    const blockedRetry = within(loading).getByRole("button", { name: "Försök igen" });
    expect(blockedRetry).toBeDisabled();
    fireEvent.click(blockedRetry);
    await waitFor(() => expect(listVersions).toHaveBeenCalledTimes(2));
    expect(listVersions.mock.calls).toEqual([["project-1"], ["project-1"]]);

    await act(async () => {
      resolveRetry([revision(1, "draft")]);
      await retryRequest;
    });
    expect(await screen.findByRole("list", { name: "Serverrevisioner" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Försök igen" })).not.toBeInTheDocument();
  });

  it("deduplicates the automatic request under React strict effects", async () => {
    let resolveRequest!: (versions: DesignVersionRead[]) => void;
    const request = new Promise<DesignVersionRead[]>((resolve) => { resolveRequest = resolve; });
    const listVersions = vi.fn(() => request);

    render(
      <StrictMode>
        <RevisionHistory
          active
          api={{ configured: true, listVersions }}
          projectId="project-1"
          localDesignHash={"a".repeat(64)}
        />
      </StrictMode>,
    );

    await waitFor(() => expect(listVersions).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("status")).toHaveTextContent("Laddar versionshistorik");
    await act(async () => {
      resolveRequest([]);
      await request;
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Inga serverrevisioner finns ännu");
  });

  it.each([
    {
      name: "offline",
      props: { api: { configured: false, listVersions: vi.fn() }, projectId: "project-1" },
      message: "inte tillgänglig utan serveranslutning",
    },
    {
      name: "without a project",
      props: { api: { configured: true, listVersions: vi.fn() }, projectId: undefined },
      message: "Inget serverprojekt är valt",
    },
    {
      name: "for a concept model",
      props: {
        api: { configured: true, listVersions: vi.fn() },
        projectId: "project-1",
        unavailableReason: "concept" as const,
      },
      message: "Konceptmodeller har ingen serverbaserad versionshistorik",
    },
  ])("does not fetch $name", async ({ props, message }) => {
    render(
      <RevisionHistory
        active
        localDesignHash={"a".repeat(64)}
        {...props}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(message);
    await waitFor(() => expect(props.api.listVersions).not.toHaveBeenCalled());
  });
});
