import {
  BACK_MATERIALS,
  MACHINES,
  MATERIALS,
  type DesignSpec,
} from "./design-types";

export type WorkshopStockRole = "carcass" | "back";
export type WorkshopGrainDirection = "X" | "Y" | "NONE";

export interface WorkshopStockZone {
  x_um: number;
  y_um: number;
  width_um: number;
  height_um: number;
}

export interface WorkshopStockProfile {
  role: WorkshopStockRole;
  declaration_authority: "CLIENT_DECLARED";
  supplier_profile_id: string;
  supplier_profile_version: string;
  material_id: string;
  material_version: string;
  sheet_width_um: number;
  sheet_height_um: number;
  thickness_um: number;
  sheet_count: number;
  trim_margin_um: number;
  kerf_um: number;
  grain_direction: WorkshopGrainDirection;
  allow_rotation: boolean;
  defect_zones: WorkshopStockZone[];
  fixture_keep_out_zones: WorkshopStockZone[];
}

export interface WorkshopRegistrationPin {
  x_um: number;
  y_um: number;
}

export interface WorkshopTwoSidedRegistration {
  stock_role: WorkshopStockRole;
  sheet_index: number;
  declaration_authority: "CLIENT_DECLARED";
  flip_axis: "X";
  fixture_method_id: string;
  fixture_method_version: string;
  pin_diameter_um: number;
  position_tolerance_um: number;
  pins: WorkshopRegistrationPin[];
}

/** Supplier/shop declarations that affect nesting and setup, never CAD geometry. */
export interface WorkshopProductionContext {
  stock_profiles: WorkshopStockProfile[];
  two_sided_registrations?: WorkshopTwoSidedRegistration[];
}

export interface RevisionProductionContextSnapshot {
  stock_width_mm: number;
  stock_height_mm: number;
  stock_count: number;
  back_stock_width_mm: number;
  back_stock_height_mm: number;
  back_stock_count: number;
  machine_profile_id: string;
  stock_profiles?: WorkshopStockProfile[];
  two_sided_registrations?: WorkshopTwoSidedRegistration[];
}

export interface WorkshopDesignBinding {
  material_id: string;
  measured_thickness_mm: number;
  measured_back_thickness_mm: number;
  back_panel: boolean;
  back_material_id: string;
  stock_width_mm: number;
  stock_height_mm: number;
  stock_count: number;
  back_stock_width_mm: number;
  back_stock_height_mm: number;
  back_stock_count: number;
}

export class WorkshopProductionContextError extends Error {
  constructor(readonly issues: readonly string[]) {
    super(issues[0] ?? "Verkstadskontexten är ogiltig.");
    this.name = "WorkshopProductionContextError";
  }
}

type JsonRecord = Record<string, unknown>;

const IDENTITY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const PROFILE_KEYS = [
  "role",
  "declaration_authority",
  "supplier_profile_id",
  "supplier_profile_version",
  "material_id",
  "material_version",
  "sheet_width_um",
  "sheet_height_um",
  "thickness_um",
  "sheet_count",
  "trim_margin_um",
  "kerf_um",
  "grain_direction",
  "allow_rotation",
  "defect_zones",
  "fixture_keep_out_zones",
] as const;
const ZONE_KEYS = ["x_um", "y_um", "width_um", "height_um"] as const;
const REGISTRATION_KEYS = [
  "stock_role",
  "sheet_index",
  "declaration_authority",
  "flip_axis",
  "fixture_method_id",
  "fixture_method_version",
  "pin_diameter_um",
  "position_tolerance_um",
  "pins",
] as const;
const PIN_KEYS = ["x_um", "y_um"] as const;

function fail(issue: string): never {
  throw new WorkshopProductionContextError([issue]);
}

function record(value: unknown, path: string): JsonRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    fail(`${path} måste vara ett objekt.`);
  }
  return value as JsonRecord;
}

function exactKeys(
  value: JsonRecord,
  required: readonly string[],
  optional: readonly string[],
  path: string,
): void {
  const allowed = new Set([...required, ...optional]);
  if (Object.keys(value).some((key) => !allowed.has(key))) {
    fail(`${path} innehåller okända fält.`);
  }
  if (required.some((key) => !Object.hasOwn(value, key))) {
    fail(`${path} saknar obligatoriska fält.`);
  }
}

function exactInteger(value: unknown, path: string, minimum: number, maximum: number): number {
  if (
    typeof value !== "number"
    || !Number.isSafeInteger(value)
    || value < minimum
    || value > maximum
  ) fail(`${path} måste vara ett heltal mellan ${minimum} och ${maximum}.`);
  return value;
}

function exactBoolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") fail(`${path} måste vara sant eller falskt.`);
  return value;
}

function identity(value: unknown, path: string): string {
  if (
    typeof value !== "string"
    || value.length < 1
    || value.length > 64
    || !IDENTITY_PATTERN.test(value)
  ) fail(`${path} måste vara ett stabilt ID med 1–64 ASCII-tecken.`);
  return value;
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], path: string): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    fail(`${path} har ett värde som inte stöds.`);
  }
  return value as T;
}

function boundedArray(value: unknown, path: string, minimum: number, maximum: number): unknown[] {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) {
    fail(`${path} måste innehålla ${minimum}–${maximum} poster.`);
  }
  return value;
}

/** Parse decimal millimetres exactly; binary floating-point multiplication is never used. */
export function exactMillimetreTextToMicrometres(
  raw: string,
  options: { minimumUm?: number; maximumUm?: number } = {},
): number {
  const value = raw.trim();
  if (value.length === 0 || value.length > 64) {
    fail("Ange ett mått i millimeter med högst tre decimaler.");
  }
  const match = /^([+-]?)(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$/.exec(value);
  if (!match) fail("Ange ett mått i millimeter med högst tre decimaler.");
  const fraction = match[3] ?? match[4] ?? "";
  const exponent = Number(match[5] ?? "0");
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 100) {
    fail("Måttet ligger utanför tillåtet format.");
  }
  const unsignedDigits = `${match[2] ?? "0"}${fraction}`.replace(/^0+(?=\d)/, "");
  let scaled = BigInt(unsignedDigits || "0");
  const power = exponent - fraction.length + 3;
  if (power >= 0) {
    scaled *= 10n ** BigInt(power);
  } else {
    const divisor = 10n ** BigInt(-power);
    if (scaled % divisor !== 0n) {
      fail("Ange högst tre decimaler (en tusendels millimeter).");
    }
    scaled /= divisor;
  }
  if (match[1] === "-") scaled = -scaled;
  const minimum = BigInt(options.minimumUm ?? 0);
  const maximum = BigInt(options.maximumUm ?? Number.MAX_SAFE_INTEGER);
  if (scaled < minimum || scaled > maximum || scaled > BigInt(Number.MAX_SAFE_INTEGER)) {
    fail("Måttet ligger utanför tillåtet intervall.");
  }
  return Number(scaled);
}

export function micrometresToMillimetreText(valueUm: number): string {
  const value = exactInteger(valueUm, "Mikrometervärdet", 0, Number.MAX_SAFE_INTEGER);
  const whole = Math.floor(value / 1_000);
  const fraction = String(value % 1_000).padStart(3, "0").replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : String(whole);
}

function millimetreNumberToMicrometres(
  value: number,
  path: string,
  maximumUm: number,
): number {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(`${path} är inte ett giltigt mått.`);
  try {
    return exactMillimetreTextToMicrometres(String(value), {
      minimumUm: 1,
      maximumUm,
    });
  } catch (caught) {
    if (caught instanceof WorkshopProductionContextError) {
      fail(`${path}: ${caught.message}`);
    }
    throw caught;
  }
}

function parseZone(
  value: unknown,
  path: string,
  sheetWidthUm: number,
  sheetHeightUm: number,
): WorkshopStockZone {
  const root = record(value, path);
  exactKeys(root, ZONE_KEYS, [], path);
  const zone = {
    x_um: exactInteger(root.x_um, `${path}.x_um`, 0, 10_000_000),
    y_um: exactInteger(root.y_um, `${path}.y_um`, 0, 5_000_000),
    width_um: exactInteger(root.width_um, `${path}.width_um`, 1, 10_000_000),
    height_um: exactInteger(root.height_um, `${path}.height_um`, 1, 5_000_000),
  };
  if (zone.x_um + zone.width_um > sheetWidthUm || zone.y_um + zone.height_um > sheetHeightUm) {
    fail(`${path} ligger utanför råskivan.`);
  }
  return zone;
}

function zoneKey(zone: WorkshopStockZone): string {
  return `${zone.y_um}:${zone.x_um}:${zone.height_um}:${zone.width_um}`;
}

function parseZones(
  value: unknown,
  path: string,
  sheetWidthUm: number,
  sheetHeightUm: number,
): WorkshopStockZone[] {
  const zones = boundedArray(value, path, 0, 100)
    .map((zone, index) => parseZone(zone, `${path}[${index}]`, sheetWidthUm, sheetHeightUm))
    .sort((left, right) => (
      left.y_um - right.y_um
      || left.x_um - right.x_um
      || left.height_um - right.height_um
      || left.width_um - right.width_um
    ));
  if (new Set(zones.map(zoneKey)).size !== zones.length) fail(`${path} innehåller dubbletter.`);
  return zones;
}

function parseProfile(value: unknown, path: string): WorkshopStockProfile {
  const root = record(value, path);
  exactKeys(root, PROFILE_KEYS, [], path);
  const sheetWidthUm = exactInteger(root.sheet_width_um, `${path}.sheet_width_um`, 1, 10_000_000);
  const sheetHeightUm = exactInteger(root.sheet_height_um, `${path}.sheet_height_um`, 1, 5_000_000);
  const profile: WorkshopStockProfile = {
    role: oneOf(root.role, ["carcass", "back"] as const, `${path}.role`),
    declaration_authority: oneOf(
      root.declaration_authority,
      ["CLIENT_DECLARED"] as const,
      `${path}.declaration_authority`,
    ),
    supplier_profile_id: identity(root.supplier_profile_id, `${path}.supplier_profile_id`),
    supplier_profile_version: identity(root.supplier_profile_version, `${path}.supplier_profile_version`),
    material_id: identity(root.material_id, `${path}.material_id`),
    material_version: identity(root.material_version, `${path}.material_version`),
    sheet_width_um: sheetWidthUm,
    sheet_height_um: sheetHeightUm,
    thickness_um: exactInteger(root.thickness_um, `${path}.thickness_um`, 1, 100_000),
    sheet_count: exactInteger(root.sheet_count, `${path}.sheet_count`, 1, 100),
    trim_margin_um: exactInteger(root.trim_margin_um, `${path}.trim_margin_um`, 0, 500_000),
    kerf_um: exactInteger(root.kerf_um, `${path}.kerf_um`, 6_000, 100_000),
    grain_direction: oneOf(root.grain_direction, ["X", "Y", "NONE"] as const, `${path}.grain_direction`),
    allow_rotation: exactBoolean(root.allow_rotation, `${path}.allow_rotation`),
    defect_zones: parseZones(root.defect_zones, `${path}.defect_zones`, sheetWidthUm, sheetHeightUm),
    fixture_keep_out_zones: parseZones(
      root.fixture_keep_out_zones,
      `${path}.fixture_keep_out_zones`,
      sheetWidthUm,
      sheetHeightUm,
    ),
  };
  if (profile.trim_margin_um * 2 >= Math.min(sheetWidthUm, sheetHeightUm)) {
    fail(`${path}.trim_margin_um förbrukar hela den användbara råskivan.`);
  }
  return profile;
}

function parsePin(
  value: unknown,
  path: string,
  profile: WorkshopStockProfile,
): WorkshopRegistrationPin {
  const root = record(value, path);
  exactKeys(root, PIN_KEYS, [], path);
  return {
    x_um: exactInteger(root.x_um, `${path}.x_um`, 0, profile.sheet_width_um),
    y_um: exactInteger(root.y_um, `${path}.y_um`, 0, profile.sheet_height_um),
  };
}

function parseRegistration(
  value: unknown,
  path: string,
  profiles: ReadonlyMap<WorkshopStockRole, WorkshopStockProfile>,
): WorkshopTwoSidedRegistration {
  const root = record(value, path);
  exactKeys(root, REGISTRATION_KEYS, [], path);
  const stockRole = oneOf(root.stock_role, ["carcass", "back"] as const, `${path}.stock_role`);
  const profile = profiles.get(stockRole);
  if (!profile) fail(`${path}.stock_role saknar motsvarande råmaterialprofil.`);
  const pins = boundedArray(root.pins, `${path}.pins`, 2, 16)
    .map((pin, index) => parsePin(pin, `${path}.pins[${index}]`, profile));
  const pinKeys = pins.map((pin) => `${pin.x_um}:${pin.y_um}`);
  if (new Set(pinKeys).size !== pinKeys.length) fail(`${path}.pins måste vara unika.`);
  const pinDiameterUm = exactInteger(root.pin_diameter_um, `${path}.pin_diameter_um`, 1_000, 50_000);
  const positionToleranceUm = exactInteger(
    root.position_tolerance_um,
    `${path}.position_tolerance_um`,
    1,
    10_000,
  );
  if (positionToleranceUm * 2 >= pinDiameterUm) {
    fail(`${path}.position_tolerance_um måste vara mindre än pinnens radie.`);
  }
  const footprintRadius = Math.ceil(pinDiameterUm / 2) + positionToleranceUm;
  const blockedZones = [...profile.defect_zones, ...profile.fixture_keep_out_zones];
  for (const [index, pin] of pins.entries()) {
    if (
      pin.x_um < footprintRadius
      || pin.y_um < footprintRadius
      || pin.x_um + footprintRadius > profile.sheet_width_um
      || pin.y_um + footprintRadius > profile.sheet_height_um
    ) fail(`${path}.pins[${index}] har ett fotavtryck utanför råskivan.`);
    if (blockedZones.some((zone) => (
      pin.x_um + footprintRadius > zone.x_um
      && pin.x_um - footprintRadius < zone.x_um + zone.width_um
      && pin.y_um + footprintRadius > zone.y_um
      && pin.y_um - footprintRadius < zone.y_um + zone.height_um
    ))) fail(`${path}.pins[${index}] kolliderar med en blockerad råmaterialzon.`);
  }
  const requiredCentreDistance = 100_000 + 2 * footprintRadius;
  if (pins.some((left, leftIndex) => pins.slice(leftIndex + 1).some((right) => (
    (right.x_um - left.x_um) ** 2 + (right.y_um - left.y_um) ** 2
      < requiredCentreDistance ** 2
  )))) {
    fail(`${path}.pins måste parvis ha minst 100 mm användbar registreringsbaslinje.`);
  }
  return {
    stock_role: stockRole,
    sheet_index: exactInteger(root.sheet_index, `${path}.sheet_index`, 0, profile.sheet_count - 1),
    declaration_authority: oneOf(
      root.declaration_authority,
      ["CLIENT_DECLARED"] as const,
      `${path}.declaration_authority`,
    ),
    flip_axis: oneOf(root.flip_axis, ["X"] as const, `${path}.flip_axis`),
    fixture_method_id: identity(root.fixture_method_id, `${path}.fixture_method_id`),
    fixture_method_version: identity(
      root.fixture_method_version,
      `${path}.fixture_method_version`,
    ),
    pin_diameter_um: pinDiameterUm,
    position_tolerance_um: positionToleranceUm,
    pins,
  };
}

function assertDesignBinding(
  profiles: readonly WorkshopStockProfile[],
  binding: WorkshopDesignBinding,
): void {
  const expectedRoles: WorkshopStockRole[] = binding.back_panel ? ["carcass", "back"] : ["carcass"];
  if (
    profiles.length !== expectedRoles.length
    || profiles.some((profile, index) => profile.role !== expectedRoles[index])
  ) fail("Råmaterialprofilerna täcker inte exakt den aktuella designens material.");

  const carcassMaterial = MATERIALS.find((material) => material.id === binding.material_id);
  const backMaterial = BACK_MATERIALS.find((material) => material.id === binding.back_material_id);
  if (!carcassMaterial || (binding.back_panel && !backMaterial)) {
    fail("Designens material saknar en versionslåst katalogpost.");
  }
  const expectedByRole: Record<WorkshopStockRole, {
    materialId: string;
    materialVersion: string;
    thicknessUm: number;
    widthUm: number;
    heightUm: number;
    count: number;
  }> = {
    carcass: {
      materialId: binding.material_id,
      materialVersion: carcassMaterial.version,
      thicknessUm: millimetreNumberToMicrometres(binding.measured_thickness_mm, "Stommens tjocklek", 100_000),
      widthUm: millimetreNumberToMicrometres(binding.stock_width_mm, "Stomskivans bredd", 10_000_000),
      heightUm: millimetreNumberToMicrometres(binding.stock_height_mm, "Stomskivans höjd", 5_000_000),
      count: binding.stock_count,
    },
    back: {
      materialId: binding.back_material_id,
      materialVersion: backMaterial?.version ?? "",
      thicknessUm: millimetreNumberToMicrometres(
        binding.measured_back_thickness_mm,
        "Bakstyckets tjocklek",
        100_000,
      ),
      widthUm: millimetreNumberToMicrometres(binding.back_stock_width_mm, "Bakskivans bredd", 10_000_000),
      heightUm: millimetreNumberToMicrometres(binding.back_stock_height_mm, "Bakskivans höjd", 5_000_000),
      count: binding.back_stock_count,
    },
  };
  for (const profile of profiles) {
    const expected = expectedByRole[profile.role];
    if (
      profile.material_id !== expected.materialId
      || profile.material_version !== expected.materialVersion
      || profile.thickness_um !== expected.thicknessUm
      || profile.sheet_width_um !== expected.widthUm
      || profile.sheet_height_um !== expected.heightUm
      || profile.sheet_count !== expected.count
    ) fail(`${profile.role}-profilen matchar inte designens frysta material och skivmått.`);
  }
}

/** Strictly parse and canonically order caller-declared workshop truth. */
export function parseWorkshopProductionContext(
  value: unknown,
  binding?: WorkshopDesignBinding,
): WorkshopProductionContext | undefined {
  if (value === undefined) return undefined;
  const root = record(value, "workshop_context");
  exactKeys(root, ["stock_profiles"], ["two_sided_registrations"], "workshop_context");
  const profiles = boundedArray(root.stock_profiles, "workshop_context.stock_profiles", 1, 2)
    .map((profile, index) => parseProfile(profile, `workshop_context.stock_profiles[${index}]`))
    .sort((left, right) => (left.role === right.role ? 0 : left.role === "carcass" ? -1 : 1));
  if (new Set(profiles.map((profile) => profile.role)).size !== profiles.length) {
    fail("workshop_context.stock_profiles innehåller samma roll flera gånger.");
  }
  const profileIdentities = profiles.map((profile) => (
    `${profile.supplier_profile_id}\u0000${profile.supplier_profile_version}`
  ));
  if (new Set(profileIdentities).size !== profileIdentities.length) {
    fail("Leverantörsprofilernas identiteter måste vara unika.");
  }
  if (profiles[0]?.role !== "carcass") fail("En stomprofil är obligatorisk.");
  if (binding) assertDesignBinding(profiles, binding);

  const profileByRole = new Map(profiles.map((profile) => [profile.role, profile] as const));
  const rawRegistrations = root.two_sided_registrations;
  const registrations = rawRegistrations === undefined
    ? []
    : boundedArray(rawRegistrations, "workshop_context.two_sided_registrations", 0, 200)
      .map((registration, index) => parseRegistration(
        registration,
        `workshop_context.two_sided_registrations[${index}]`,
        profileByRole,
      ))
      .sort((left, right) => (
        (left.stock_role === right.stock_role ? 0 : left.stock_role === "carcass" ? -1 : 1)
        || left.sheet_index - right.sheet_index
      ));
  const registrationKeys = registrations.map((registration) => (
    `${registration.stock_role}:${registration.sheet_index}`
  ));
  if (new Set(registrationKeys).size !== registrations.length) {
    fail("Tvåsidig registrering får bara anges en gång per råskiva.");
  }
  return {
    stock_profiles: profiles,
    ...(registrations.length > 0 ? { two_sided_registrations: registrations } : {}),
  };
}

export function productionContextFromDesignSpec(spec: DesignSpec): RevisionProductionContextSnapshot {
  const workshop = parseWorkshopProductionContext(spec.workshop_context, spec);
  return {
    stock_width_mm: spec.stock_width_mm,
    stock_height_mm: spec.stock_height_mm,
    stock_count: spec.stock_count,
    back_stock_width_mm: spec.back_stock_width_mm,
    back_stock_height_mm: spec.back_stock_height_mm,
    back_stock_count: spec.back_stock_count,
    machine_profile_id: spec.machine_profile_id,
    ...(workshop?.stock_profiles ? { stock_profiles: workshop.stock_profiles } : {}),
    ...(workshop?.two_sided_registrations
      ? { two_sided_registrations: workshop.two_sided_registrations }
      : {}),
  };
}

export function parseRevisionProductionContext(
  value: unknown,
  spec?: DesignSpec,
): RevisionProductionContextSnapshot {
  const root = record(value, "production_context");
  const required = [
    "stock_width_mm",
    "stock_height_mm",
    "stock_count",
    "back_stock_width_mm",
    "back_stock_height_mm",
    "back_stock_count",
    "machine_profile_id",
  ] as const;
  exactKeys(root, required, ["stock_profiles", "two_sided_registrations"], "production_context");
  const numberField = (field: typeof required[number], minimumUm: number, maximumUm: number): number => {
    const raw = root[field];
    if (typeof raw !== "number" || !Number.isFinite(raw)) fail(`production_context.${field} är ogiltigt.`);
    exactMillimetreTextToMicrometres(String(raw), { minimumUm, maximumUm });
    return raw;
  };
  const countField = (field: "stock_count" | "back_stock_count") => (
    exactInteger(root[field], `production_context.${field}`, 1, 100)
  );
  const machineProfileId = oneOf(
    root.machine_profile_id,
    MACHINES.map((machine) => machine.id),
    "production_context.machine_profile_id",
  );
  const parsed: RevisionProductionContextSnapshot = {
    stock_width_mm: numberField("stock_width_mm", 1, 10_000_000),
    stock_height_mm: numberField("stock_height_mm", 1, 5_000_000),
    stock_count: countField("stock_count"),
    back_stock_width_mm: numberField("back_stock_width_mm", 1, 10_000_000),
    back_stock_height_mm: numberField("back_stock_height_mm", 1, 5_000_000),
    back_stock_count: countField("back_stock_count"),
    machine_profile_id: machineProfileId,
  };
  if (spec) {
    const legacyExpected = {
      stock_width_mm: spec.stock_width_mm,
      stock_height_mm: spec.stock_height_mm,
      stock_count: spec.stock_count,
      back_stock_width_mm: spec.back_stock_width_mm,
      back_stock_height_mm: spec.back_stock_height_mm,
      back_stock_count: spec.back_stock_count,
      machine_profile_id: spec.machine_profile_id,
    };
    if (Object.entries(legacyExpected).some(([key, expected]) => parsed[key as keyof typeof parsed] !== expected)) {
      fail("production_context matchar inte den aktuella designens lager- och maskinval.");
    }
  }
  if (root.two_sided_registrations !== undefined && root.stock_profiles === undefined) {
    fail("Tvåsidig registrering kräver strukturerade råmaterialprofiler.");
  }
  const workshop = root.stock_profiles === undefined
    ? undefined
    : parseWorkshopProductionContext({
        stock_profiles: root.stock_profiles,
        ...(root.two_sided_registrations === undefined
          ? {}
          : { two_sided_registrations: root.two_sided_registrations }),
      }, spec);
  return {
    ...parsed,
    ...(workshop?.stock_profiles ? { stock_profiles: workshop.stock_profiles } : {}),
    ...(workshop?.two_sided_registrations
      ? { two_sided_registrations: workshop.two_sided_registrations }
      : {}),
  };
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as JsonRecord)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => `${JSON.stringify(key)}:${canonicalJson(child)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function productionContextsEqual(
  value: unknown,
  spec: DesignSpec,
): boolean {
  try {
    return canonicalJson(parseRevisionProductionContext(value, spec))
      === canonicalJson(productionContextFromDesignSpec(spec));
  } catch {
    return false;
  }
}
