import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/furniture-production-preview.json";
import { ApiError, CustombuildApiClient, type CurrentPrincipal, type DesignVersionRead } from "@/lib/api-client";
import type { FurnitureProductionPreview } from "@/lib/furniture-workspace";
import { furnitureProductionRecovery, furnitureProductionRecoveryKey } from "@/lib/furniture-production-recovery";
import { DEFAULT_DESIGN_SPEC } from "@/lib/design-types";
import { FurnitureProduction } from "./furniture-production";

const preview = fixture as unknown as FurnitureProductionPreview;
const source = preview.source_furniture;
const projectId = source.workspace.design.design_id;
const principal: CurrentPrincipal = {
  user_id: "fixture-owner", organization_id: "fixture-org", role: "owner", name: "Designer", email: "fixture@example.test",
};
function versionFixture(patch: Partial<DesignVersionRead> = {}): DesignVersionRead {
  return {
    id: "production-version", project_id: projectId, revision: 1, status: "draft",
    design_hash: fixture.preview.design_hash, context_hash: "d".repeat(64),
    spec_json: fixture.preview.spec, result_json: fixture.preview,
    source_provenance_json: {}, source_import_id: null,
    engine_version: fixture.preview.engine_version,
    template_version: `bookcase@${fixture.preview.template_version}`,
    rule_version: "bookcase-rules@1.4.0", template_id: "shelving",
    template_capability_fingerprint: "e".repeat(64), immutable: false,
    created_at: "2026-09-08T10:00:00Z", ...patch,
  };
}
function setup() {
  const api = new CustombuildApiClient("https://api.example.test");
  vi.spyOn(api, "previewFurnitureProduction").mockResolvedValue(preview);
  vi.spyOn(api, "getProductionState").mockResolvedValue({ project_id: projectId,
    version: null, approvals: [], latest_job: null, release: null });
  vi.spyOn(api, "listExternalEvidence").mockResolvedValue([]);
  vi.spyOn(api, "listVersions").mockResolvedValue([]);
  return api;
}
function show(api: CustombuildApiClient, onClose = vi.fn()) {
  return render(<FurnitureProduction api={api} principal={principal} workspace={source.workspace}
    designHash={source.furniture_design_hash} projectName="Mitt hyllsystem" onClose={onClose} />);
}
afterEach(() => { vi.restoreAllMocks(); window.sessionStorage.clear(); window.localStorage.clear(); });

describe("beredning från sparad möbel", () => {
  it("skickar den faktiska servermodellen och ursprungsrevisionen till befintligt produktionsflöde", async () => {
    const api = setup();
    const create = vi.spyOn(api, "createVersion").mockImplementation(async (_id, _spec, hash, _revision, _template, _retention, bound) => versionFixture({
      design_hash: hash,
      result_json: { ...preview.preview, source_furniture: bound }, immutable: false,
    }));
    vi.spyOn(api, "validateVersion").mockRejectedValue(new ApiError("Blocking construction or DFM rules remain", 409));
    show(api);
    const save = await screen.findByRole("button", { name: "Spara och kontrollera" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    const call = create.mock.calls[0]!;
    expect(call[1]).toMatchObject({ measured_thickness_mm: 17.801, width_mm: 700.001,
      height_mm: 1000.003, divider_count: 0, shelf_count: 2, plinth: false,
      machine_profile_id: source.workspace.manufacturing!.machine_profile_id });
    expect(call[2]).toBe(preview.preview.design_hash);
    expect(call[6]).toEqual(source);
    expect(screen.queryByRole("button", { name: "Godkänn CAM" })).not.toBeInTheDocument();
  });

  it("varnar innan en ändrad verkstadsprofil kastas bort", async () => {
    const api = setup(); const close = vi.fn();
    show(api, close);
    await screen.findByRole("button", { name: "Spara och kontrollera" });
    const machines = screen.getAllByRole("radio");
    const larger = machines.find(input => (input as HTMLInputElement).value === "custombuild-router-5125-linuxcnc")!;
    expect(larger).toBeEnabled();
    fireEvent.click(larger);
    fireEvent.click(screen.getByRole("button", { name: "Tillbaka till möbeln" }));
    expect(await screen.findByRole("alertdialog", { name: "Osparad beredning" })).toBeVisible();
    expect(close).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Lämna utan att spara beredningen" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("visar ett serverfel och kan hämta den sparade modellen igen", async () => {
    const api = setup();
    vi.mocked(api.previewFurnitureProduction).mockRejectedValueOnce(new ApiError("Möbelrevisionen har ändrats", 409));
    show(api);
    expect(await screen.findByRole("alert")).toHaveTextContent("Möbelrevisionen har ändrats");
    expect(screen.queryByRole("button", { name: "Spara och kontrollera" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Försök igen" }));
    expect(await screen.findByRole("button", { name: "Spara och kontrollera" })).toBeVisible();
  });

  it("återställer exakt den sparade verkstadsprofilen och varnar inte för oförändrat underlag", async () => {
    const api = setup(); const close = vi.fn();
    vi.mocked(api.getProductionState).mockResolvedValue({ project_id: projectId, approvals: [], latest_job: null, release: null,
      version: versionFixture({ id: "saved-production", revision: 3, status: "design_validated",
        result_json: { ...preview.preview, source_furniture: source, production_context: {
          machine_profile_id: "custombuild-router-5125-linuxcnc", stock_width_mm: 2500,
          stock_height_mm: 1300, stock_count: 3, back_stock_width_mm: 2500,
          back_stock_height_mm: 1300, back_stock_count: 2,
        } }, immutable: false }) });
    show(api, close);
    const larger = await screen.findByRole("radio", { name: /5125/ });
    expect(larger).toBeChecked();
    expect(screen.queryByText(/Modellen har ändrats\. Spara/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Tillbaka till möbeln" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("återanvänder inte en beredning från en annan materialbatch med samma geometri", async () => {
    const api = setup();
    vi.mocked(api.getProductionState).mockResolvedValue({ project_id: projectId, approvals: [], latest_job: null, release: null,
      version: versionFixture({ id: "old-production", revision: 1, status: "design_validated",
        result_json: { ...preview.preview, source_furniture: { ...source, workspace_sha256: "0".repeat(64) }, production_context: {
          machine_profile_id: source.workspace.manufacturing!.machine_profile_id,
          stock_width_mm: 2440, stock_height_mm: 1220, stock_count: 1,
          back_stock_width_mm: 2440, back_stock_height_mm: 1220, back_stock_count: 1,
        } }, immutable: false }) });
    show(api);
    expect(await screen.findByText(/Modellen har ändrats\. Spara/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Spara och kontrollera" })).toBeVisible();
  });

  it("återställer tillämpade verkstadsuppgifter efter omladdning först när källan kontrollerats på nytt", async () => {
    const api = setup();
    const first = show(api);
    fireEvent.click(await screen.findByRole("radio", { name: /5125/ }));
    const key = furnitureProductionRecoveryKey(api.baseUrl, principal, source);
    await waitFor(() => expect(window.localStorage.getItem(key)).toContain("custombuild-router-5125-linuxcnc"));
    const raw = window.localStorage.getItem(key)!;
    expect(JSON.parse(raw)).not.toHaveProperty("approvals");
    first.unmount();
    show(api);
    expect(await screen.findByRole("region", { name: "Lokal beredningskopia" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Spara och kontrollera" })).not.toBeInTheDocument();
    vi.mocked(api.previewFurnitureProduction).mockRejectedValueOnce(new ApiError("Tillfälligt offline", 503));
    fireEvent.click(screen.getByRole("button", { name: "Återställ beredningskopian" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Tillfälligt offline");
    expect(window.localStorage.getItem(key)).toBe(raw);
    let finishVerification!: (value: FurnitureProductionPreview) => void;
    vi.mocked(api.previewFurnitureProduction).mockImplementationOnce(() => new Promise(resolve => { finishVerification = resolve; }));
    fireEvent.click(screen.getByRole("button", { name: "Återställ beredningskopian" }));
    expect(screen.getByRole("button", { name: "Återställ beredningskopian" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Kasta beredningskopian" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Kasta beredningskopian" }));
    expect(window.localStorage.getItem(key)).toBe(raw);
    await act(async () => finishVerification(preview));
    expect(await screen.findByRole("radio", { name: /5125/ })).toBeChecked();
    expect(screen.getByText(/Kopian innehåller inga godkännanden/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Tillbaka till möbeln" }));
    expect(screen.getByRole("alertdialog", { name: "Osparad beredning" })).toBeVisible();
  });

  it("byter inte källa eller användare när en annan kopia finns och återanvänder inte minnesändringar", async () => {
    const api = setup();
    const key = furnitureProductionRecoveryKey(api.baseUrl, principal, source);
    const otherKey = furnitureProductionRecoveryKey(api.baseUrl, { ...principal, user_id: "another-user" }, source);
    const raw = JSON.stringify(furnitureProductionRecovery({ ...DEFAULT_DESIGN_SPEC,
      machine_profile_id: "custombuild-router-5125-linuxcnc" }, source));
    window.localStorage.setItem(otherKey, raw);
    const mounted = show(api);
    const larger = await screen.findByRole("radio", { name: /5125/ });
    expect(larger).not.toBeChecked();
    expect(screen.queryByRole("region", { name: "Lokal beredningskopia" })).not.toBeInTheDocument();
    fireEvent.click(larger);
    await waitFor(() => expect(window.localStorage.getItem(key)).not.toBeNull());
    mounted.rerender(<FurnitureProduction api={api} principal={{ ...principal, organization_id: "another-org" }}
      workspace={source.workspace} designHash={source.furniture_design_hash} projectName="Mitt hyllsystem" onClose={vi.fn()} />);
    expect(await screen.findByRole("radio", { name: /5125/ })).not.toBeChecked();
    expect(window.localStorage.getItem(key)).not.toBeNull();
    expect(window.localStorage.getItem(otherKey)).toBe(raw);
  });

  it("behåller korrupta kopior för nedladdning utan att tillåta produktionsåtgärder", async () => {
    const api = setup();
    const key = furnitureProductionRecoveryKey(api.baseUrl, principal, source);
    window.localStorage.setItem(key, "{trasigt");
    show(api);
    fireEvent.click(await screen.findByRole("button", { name: "Återställ beredningskopian" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(window.localStorage.getItem(key)).toBe("{trasigt");
    expect(screen.getByRole("button", { name: "Ladda ned beredningskopian" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Spara och kontrollera" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Kasta beredningskopian" }));
    expect(await screen.findByRole("button", { name: "Spara och kontrollera" })).toBeVisible();
    expect(window.localStorage.getItem(key)).toBeNull();
  });

  it("visar lagringsfel och fångar intern navigation utan att tappa utkastet", async () => {
    const api = setup();
    show(api);
    const larger = await screen.findByRole("radio", { name: /5125/ });
    const original = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (key.startsWith("custombuild:furniture-production-recovery:")) throw new DOMException("Quota", "QuotaExceededError");
      original.call(this, key, value);
    });
    fireEvent.click(larger);
    expect(await screen.findByText(/Den lokala beredningskopian kunde inte sparas/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Ladda ned beredningsutkast" })).toBeEnabled();
    const link = document.createElement("a"); link.href = "/another-page"; link.textContent = "Annan sida";
    document.body.appendChild(link);
    try {
      expect(fireEvent.click(link)).toBe(false);
      expect(screen.getByRole("alertdialog", { name: "Osparad beredning" })).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "Fortsätt bereda" }));
      expect(larger).toBeChecked();
      const leave = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(leave);
      expect(leave.defaultPrevented).toBe(true);
    } finally { link.remove(); }
  });

  it("skriver inte över eller raderar en annan fliks nyare beredningskopia", async () => {
    const api = setup();
    const mounted = show(api);
    const larger = await screen.findByRole("radio", { name: /5125/ });
    const original = screen.getAllByRole("radio").find(input => (input as HTMLInputElement).checked)!;
    fireEvent.click(larger);
    const key = furnitureProductionRecoveryKey(api.baseUrl, principal, source);
    await waitFor(() => expect(window.localStorage.getItem(key)).not.toBeNull());
    const other = window.localStorage.getItem(key)!.replace('"stock_count":1', '"stock_count":2');
    window.localStorage.setItem(key, other);
    // Returning to the saved baseline must not remove the newer copy.
    fireEvent.click(original);
    expect(await screen.findByText(/En annan flik har ändrat återställningskopian/)).toBeVisible();
    expect(window.localStorage.getItem(key)).toBe(other);
    fireEvent.click(larger);
    await waitFor(() => expect(larger).toBeChecked());
    expect(window.localStorage.getItem(key)).toBe(other);
    mounted.unmount();
    show(api);
    await screen.findByRole("button", { name: "Kasta beredningskopian" });
    const newest = other.replace('"stock_count":2', '"stock_count":3');
    window.localStorage.setItem(key, newest);
    fireEvent.click(screen.getByRole("button", { name: "Kasta beredningskopian" }));
    expect(await screen.findByText(/En annan flik har ändrat återställningskopian/)).toBeVisible();
    expect(window.localStorage.getItem(key)).toBe(newest);
    expect(screen.getByRole("region", { name: "Lokal beredningskopia" })).toBeVisible();
  });
});
