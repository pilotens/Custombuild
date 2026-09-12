import type { FurnitureInstallation, FurnitureWorkspace } from "./furniture-workspace";

export const INSTALLATION_ALLOWANCES = [
  ["left_allowance_um", "Vänster"], ["right_allowance_um", "Höger"],
  ["top_allowance_um", "Ovanför"], ["bottom_allowance_um", "Under"],
  ["front_allowance_um", "Framför"], ["rear_allowance_um", "Bakom"],
] as const;

/** Match the server invariant even when some allowances are still unknown. */
export function assertInstallationRemainingSpace(value: FurnitureInstallation): void {
  for (const [size, first, second] of [
    [value.width_um, value.left_allowance_um, value.right_allowance_um],
    [value.height_um, value.top_allowance_um, value.bottom_allowance_um],
    [value.depth_um, value.front_allowance_um, value.rear_allowance_um],
  ]) {
    if ((first ?? 0) + (second ?? 0) >= size!) {
      throw new Error("Reserverat utrymme måste lämna ett positivt stommått. Kontrollera kundmåttet och båda sidornas frigång.");
    }
  }
}

/** The editor must not put a schema-invalid row load in its recoverable workspace. */
export function assertFurnitureRowLoad(workspace: FurnitureWorkspace): void {
  const intent = workspace.design.intent;
  if (intent.family !== "shelving") return;
  const perMetre = intent.shelf_load_per_metre_n ?? 0;
  if (!Number.isSafeInteger(perMetre) || perMetre < 0 || perMetre > 5_000
    || (intent.shelf_load_basis === "per_metre" && Math.ceil(intent.width_um * perMetre / 1_000_000) > 5_000)) {
    throw new Error("Den jämnt fördelade lasten för hela hyllraden får vara högst 5000 N. Minska lasten eller bredden.");
  }
}

export function installationCarcassDimensions(value: FurnitureInstallation) {
  assertInstallationRemainingSpace(value);
  if (INSTALLATION_ALLOWANCES.some(([key]) => value[key] === null)) return null;
  const dimensions = {
    width_um: value.width_um - value.left_allowance_um! - value.right_allowance_um!,
    height_um: value.height_um - value.top_allowance_um! - value.bottom_allowance_um!,
    depth_um: value.depth_um - value.front_allowance_um! - value.rear_allowance_um!,
  };
  if (Object.values(dimensions).some(v => !Number.isSafeInteger(v) || v <= 0)) {
    throw new Error("Reserverat utrymme lämnar inget positivt stommått.");
  }
  return dimensions;
}

/** Percent text is exact to one ppm; no floating point normalization of the design. */
export function parseFurniturePercentages(raw: string, count: number, kind: "bays" | "shelves"): number[] {
  if (!raw.trim()) return [];
  const tokens = raw.trim().split(/[;\s]+/);
  if (tokens.length !== count) throw new Error(`Ange ${count} värden eller lämna fältet tomt för jämn fördelning.`);
  const values = tokens.map(token => {
    const match = /^(\d{1,3})(?:[.,](\d{1,4}))?$/.exec(token);
    if (!match) throw new Error("Använd procent med högst fyra decimaler. Separera värden med semikolon.");
    return Number(match[1]) * 10_000 + Number((match[2] ?? "").padEnd(4, "0"));
  });
  if (kind === "bays" && (values.some(v => v < 80_000) || values.reduce((a, b) => a+b, 0) !== 1_000_000)) {
    throw new Error("Fackbredderna ska summera till 100 %. Varje fack ska vara minst 8 %.");
  }
  if (kind === "shelves" && values.some((v, i) => v < 50_000 || v > 950_000 || (i > 0 && v-values[i-1]! < 50_000))) {
    throw new Error("Hyllcentrumen ska ligga mellan 5 och 95 %, i stigande ordning med minst 5 procentenheters mellanrum.");
  }
  return values;
}
