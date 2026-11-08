import { describe, expect, it } from "vitest";
import { changeTrimProfile, installationCarcassDimensions, parseFurniturePercentages } from "./furniture-dimensions";
import type { FurnitureInstallation, FurnitureTrimProfile } from "./furniture-workspace";

const measured: FurnitureInstallation = {
  width_um: 4_340_007, height_um: 2_540_009, depth_um: 280_001, width_includes_trim: true,
  left_allowance_um: 50_001, right_allowance_um: 45_003, top_allowance_um: 10_002,
  bottom_allowance_um: 0, front_allowance_um: 0, rear_allowance_um: 3_001,
};

describe("kundmått och proportioner", () => {
  it("drar av reserverat utrymme exakt och behandlar aldrig okänt som noll", () => {
    expect(installationCarcassDimensions(measured)).toEqual({ width_um: 4_245_003, height_um: 2_530_007, depth_um: 277_000 });
    expect(installationCarcassDimensions({ ...measured, bottom_allowance_um: null })).toBeNull();
    expect(() => installationCarcassDimensions({ ...measured, rear_allowance_um: 280_001 })).toThrow();
  });
  it("behåller väggen när listen tas bort och låter ouppmätta sidor vara okända", () => {
    const left: FurnitureTrimProfile = { use: "existing_room_trim", height_um: 90_000, width_um: 20_000, walls: ["left"] };
    const partial = changeTrimProfile({ ...measured, trim_profile: left }, null);
    expect(partial.additional_bounded_walls).toEqual(["left"]);
    expect(partial.remaining_walls_unmeasured).toBe(true);
    const surveyed = changeTrimProfile({ ...measured, trim_profile: left, additional_bounded_walls: ["rear"] }, null);
    expect(surveyed.additional_bounded_walls).toEqual(["rear", "left"]);
    expect(surveyed.remaining_walls_unmeasured).toBeUndefined();
    const all: FurnitureTrimProfile = { ...left, walls: ["left", "right", "rear"] };
    const removed = changeTrimProfile({ ...measured, trim_profile: all }, left);
    expect(removed.additional_bounded_walls).toEqual(["right", "rear"]);
    expect(removed.remaining_walls_unmeasured).toBeUndefined();
    const edited = changeTrimProfile(removed, { ...left, width_um: 25_000 });
    expect(edited.additional_bounded_walls).toEqual(["right", "rear"]);
    expect(edited.left_allowance_um).toBe(measured.left_allowance_um);
  });
  it("bevarar oregelbundna fack och hyllcentrum utan flyttalsavrundning", () => {
    expect(parseFurniturePercentages("25,0001; 34.9999; 40", 3, "bays")).toEqual([250_001, 349_999, 400_000]);
    expect(parseFurniturePercentages("10,0001; 45.0007; 80.0009", 3, "shelves")).toEqual([100_001, 450_007, 800_009]);
    expect(parseFurniturePercentages("", 9, "shelves")).toEqual([]);
  });
  it.each(["25; 35; 39", "1; 49; 50", "25.00001; 35; 40", "25; 75"])("avvisar felaktiga fackvärden: %s", raw => {
    expect(() => parseFurniturePercentages(raw, 3, "bays")).toThrow();
  });
  it.each(["5; 7; 80", "40; 10; 80", "10; 50; 98"])("avvisar hyllor med fel ordning eller avstånd: %s", raw => {
    expect(() => parseFurniturePercentages(raw, 3, "shelves")).toThrow();
  });
});
