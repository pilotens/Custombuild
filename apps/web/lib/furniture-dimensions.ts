import type { FurnitureInstallation, FurniturePreview, FurnitureTrimProfile, SurveyField } from "./furniture-workspace";

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

export function updateSurvey(site: FurnitureInstallation, field: SurveyField, value: number | null) {
  const fields = (site.surveyed_fields ?? []).filter(f => f !== field);
  if (value !== null) fields.push(field);
  return { ...site, [`measured_${field}_um`]: value, surveyed_fields: fields };
}

export function invalidateMovedDatums(before: FurnitureInstallation, after: FurnitureInstallation) {
  const changed = (key: keyof FurnitureInstallation) => before[key] !== after[key];
  const moved = new Set<SurveyField>();
  if (changed("width_um") || changed("left_allowance_um")) moved.add("left_wall");
  if (changed("width_um") || changed("right_allowance_um")) moved.add("right_wall");
  if (changed("depth_um") || changed("rear_allowance_um")) moved.add("rear_wall");
  if (changed("depth_um") || changed("front_allowance_um")) moved.add("front_space");
  let site = after;
  for (const field of moved) site = updateSurvey(site, field, null);
  return site;
}

/** Design reservations based on the server's actual timber strokes. Never supply survey data. */
export function standingFitAlternative(site: FurnitureInstallation, preview?: FurniturePreview) {
  const review = preview?.workshop_handoff?.dimensions.standing_assembly;
  const current = installationCarcassDimensions(site);
  if (!current || !review || site.bottom_allowance_um == null || site.top_allowance_um == null ||
    review.walls.some(w => w.required_reservation_um == null) ||
    (Object.keys(current) as (keyof typeof current)[]).some(k =>
      current[k] !== preview?.workshop_handoff?.dimensions.carcass_dimensions_um[k])) return null;
  if (review.measured_front_space_um != null &&
      review.measured_front_space_um <= review.minimum_front_space_um) return null;
  const next = { ...site };
  for (const wall of review.walls) {
    const key = `${wall.wall}_allowance_um` as const;
    // A smaller observed gap at the original datum adds to the reservation needed.
    const shortfall = wall.measured_distance_um == null ? 0 : Math.max(0, site[key]! - wall.measured_distance_um);
    const required = wall.required_reservation_um! + shortfall;
    // A moving side must clear the wall strictly. Round up to the next 10 mm.
    const planned = wall.step_number == null ? required : (Math.floor(required / 10_000) + 1) * 10_000;
    next[key] = Math.max(site[key]!, planned);
  }
  const rise = Math.max(0, review.minimum_height_from_base_um - current.height_um);
  // A planning allowance of 20 mm above the moving timber, within the customer's envelope.
  const ceilingDeficit = review.measured_ceiling_height_um == null ? 0 :
    Math.max(0, site.height_um - review.measured_ceiling_height_um);
  next.top_allowance_um = Math.max(site.top_allowance_um, Math.ceil((rise + ceilingDeficit + 20_000) / 10_000) * 10_000);
  if (INSTALLATION_ALLOWANCES.some(([key]) => next[key]! > 500_000) ||
      INSTALLATION_ALLOWANCES.every(([key]) => next[key] === site[key])) return null;
  const width = next.width_um - next.left_allowance_um! - next.right_allowance_um!;
  const height = next.height_um - next.bottom_allowance_um! - next.top_allowance_um!;
  if (width < 250_000 || width > 6_000_000 || height < 300_000 || height > 4_000_000) return null;
  return invalidateMovedDatums(site, next);
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
