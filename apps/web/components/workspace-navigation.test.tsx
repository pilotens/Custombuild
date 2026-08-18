import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WorkspaceNavigation, WORKSPACE_MODES, WORKSPACE_STAGES } from "./workspace-navigation";

describe("WorkspaceNavigation", () => {
  it("presents four product modes without wizard numbering", () => {
    const onStageChange = vi.fn();
    const { container } = render(
      <WorkspaceNavigation current="studio" onStageChange={onStageChange} />,
    );

    expect(WORKSPACE_MODES.map((mode) => mode.label)).toEqual([
      "Utforska", "Studio", "Kontroll", "Underlag",
    ]);
    expect(WORKSPACE_STAGES).toBe(WORKSPACE_MODES);
    expect(screen.getByRole("navigation", { name: "Produktlägen" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Studio/ })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: /Underlag/ })).toHaveTextContent("Designgranska och exportera");
    expect(container.querySelector("ol")).not.toBeInTheDocument();
    expect(container.querySelector(".cb-workspace-step-number")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Kontroll/ }));
    expect(onStageChange).toHaveBeenCalledWith("check");
  });

  it("maps a legacy current step while the workspace integration migrates", () => {
    render(<WorkspaceNavigation current="verification" onStageChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Kontroll/ })).toHaveAttribute("aria-current", "page");
  });

  it("keeps an empty authenticated project in Explore until a start point is selected", () => {
    const onStageChange = vi.fn();
    render(
      <WorkspaceNavigation
        current="explore"
        startPointSelected={false}
        onStageChange={onStageChange}
      />,
    );

    expect(screen.getByRole("button", { name: /Utforska/ })).toBeEnabled();
    for (const name of [/Studio/, /Kontroll/, /Underlag/]) {
      const button = screen.getByRole("button", { name });
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute("title", "Välj en startpunkt i Utforska först.");
      fireEvent.click(button);
    }
    expect(onStageChange).not.toHaveBeenCalled();
  });

  it("keeps old props source-compatible without rendering guided/free controls", () => {
    const onModeChange = vi.fn();
    render(
      <WorkspaceNavigation
        current="composition"
        mode="free"
        onStageChange={vi.fn()}
        onModeChange={onModeChange}
        componentLibraryOpen
        contextPanelOpen={false}
        onComponentLibraryOpenChange={vi.fn()}
        onContextPanelOpenChange={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Guidat" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Fritt" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delar" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Egenskaper" })).not.toBeInTheDocument();
    expect(onModeChange).not.toHaveBeenCalled();
  });
});
