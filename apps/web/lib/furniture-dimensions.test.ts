import { describe, expect, it } from "vitest";
import { installationCarcassDimensions, parseFurniturePercentages } from "./furniture-dimensions";
import type { FurnitureInstallation } from "./furniture-workspace";

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
