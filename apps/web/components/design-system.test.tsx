import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ComponentLibrary,
  ConfirmDialog,
  DimensionHandle,
  DimensionInput,
  Drawer,
  EmptyState,
  ErrorState,
  ExportCard,
  LoadingState,
  Modal,
  PersistentModelCanvas,
  PropertyInspector,
  RevisionBadge,
  SegmentedControl,
  SelectableCard,
  StatusRow,
  Tooltip,
  ValidationItem,
} from "./design-system";

describe("design system primitives", () => {
  it("exposes semantic regions for the persistent editor layout", () => {
    render(
      <>
        <PersistentModelCanvas label="Möbelmodell" toolbar={<button type="button">Framifrån</button>} status="Sparad">
          <canvas title="3D-modell" />
        </PersistentModelCanvas>
        <ComponentLibrary title="Byggdelar" description="Lägg till delar"><button type="button">Hyllplan</button></ComponentLibrary>
        <PropertyInspector title="Vald del" footer={<button type="button">Klar</button>}><label>Material<input /></label></PropertyInspector>
      </>,
    );

    expect(screen.getByRole("region", { name: "Möbelmodell" })).toBeVisible();
    expect(screen.getByRole("complementary", { name: "Byggdelar" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Vald del" })).toBeVisible();
  });

  it("previews dimensions, clamps commits and supports keyboard nudging", () => {
    const onPreview = vi.fn();
    const onCommit = vi.fn();
    const onNudge = vi.fn();
    render(
      <>
        <DimensionInput label="Bredd" value={1200} min={300} max={2400} step={10} hint="Yttermått" onPreview={onPreview} onCommit={onCommit} />
        <DimensionHandle label="Ändra bredd" axis="x" keyboardStep={10} onNudge={onNudge} />
      </>,
    );

    const input = screen.getByRole("spinbutton", { name: "Bredd" });
    fireEvent.change(input, { target: { value: "2413" } });
    expect(onPreview).toHaveBeenLastCalledWith(2413);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onCommit).toHaveBeenLastCalledWith(2400);

    const handle = screen.getByRole("button", { name: "Ändra bredd" });
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    fireEvent.keyDown(handle, { key: "ArrowUp" });
    expect(onNudge).toHaveBeenNthCalledWith(1, -10);
    expect(onNudge).toHaveBeenNthCalledWith(2, 10);
  });

  it("uses native choices and explicit selected-card state", () => {
    const onModeChange = vi.fn();
    render(
      <>
        <SegmentedControl
          label="Arbetssätt"
          value="guided"
          options={[{ id: "guided", label: "Guidat" }, { id: "free", label: "Fritt" }]}
          onChange={onModeChange}
        />
        <SelectableCard selected title="Väggbibliotek" description="Öppet och dolt" />
      </>,
    );

    fireEvent.click(screen.getByRole("radio", { name: "Fritt" }));
    expect(onModeChange).toHaveBeenCalledWith("free");
    expect(screen.getByRole("button", { name: /Väggbibliotek/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("never relies on color alone for status and validation", () => {
    render(
      <>
        <StatusRow status="decision" title="Beslag behöver väljas" description="Välj ett verkligt beslag." />
        <ValidationItem status="blocked" title="För lång spännvidd" summary="Lägg till ett stöd." details="Gräns 800 mm" defaultOpen />
        <RevisionBadge revision="R3" status="draft" />
      </>,
    );

    expect(screen.getByText("Beslag behöver väljas").closest(".cb-status-row")).toHaveAttribute("data-status", "decision");
    expect(screen.getByText("Måste lösas")).toBeVisible();
    expect(screen.getByText("Version R3")).toBeVisible();
    expect(screen.getByText("Utkast")).toBeVisible();
  });

  it("provides semantic empty, loading, error and export states", () => {
    render(
      <>
        <EmptyState title="Inga projekt" action={<button type="button">Skapa projekt</button>} />
        <LoadingState title="Modellen laddas" />
        <ErrorState title="Kunde inte ladda" action={<button type="button">Försök igen</button>} />
        <ExportCard title="Tillverkningsunderlag" format="ZIP" status="ready" action={<button type="button">Ladda ned</button>} />
      </>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Modellen laddas");
    expect(screen.getByRole("alert")).toHaveTextContent("Kunde inte ladda");
    expect(screen.getByRole("article", { name: "Tillverkningsunderlag" })).toHaveTextContent("Klar att hämta");
  });

  it("labels tooltips and dismisses modal surfaces with Escape", () => {
    const onClose = vi.fn();
    render(
      <>
        <Tooltip content="Ändra modellens bredd"><button type="button">Bredd</button></Tooltip>
        <Modal open title="Material" description="Välj yta" onClose={onClose}><button type="button">Björk</button></Modal>
      </>,
    );

    const trigger = screen.getByRole("button", { name: "Bredd", hidden: true });
    const tooltip = screen.getByRole("tooltip", { hidden: true });
    expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);
    expect(screen.getByRole("dialog", { name: "Material" })).toHaveAttribute("aria-modal", "true");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("distinguishes drawers and destructive confirmation dialogs", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    const { rerender } = render(<Drawer open title="Egenskaper" onClose={onClose}><p>Vald del</p></Drawer>);
    expect(screen.getByRole("dialog", { name: "Egenskaper" }).className).toContain("cb-drawer--right");

    rerender(
      <ConfirmDialog open title="Ta bort delen?" tone="danger" confirmLabel="Ta bort" onConfirm={onConfirm} onClose={onClose} />,
    );
    expect(screen.getByRole("alertdialog", { name: "Ta bort delen?" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Ta bort" }));
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});
