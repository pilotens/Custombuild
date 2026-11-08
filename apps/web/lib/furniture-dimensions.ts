import type { FurnitureInstallation, FurnitureTrimProfile } from "./furniture-workspace";

export const INSTALLATION_ALLOWANCES = [
  ["left_allowance_um", "Vänster"], ["right_allowance_um", "Höger"],
  ["top_allowance_um", "Ovanför"], ["bottom_allowance_um", "Under"],
  ["front_allowance_um", "Framför"], ["rear_allowance_um", "Bakom"],
] as const;

export function installationCarcassDimensions(value: FurnitureInstallation) {
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

/** A removed skirting board leaves a wall. Preserve what was measured on the other sides. */
export function changeTrimProfile(site: FurnitureInstallation, profile: FurnitureTrimProfile | null): FurnitureInstallation {
  const previousWalls = site.trim_profile?.use === "existing_room_trim" ? site.trim_profile.walls : [];
  const nextWalls = profile?.use === "existing_room_trim" ? profile.walls : [];
  const removed = previousWalls.filter(wall => !nextWalls.includes(wall));
  const updated = { ...site, trim_profile: profile };
  if (!removed.length) return updated;

  const bounded = [...new Set([...(site.additional_bounded_walls ?? []), ...removed])];
  const unknown = (site.additional_bounded_walls == null || site.remaining_walls_unmeasured)
    && new Set([...bounded, ...nextWalls]).size < 3;
  return { ...updated, additional_bounded_walls: bounded, remaining_walls_unmeasured: unknown || undefined };
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
