import { render, screen } from "@testing-library/react";
import { useRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { useDialogFocus } from "./use-dialog-focus";

function Harness({ open }: Readonly<{ open: boolean }>) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(open, dialogRef, vi.fn());
  return <div ref={dialogRef} tabIndex={-1}><button type="button">Fortsätt</button></div>;
}

describe("useDialogFocus", () => {
  it("locks responsive body scrolling with a class instead of an inline style", () => {
    const { rerender } = render(<Harness open />);

    expect(document.body).toHaveClass("dialog-scroll-locked");
    expect(document.body).not.toHaveAttribute("style");
    expect(screen.getByRole("button", { name: "Fortsätt" })).toBeInTheDocument();

    rerender(<Harness open={false} />);
    expect(document.body).not.toHaveClass("dialog-scroll-locked");
  });

  it("retains the lock until the last concurrent dialog closes", () => {
    const first = render(<Harness open />);
    const second = render(<Harness open />);

    first.unmount();
    expect(document.body).toHaveClass("dialog-scroll-locked");

    second.unmount();
    expect(document.body).not.toHaveClass("dialog-scroll-locked");
  });
});
