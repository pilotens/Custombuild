import rawContract from "../../../packages/contracts/design-constraints.v1.json";

const EXPECTED_SCHEMA_VERSION = "custombuild.design-constraints.v1";
const EXPECTED_CONTRACT_VERSION = "1.0.0";
const EXPECTED_FINGERPRINT =
  "b529bca5e98a0c434ba34c545173b0e793c4d53015340997de64053e039ea789";

type ValueType = "integer" | "number";

export interface DesignConstraintRange {
  readonly minimum: number;
  readonly maximum: number;
  readonly type: ValueType;
}

export interface DesignConstraints {
  readonly schemaVersion: string;
  readonly contractVersion: string;
  readonly fingerprint: string;
  readonly physicalCuttingAuthorized: false;
  readonly widthMm: DesignConstraintRange;
  readonly heightMm: DesignConstraintRange;
  readonly depthMm: DesignConstraintRange;
  readonly shelfCount: DesignConstraintRange;
  readonly dividerCount: DesignConstraintRange;
  readonly bayCount: DesignConstraintRange;
  readonly shelfLoadKg: DesignConstraintRange;
  readonly baseCabinetModuleCount: DesignConstraintRange;
  readonly baseCabinetHeightMm: DesignConstraintRange;
  readonly baseCabinetDepthMm: DesignConstraintRange;
  readonly minimumShelfWidthMm: number;
  readonly minimumShelfOpeningMm: number;
  readonly minimumBaseCabinetOpeningMm: number;
  readonly wallLibraryBaseHeightMinimumMm: number;
  readonly baseCabinetUpperClearanceMm: number;
}

function requireRecord(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`Invalid design constraints contract: ${path} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function requireString(record: Record<string, unknown>, key: string, path: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`Invalid design constraints contract: ${path}.${key} must be a string.`);
  }
  return value;
}

function requireBoolean(record: Record<string, unknown>, key: string, path: string): boolean {
  const value = record[key];
  if (typeof value !== "boolean") {
    throw new Error(`Invalid design constraints contract: ${path}.${key} must be a boolean.`);
  }
  return value;
}

function requireStringArray(record: Record<string, unknown>, key: string, path: string): string[] {
  const value = record[key];
  if (!Array.isArray(value) || !value.every((entry) => typeof entry === "string")) {
    throw new Error(`Invalid design constraints contract: ${path}.${key} must be a string array.`);
  }
  return value;
}

function requireRange(
  container: Record<string, unknown>,
  key: string,
  expectedType: ValueType,
  path = "envelope",
): DesignConstraintRange {
  const range = requireRecord(container[key], `${path}.${key}`);
  const minimum = range.minimum;
  const maximum = range.maximum;
  const type = range.type;
  if (
    typeof minimum !== "number" ||
    !Number.isFinite(minimum) ||
    typeof maximum !== "number" ||
    !Number.isFinite(maximum) ||
    minimum > maximum ||
    type !== expectedType ||
    (expectedType === "integer" && (!Number.isInteger(minimum) || !Number.isInteger(maximum)))
  ) {
    throw new Error(`Invalid design constraints contract: ${path}.${key} has an invalid range.`);
  }
  return Object.freeze({ minimum, maximum, type: expectedType });
}

function requireInvariantExpression(
  invariants: readonly unknown[],
  id: string,
): string {
  const invariant = invariants.find((candidate) => {
    const record = requireRecord(candidate, "dynamic_invariants[]");
    return record.id === id;
  });
  if (invariant === undefined) {
    throw new Error(`Invalid design constraints contract: dynamic invariant ${id} is missing.`);
  }
  return requireString(requireRecord(invariant, `dynamic_invariants.${id}`), "expression", `dynamic_invariants.${id}`);
}

function captureInteger(expression: string, pattern: RegExp, description: string): number {
  const match = pattern.exec(expression);
  if (match === null) {
    throw new Error(`Invalid design constraints contract: ${description} has an unsupported expression.`);
  }
  return Number(match[1]);
}

export function parseDesignConstraintsContract(value: unknown): DesignConstraints {
  const contract = requireRecord(value, "contract");
  const schemaVersion = requireString(contract, "schema_version", "contract");
  const contractVersion = requireString(contract, "contract_version", "contract");
  if (schemaVersion !== EXPECTED_SCHEMA_VERSION || contractVersion !== EXPECTED_CONTRACT_VERSION) {
    throw new Error("Unsupported design constraints contract schema or version.");
  }

  if (requireString(contract, "status", "contract") !== "published") {
    throw new Error("Design constraints contract must be published.");
  }

  const fingerprint = requireRecord(contract.fingerprint, "fingerprint");
  if (
    requireString(fingerprint, "algorithm", "fingerprint") !== "sha256" ||
    requireString(fingerprint, "canonicalization", "fingerprint") !== "UTF-8 JSON with recursively sorted object keys, compact separators, ensure_ascii=false, and the top-level fingerprint member omitted" ||
    requireString(fingerprint, "value", "fingerprint") !== EXPECTED_FINGERPRINT
  ) {
    throw new Error("Design constraints contract fingerprint does not match the version-bound fingerprint.");
  }

  const safety = requireRecord(contract.safety, "safety");
  const physicalCuttingAuthorized = requireBoolean(safety, "physical_cutting_authorized", "safety");
  if (physicalCuttingAuthorized || !requireBoolean(safety, "design_review_required", "safety")) {
    throw new Error("Design constraints contract must remain review-only and unsafe for physical cutting.");
  }

  const envelope = requireRecord(contract.envelope, "envelope");
  const widthMm = requireRange(envelope, "width_mm", "number");
  const heightMm = requireRange(envelope, "height_mm", "number");
  const depthMm = requireRange(envelope, "depth_mm", "number");
  const shelfCount = requireRange(envelope, "shelf_count", "integer");
  const dividerCount = requireRange(envelope, "divider_count", "integer");
  const shelfLoadKg = requireRange(envelope, "load_per_shelf_kg", "number");
  const baseCabinetModuleCount = requireRange(envelope, "base_cabinet_count", "integer");
  const baseCabinetHeightMm = requireRange(envelope, "base_cabinet_height_mm", "number");
  const baseCabinetDepthMm = requireRange(envelope, "base_cabinet_depth_mm", "number");

  const derived = requireRecord(contract.derived_fields, "derived_fields");
  const derivedBayCount = requireRecord(derived.bay_count, "derived.bay_count");
  const bayCount = requireRange(derived, "bay_count", "integer", "derived_fields");
  if (
    requireString(derivedBayCount, "expression", "derived.bay_count") !== "divider_count + 1" ||
    derivedBayCount.minimum !== bayCount.minimum ||
    derivedBayCount.maximum !== bayCount.maximum
  ) {
    throw new Error("Invalid design constraints contract: derived bay count does not match its envelope.");
  }

  if (!Array.isArray(contract.dynamic_invariants)) {
    throw new Error("Invalid design constraints contract: dynamic_invariants must be an array.");
  }
  const minimumShelfWidthMm = captureInteger(
    requireInvariantExpression(contract.dynamic_invariants, "manufacturable-shelf-width"),
    /at least ([0-9]+) mm/,
    "manufacturable shelf-width invariant",
  );
  const minimumShelfOpeningMm = captureInteger(
    requireInvariantExpression(contract.dynamic_invariants, "manufacturable-opening-height"),
    /at least ([0-9]+) mm$/,
    "manufacturable opening invariant",
  );
  const minimumBaseCabinetOpeningMm = captureInteger(
    requireInvariantExpression(contract.dynamic_invariants, "manufacturable-base-opening"),
    /at least ([0-9]+) mm$/,
    "manufacturable base-opening invariant",
  );

  const families = requireRecord(contract.family_invariants, "family_invariants");
  const bookcase = requireStringArray(families, "bookcase", "family_invariants");
  if (
    !bookcase.includes("base_cabinet_count == 0") ||
    !bookcase.includes("base_cabinet_height_mm == 0") ||
    !bookcase.includes("base_cabinet_depth_mm == 0")
  ) {
    throw new Error("Invalid design constraints contract: bookcase base invariants are incomplete.");
  }
  const wallLibrary = requireStringArray(families, "wall_library", "family_invariants");
  const wallLibraryBaseHeightExpression = wallLibrary.find((entry) =>
    entry.startsWith("base_cabinet_height_mm is in ["),
  );
  const wallLibraryUpperClearanceExpression = wallLibrary.find((entry) =>
    entry.startsWith("base_cabinet_height_mm < height_mm"),
  );
  if (
    !wallLibrary.includes("base_cabinet_depth_mm == depth_mm") ||
    wallLibraryBaseHeightExpression === undefined ||
    wallLibraryUpperClearanceExpression === undefined
  ) {
    throw new Error("Invalid design constraints contract: wall-library base invariants are incomplete.");
  }
  const wallLibraryBaseHeightMinimumMm = captureInteger(
    wallLibraryBaseHeightExpression,
    /^base_cabinet_height_mm is in \[([0-9]+), [0-9]+\]$/,
    "wall-library base-height invariant",
  );
  const baseCabinetUpperClearanceMm = captureInteger(
    wallLibraryUpperClearanceExpression,
    /measured_thickness_mm - ([0-9]+)$/,
    "wall-library upper-clearance invariant",
  );

  if (
    minimumShelfWidthMm <= 0 ||
    minimumShelfOpeningMm <= 0 ||
    minimumBaseCabinetOpeningMm <= 0 ||
    wallLibraryBaseHeightMinimumMm < baseCabinetHeightMm.minimum ||
    baseCabinetUpperClearanceMm <= 0
  ) {
    throw new Error("Invalid design constraints contract: derived invariant constants are invalid.");
  }

  return Object.freeze({
    schemaVersion,
    contractVersion,
    fingerprint: EXPECTED_FINGERPRINT,
    physicalCuttingAuthorized: false,
    widthMm,
    heightMm,
    depthMm,
    shelfCount,
    dividerCount,
    bayCount,
    shelfLoadKg,
    baseCabinetModuleCount,
    baseCabinetHeightMm,
    baseCabinetDepthMm,
    minimumShelfWidthMm,
    minimumShelfOpeningMm,
    minimumBaseCabinetOpeningMm,
    wallLibraryBaseHeightMinimumMm,
    baseCabinetUpperClearanceMm,
  });
}

export const DESIGN_CONSTRAINTS = parseDesignConstraintsContract(rawContract);

export function maximumBaseCabinetHeightMm(heightMm: number, measuredThicknessMm: number): number {
  const strictUpperBound = heightMm - measuredThicknessMm - DESIGN_CONSTRAINTS.baseCabinetUpperClearanceMm;
  return Math.min(
    DESIGN_CONSTRAINTS.baseCabinetHeightMm.maximum,
    Math.ceil(strictUpperBound) - 1,
  );
}
