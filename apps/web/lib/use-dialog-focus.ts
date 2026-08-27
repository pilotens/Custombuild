"use client";

import { useEffect, useRef, type RefObject } from "react";

let bodyScrollLockCount = 0;

function lockBodyScroll(): void {
  bodyScrollLockCount += 1;
  document.body.classList.add("dialog-scroll-locked");
}

function unlockBodyScroll(): void {
  bodyScrollLockCount = Math.max(0, bodyScrollLockCount - 1);
  if (bodyScrollLockCount === 0) document.body.classList.remove("dialog-scroll-locked");
}

const FOCUSABLE = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/** Keeps keyboard focus inside a modal and restores it to the opener on close. */
export function useDialogFocus(
  open: boolean,
  dialogRef: RefObject<HTMLElement | null>,
  onClose: () => void,
  escapeEnabled = true,
): void {
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    lockBodyScroll();

    const modalRoot = dialogRef.current?.closest<HTMLElement>("[data-modal-root='true']") ?? dialogRef.current;
    const background = modalRoot?.parentElement
      ? Array.from(modalRoot.parentElement.children).filter(
          (element): element is HTMLElement => element instanceof HTMLElement && element !== modalRoot,
        )
      : [];
    const backgroundState = background.map((element) => ({
      element,
      inert: element.inert,
      ariaHidden: element.getAttribute("aria-hidden"),
    }));
    for (const element of background) {
      element.inert = true;
      element.setAttribute("aria-hidden", "true");
    }

    const frame = window.requestAnimationFrame(() => {
      const first = dialogRef.current?.querySelector<HTMLElement>(FOCUSABLE);
      (first ?? dialogRef.current)?.focus();
    });

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && escapeEnabled) {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");
      if (focusable.length === 0) {
        event.preventDefault();
        dialogRef.current.focus();
        return;
      }
      const first = focusable[0]!;
      const last = focusable.at(-1)!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleKeyDown);
      unlockBodyScroll();
      for (const state of backgroundState) {
        state.element.inert = state.inert;
        if (state.ariaHidden === null) state.element.removeAttribute("aria-hidden");
        else state.element.setAttribute("aria-hidden", state.ariaHidden);
      }
      previous?.focus();
    };
  }, [dialogRef, escapeEnabled, open]);
}
