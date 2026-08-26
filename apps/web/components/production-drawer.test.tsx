import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resolveDesign } from "@/lib/design-engine";
import { DEFAULT_DESIGN_SPEC } from "@/lib/design-types";
import { furnitureTemplate } from "@/lib/furniture-templates";
import { ProductionDrawer } from "./production-drawer";

afterEach(() => vi.unstubAllEnvs());

describe("ProductionDrawer", () => {
  it("blocks concept templates with a concrete explanation", () => {
    const template = furnitureTemplate("hanging-shelf");
    const spec = { ...DEFAULT_DESIGN_SPEC, ...template.patch };
    render(
      <ProductionDrawer
        open
        spec={spec}
        design={resolveDesign(spec)}
        template={template}
        onClose={vi.fn()}
        onOpenTemplates={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Den här mallen är fortfarande en konceptmodell" })).toBeVisible();
    expect(screen.getByText(/Väggbeslag, infästningspunkter/)).toBeVisible();
    expect(screen.getByText(/inget designgranskningspaket skapas/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Spara revision" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Skapa underlag/ })).not.toBeInTheDocument();
  });

  it("exposes the screened production workflow and can close", () => {
    const onClose = vi.fn();
    const template = furnitureTemplate("shelving");
    const spec = { ...DEFAULT_DESIGN_SPEC, ...template.patch };
    render(
      <ProductionDrawer
        open
        spec={spec}
        design={resolveDesign(spec)}
        template={template}
        onClose={onClose}
        onOpenTemplates={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { level: 2, name: "Skapa underlag" })).toBeVisible();
    expect(screen.getByLabelText("Underlagets innehåll")).toHaveAttribute("tabindex", "0");
    expect(screen.getByText("Underlag")).toBeVisible();
    expect(document.querySelector(".production-drawer-backdrop")).toHaveAttribute("data-modal-root", "true");
    expect(screen.queryByRole("navigation", { name: /Projektets sex steg/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Redo för kontroll/)).toBeVisible();
    expect(screen.getByText(/innan ett designgranskningspaket skapas/i)).toBeVisible();
    fireEvent.click(
      screen.getByRole("dialog", { name: "Skapa underlag" })
        .querySelector<HTMLButtonElement>(".production-drawer-close")!,
    );
    expect(onClose).toHaveBeenCalledOnce();

    onClose.mockClear();
    const scrim = document.querySelector<HTMLButtonElement>(".production-drawer-scrim");
    expect(scrim).not.toBeNull();
    fireEvent.click(scrim!);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("keeps the wall library as a previewable concept without revision controls", () => {
    const template = furnitureTemplate("wall-library");
    const spec = { ...DEFAULT_DESIGN_SPEC, ...template.patch };
    render(
      <ProductionDrawer
        open
        spec={spec}
        design={resolveDesign(spec)}
        template={template}
        onClose={vi.fn()}
        onOpenTemplates={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Den här mallen är fortfarande en konceptmodell" })).toBeVisible();
    expect(screen.getByText(/gångjärn, beslag, borrbilder, frontspel/)).toBeVisible();
    expect(screen.getByText(/inget designgranskningspaket skapas/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Spara.*revision/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Skapa underlag/i })).not.toBeInTheDocument();
  });

  it("cannot promote wall-library geometry by pairing it with a screened template id", () => {
    const screenedTemplate = furnitureTemplate("shelving");
    const wallLibrary = furnitureTemplate("wall-library");
    const spec = { ...DEFAULT_DESIGN_SPEC, ...wallLibrary.patch };
    render(
      <ProductionDrawer
        open
        spec={spec}
        design={resolveDesign(spec)}
        template={screenedTemplate}
        onClose={vi.fn()}
        onOpenTemplates={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Den här mallen är fortfarande en konceptmodell" })).toBeVisible();
    expect(screen.getByText(/Väggbibliotekets gångjärn, beslag, borrbilder/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Skapa underlag/i })).not.toBeInTheDocument();
  });

  it("renders the same real workflow inline without modal chrome", () => {
    const template = furnitureTemplate("shelving");
    const spec = { ...DEFAULT_DESIGN_SPEC, ...template.patch };
    render(
      <ProductionDrawer
        open
        presentation="embedded"
        spec={spec}
        design={resolveDesign(spec)}
        template={template}
        onClose={vi.fn()}
        onOpenTemplates={vi.fn()}
      />,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Skapa underlag" })).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.querySelector(".production-drawer-backdrop")).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: /Projektets sex steg/ })).not.toBeInTheDocument();
    expect(screen.getByText("Underlag · Aktuell konstruktion")).toBeVisible();
    expect(screen.getByRole("heading", { level: 3, name: "Versionshistorik" })).toBeVisible();
  });

  it("blocks reference-image geometry even when based on the screened shelf template", () => {
    const template = furnitureTemplate("shelving");
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      ...template.patch,
      reference_image_import: {
        source: "reference_image" as const,
        import_id: "11111111-1111-4111-8111-111111111111",
        image_sha256: "a".repeat(64),
        file_name: "bokhylla.jpg",
        image_width_px: 1200,
        image_height_px: 800,
        confidence: 0.82,
        detected_shelves: 5,
        detected_dividers: 3,
        detected_base_cabinets: false,
        warnings: [],
      },
    };
    render(
      <ProductionDrawer
        open
        spec={spec}
        design={resolveDesign(spec)}
        template={template}
        onClose={vi.fn()}
        onOpenTemplates={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Referensbilden måste konstruktionsgranskas" })).toBeVisible();
    expect(screen.getByText(/Bilden visar inte säkra verkliga mått/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Spara revision" })).not.toBeInTheDocument();
  });

  it("blocks production after an individual board has been changed", () => {
    const template = furnitureTemplate("shelving");
    const spec = {
      ...DEFAULT_DESIGN_SPEC,
      ...template.patch,
      part_overrides: { "side-left": { width_mm: 2_000 } },
    };
    render(
      <ProductionDrawer
        open
        spec={spec}
        design={resolveDesign(spec)}
        template={template}
        onClose={vi.fn()}
        onOpenTemplates={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Deländringarna måste konstruktionsgranskas" })).toBeVisible();
    expect(screen.getByText(/förband, upplag och bärighet kan räknas om på servern/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Spara revision" })).not.toBeInTheDocument();
  });
});
