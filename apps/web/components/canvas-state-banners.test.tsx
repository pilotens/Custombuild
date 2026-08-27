import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CanvasStateBanners } from "./canvas-state-banners";

describe("CanvasStateBanners", () => {
  it("renders nothing when the workspace has no server-state message", () => {
    render(
      <CanvasStateBanners
        apiMessage=""
        canRetryApi={false}
        onRetryApi={vi.fn()}
      />,
    );

    expect(screen.queryByTestId("canvas-state-banners")).not.toBeInTheDocument();
  });

  it("keeps independent auth, project and API failures readable and retries only on request", () => {
    const onRetryApi = vi.fn();
    render(
      <CanvasStateBanners
        apiMessage="Förhandsvisningen kunde inte hämtas."
        apiState="error"
        authError="Sessionen har gått ut."
        canRetryApi
        onRetryApi={onRetryApi}
        projectError="Utkastet kunde inte läsas."
      />,
    );

    expect(screen.getAllByRole("alert")).toHaveLength(2);
    expect(screen.getByText(/Sessionen har gått ut/)).toBeVisible();
    expect(screen.getByText(/Utkastet kunde inte läsas/)).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Förhandsvisningen kunde inte hämtas.");

    fireEvent.click(screen.getByRole("button", { name: "Försök igen" }));
    expect(onRetryApi).toHaveBeenCalledOnce();
  });

  it("describes offline review honestly without exposing a meaningless retry", () => {
    render(
      <CanvasStateBanners
        apiMessage="ignored"
        apiState="offline"
        canRetryApi
        onRetryApi={vi.fn()}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Lokalt konstruktionsläge.");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Produktionsfiler och serverauktoritativ geometri är inte tillgängliga.",
    );
    expect(screen.queryByRole("button", { name: "Försök igen" })).not.toBeInTheDocument();
  });
});
