import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ReferencePostEditor, referencePostVisual } from "./reference-posts";

describe("independent reference post", () => {
  it("requires measured coordinates and preserves an odd diameter and a negative offset", () => {
    const updated = vi.fn();
    render(<ReferencePostEditor posts={[]} change={updated} />);
    fireEvent.click(screen.getByText(/Separata uppmätta/));
    fireEvent.click(
      screen.getByRole("button", { name: "Lägg till uppmätt stolpe" }),
    );
    for (const [label, value] of [
      ["Stolpens namn", "Ändstöd"],
      ["Bredd X (mm)", "30,001"],
      ["Djup Y (mm)", "30,001"],
      ["Längd Z (mm)", "1000"],
      ["Placering X (mm)", "0"],
      ["Placering Y (mm)", "-30,001"],
      ["Underkant Z (mm)", "740"],
    ] as const) {
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
    }
    fireEvent.change(screen.getByLabelText("Tvärsnitt"), {
      target: { value: "round" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Spara stolpens mått" }),
    );
    const [post] = updated.mock.calls[0]![0];
    expect(post).toMatchObject({
      key: "ref-post-1",
      height_um: 1_000_000,
      y_um: -30_001,
    });
    expect(referencePostVisual(post)).toMatchObject({
      circular_outline: true,
      position_mm: { x: 15.0005, y: -15.0005, z: 1240 },
    });
  });
});
