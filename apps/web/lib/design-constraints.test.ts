import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import rawContract from "../../../packages/contracts/design-constraints.v1.json";
import {
  DESIGN_CONSTRAINTS,
  maximumBaseCabinetHeightMm,
  parseDesignConstraintsContract,
} from "./design-constraints";

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => `${JSON.stringify(key)}:${canonicalJson(child)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

describe("version-bound design constraints", () => {
  it("publishes the active envelope while keeping physical cutting unauthorized", () => {
    expect(DESIGN_CONSTRAINTS).toMatchObject({
      schemaVersion: "custombuild.design-constraints.v1",
      contractVersion: "1.0.0",
      fingerprint: "b529bca5e98a0c434ba34c545173b0e793c4d53015340997de64053e039ea789",
      physicalCuttingAuthorized: false,
      widthMm: { minimum: 250, maximum: 6000 },
      heightMm: { minimum: 300, maximum: 4000 },
      depthMm: { minimum: 100, maximum: 1200 },
      shelfCount: { minimum: 0, maximum: 40 },
      dividerCount: { minimum: 0, maximum: 16 },
      bayCount: { minimum: 1, maximum: 17 },
      shelfLoadKg: { minimum: 0, maximum: 500 },
      baseCabinetModuleCount: { minimum: 0, maximum: 17 },
      baseCabinetHeightMm: { minimum: 0, maximum: 2000 },
      baseCabinetDepthMm: { minimum: 0, maximum: 1200 },
      minimumShelfWidthMm: 40,
      minimumShelfOpeningMm: 40,
      minimumBaseCabinetOpeningMm: 200,
      wallLibraryBaseHeightMinimumMm: 300,
      baseCabinetUpperClearanceMm: 200,
    });
  });

  it("cryptographically binds the complete canonical source document at build verification", () => {
    const unsignedContract: Record<string, unknown> = { ...rawContract };
    delete unsignedContract.fingerprint;
    const digest = createHash("sha256").update(canonicalJson(unsignedContract), "utf8").digest("hex");

    expect(digest).toBe(rawContract.fingerprint.value);
    expect(digest).toBe(DESIGN_CONSTRAINTS.fingerprint);
  });

  it("fails closed for an unknown version, fingerprint, or physical-cutting authorization", () => {
    expect(() =>
      parseDesignConstraintsContract({ ...rawContract, contract_version: "2.0.0" }),
    ).toThrow(/Unsupported/);
    expect(() =>
      parseDesignConstraintsContract({
        ...rawContract,
        fingerprint: { ...rawContract.fingerprint, value: "0".repeat(64) },
      }),
    ).toThrow(/fingerprint/);
    expect(() =>
      parseDesignConstraintsContract({
        ...rawContract,
        safety: { ...rawContract.safety, physical_cutting_authorized: true },
      }),
    ).toThrow(/physical cutting/);
  });

  it("fails closed when a required range is absent", () => {
    const { width_mm: omitted, ...incompleteEnvelope } = rawContract.envelope;
    expect(omitted).toBeDefined();
    expect(() =>
      parseDesignConstraintsContract({ ...rawContract, envelope: incompleteEnvelope }),
    ).toThrow(/width_mm/);
  });

  it("returns an integer maximum that preserves the strict upper-clearance invariant", () => {
    expect(maximumBaseCabinetHeightMm(2400, 17.8)).toBe(2000);
    const constrainedMaximum = maximumBaseCabinetHeightMm(1000, 17.8);
    expect(constrainedMaximum).toBe(782);
    expect(constrainedMaximum).toBeLessThan(1000 - 17.8 - 200);
  });
});
