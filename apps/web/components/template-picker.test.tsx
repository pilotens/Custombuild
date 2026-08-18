import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TemplatePicker } from "./template-picker";

function renderPicker(overrides: Partial<React.ComponentProps<typeof TemplatePicker>> = {}) {
  const props: React.ComponentProps<typeof TemplatePicker> = {
    open: true,
    selectedId: "shelving",
    onSelect: vi.fn(),
    onUploadImage: vi.fn(),
    onClose: vi.fn(),
    ...overrides,
  };
  return { ...render(<TemplatePicker {...props} />), props };
}

describe("TemplatePicker Explore entry", () => {
  it("starts with four direct routes instead of a required wizard", () => {
    renderPicker({ required: true });

    expect(screen.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible();
    expect(screen.getByRole("button", { name: /Välj en design/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Skapa med Custombuild/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Utgå från en bild/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Börja tomt/ })).toBeVisible();
    expect(screen.queryByRole("navigation", { name: "Planeringssteg" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Fortsätt" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stäng Explore" })).not.toBeInTheDocument();
  });

  it("shows three real, truthfully labelled templates and opens the chosen geometry", () => {
    const onSelect = vi.fn();
    const onBriefChange = vi.fn();
    renderPicker({ onSelect, onBriefChange });

    fireEvent.click(screen.getByRole("button", { name: /Välj en design/ }));
    expect(screen.getByRole("heading", { name: "Välj en startmodell att forma vidare." })).toBeVisible();

    const cards = screen.getAllByRole("button").filter((button) => button.classList.contains("template-card"));
    expect(cards).toHaveLength(3);
    expect(screen.getAllByText("Inspirationsbild – exakt modell visas i Studio")).toHaveLength(3);
    expect(screen.getAllByText("Konstruktionsscreenad startmodell")).toHaveLength(2);
    expect(screen.getAllByText("Koncept · fortsatt kontroll krävs")).toHaveLength(1);

    const libraryCard = screen.getByRole("button", { name: /Väggbibliotek/ });
    fireEvent.click(libraryCard);
    expect(libraryCard).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Öppna Väggbibliotek i Studio" }));

    expect(onBriefChange).toHaveBeenLastCalledWith(expect.objectContaining({ startMode: "template", selectedTemplateId: "wall-library" }));
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: "wall-library", patch: expect.objectContaining({ furniture_type: "wall_library" }) }),
      expect.objectContaining({ startMode: "template", selectedTemplateId: "wall-library" }),
    );
  });

  it("keeps the exact model preview as a safe fallback if an inspiration asset fails", () => {
    renderPicker();
    fireEvent.click(screen.getByRole("button", { name: /Välj en design/ }));

    fireEvent.error(screen.getByRole("img", { name: "Inspirationsbild för Hyllsystem" }));
    expect(screen.getByRole("img", { name: "Kontrollerad förhandsvisning av Hyllsystem" })).toHaveAttribute("data-visual-errors", "0");
  });

  it("maps a lightweight need description to a structured brief and three existing suggestions", () => {
    const onSelect = vi.fn();
    const onBriefChange = vi.fn();
    renderPicker({ onSelect, onBriefChange });

    fireEvent.click(screen.getByRole("button", { name: /Skapa med Custombuild/ }));
    expect(screen.getByRole("heading", { name: "Vad ska möbeln göra för dig?" })).toBeVisible();
    expect(screen.getByText(/matchas lokalt mot strukturerade behov/i)).toBeVisible();

    fireEvent.change(screen.getByLabelText(/^Beskriv ditt behov/), {
      target: { value: "Ett väggbibliotek med mest böcker och dold förvaring. Maximal kapacitet och naturligt trä." },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Planerad bredd" }), { target: { value: "3600" } });
    fireEvent.click(screen.getByRole("button", { name: "Visa tre startförslag" }));

    expect(screen.getByText("Matchade startpunkter")).toBeVisible();
    expect(screen.getAllByRole("button").filter((button) => /^(01|02|03)/.test(button.textContent ?? ""))).toHaveLength(3);
    expect(screen.getByText("Öppet och dolt · Maximal förvaring")).toBeVisible();
    expect(onBriefChange).toHaveBeenLastCalledWith(expect.objectContaining({
      width_mm: 3600,
      space: "wall",
      primaryUse: "mixed",
      priority: "capacity",
      style: "natural",
    }));

    fireEvent.click(screen.getByRole("button", { name: "Öppna vald modell i Studio" }));
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: "wall-library", patch: expect.objectContaining({ width_mm: 3600, load_per_shelf_kg: 40 }) }),
      expect.objectContaining({ primaryUse: "mixed", priority: "capacity" }),
    );
  });

  it("validates need-mode dimensions without blocking the other start routes", () => {
    const onUploadImage = vi.fn();
    renderPicker({ onUploadImage });
    fireEvent.click(screen.getByRole("button", { name: /Skapa med Custombuild/ }));

    const width = screen.getByRole("spinbutton", { name: "Planerad bredd" });
    fireEvent.change(width, { target: { value: "" } });
    expect(width).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Ange ett mått i millimeter.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Visa tre startförslag" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Till start" }));
    fireEvent.click(screen.getByRole("button", { name: /Utgå från en bild/ }));
    expect(onUploadImage).toHaveBeenCalledWith(expect.objectContaining({ startMode: "reference" }));
  });

  it("exposes the complete canonical B1 dimension envelope", () => {
    const onBriefChange = vi.fn();
    renderPicker({ onBriefChange });
    fireEvent.click(screen.getByRole("button", { name: /Skapa med Custombuild/ }));

    const width = screen.getByRole("spinbutton", { name: "Planerad bredd" });
    const height = screen.getByRole("spinbutton", { name: "Planerad höjd" });
    const depth = screen.getByRole("spinbutton", { name: "Planerad djup" });
    expect(width).toHaveAttribute("min", "250");
    expect(width).toHaveAttribute("max", "6000");
    expect(height).toHaveAttribute("min", "300");
    expect(height).toHaveAttribute("max", "4000");
    expect(depth).toHaveAttribute("min", "100");
    expect(depth).toHaveAttribute("max", "1200");

    fireEvent.change(width, { target: { value: "250" } });
    fireEvent.change(height, { target: { value: "300" } });
    fireEvent.change(depth, { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Visa tre startförslag" }));
    expect(onBriefChange).toHaveBeenLastCalledWith(expect.objectContaining({
      width_mm: 250,
      height_mm: 300,
      depth_mm: 100,
    }));
  });

  it("shows a clear no-op when a confirmed height cannot fit a wall-library base", () => {
    const onSelect = vi.fn();
    renderPicker({ onSelect });
    fireEvent.click(screen.getByRole("button", { name: /Skapa med Custombuild/ }));
    fireEvent.change(screen.getByLabelText(/^Beskriv ditt behov/), {
      target: { value: "En vägg med dold förvaring" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Planerad höjd" }), {
      target: { value: "300" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Visa tre startförslag" }));
    fireEvent.click(screen.getByRole("button", { name: "Öppna vald modell i Studio" }));

    expect(screen.getByRole("alert")).toHaveTextContent(/kräver mer totalhöjd/);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("opens an empty frame immediately from Börja tomt", () => {
    const onSelect = vi.fn();
    renderPicker({ onSelect });

    fireEvent.click(screen.getByRole("button", { name: /Börja tomt/ }));
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "shelving",
        name: "Egen stomme",
        patch: expect.objectContaining({
          shelf_count: 0,
          divider_count: 0,
          bay_sizing_mode: "count",
          bay_width_ratios: [],
          shelf_height_ratios: [],
        }),
      }),
      expect.objectContaining({ startMode: "scratch", selectedTemplateId: "shelving" }),
    );
  });

  it("renders embedded Explore as a regular section without modal behavior", () => {
    const { container } = renderPicker({ presentation: "embedded", required: true });
    const section = container.querySelector("section[data-presentation='embedded']");

    expect(section).toBeInTheDocument();
    expect(section).not.toHaveAttribute("role", "dialog");
    expect(container.querySelector("[data-modal-root='true']")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible();
  });

  it("keeps a closable modal and the existing modal-root contract", () => {
    const onClose = vi.fn();
    const { container } = renderPicker({ onClose });

    expect(screen.getByRole("dialog", { name: "Vad vill du skapa?" })).toBeVisible();
    expect(container.querySelector(".cb-planner-backdrop")).toHaveAttribute("data-modal-root", "true");
    fireEvent.click(screen.getByRole("button", { name: "Stäng Explore" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("renders nothing while closed", () => {
    const { container } = renderPicker({ open: false });
    expect(container).toBeEmptyDOMElement();
  });
});
