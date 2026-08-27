import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DraftConflictBanner } from "./draft-conflict-banner";

describe("DraftConflictBanner", () => {
  it("preserves an explicit copy path and reloads only when the user asks", () => {
    const onCopy = vi.fn();
    const onReload = vi.fn();
    render(
      <DraftConflictBanner
        message="Servern har revision 2."
        busy={false}
        copied={false}
        onCopy={onCopy}
        onReload={onReload}
      />,
    );

    expect(screen.getByText(/lokala ändringar finns kvar/i)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Kopiera lokalt utkast/i }));
    expect(onCopy).toHaveBeenCalledOnce();
    expect(onReload).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Hämta senaste utkast/i }));
    expect(onReload).toHaveBeenCalledOnce();
  });

  it("prevents duplicate recovery actions while the latest revision is loading", () => {
    render(
      <DraftConflictBanner
        message="Konflikt."
        busy
        copied
        onCopy={vi.fn()}
        onReload={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: /Kopierat/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Hämtar/i })).toBeDisabled();
  });
});
