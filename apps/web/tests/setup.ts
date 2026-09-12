import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import { TestWebLocks } from "./web-locks";

beforeEach(() => {
  if (typeof navigator !== "undefined") {
    Object.defineProperty(navigator, "locks", { configurable: true, value: new TestWebLocks() });
  }
});
afterEach(() => cleanup());
