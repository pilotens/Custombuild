import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "./status-badge";

describe("StatusBadge", () => {
  it("renders the Swedish blocking label", () => {
    render(<StatusBadge status="BLOCK" />);
    expect(screen.getByTestId("status-badge")).toHaveTextContent("Blockerar");
    expect(screen.getByTestId("status-badge")).toHaveClass("status-block");
  });
});
