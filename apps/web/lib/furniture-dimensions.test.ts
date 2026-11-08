import { describe, expect, it } from "vitest";
import { changeTrimProfile, installationCarcassDimensions, parseFurniturePercentages, standingFitAlternative, invalidateMovedDatums, updateSurvey } from "./furniture-dimensions";
import type { FurnitureInstallation, FurniturePreview, FurnitureTrimProfile } from "./furniture-workspace";

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

describe("stående platsalternativ", () => {
  const site: FurnitureInstallation = { width_um: 4340000, height_um: 2540000, depth_um: 280000,
    left_allowance_um: 20000, right_allowance_um: 20000, rear_allowance_um: 20000,
    front_allowance_um: 0, top_allowance_um: 0, bottom_allowance_um: 0, width_includes_trim: true };
  const preview = { workshop_handoff: { dimensions: { carcass_dimensions_um: installationCarcassDimensions(site),
    standing_assembly: { minimum_height_from_base_um: 2558000,
      walls: [{wall: "left", step_number: 12, required_reservation_um: 26000},
        {wall: "right", step_number: 13, required_reservation_um: 26000},
        {wall: "rear", step_number: null, required_reservation_um: 20000}] } } } } as FurniturePreview;
  it("använder träets införingsbana och fyller inte i platsmätningar", () => {
    const next = standingFitAlternative(site, preview)!;
    expect([next.width_um, next.height_um, next.depth_um]).toEqual([4340000,2540000,280000]);
    expect([next.left_allowance_um, next.right_allowance_um, next.top_allowance_um]).toEqual([30000,30000,40000]);
    expect(installationCarcassDimensions(next)).toEqual({width_um:4280000,height_um:2500000,depth_um:260000});
    expect(next.surveyed_fields).toEqual([]);
    expect(standingFitAlternative(next, preview)).toBeNull();
    expect(site.left_allowance_um).toBe(20000);
  });
  it("tar med känd låg takhöjd och ett mindre verkligt sidomått i förslaget", () => {
    const check = structuredClone(preview);
    const survey = check.workshop_handoff!.dimensions.standing_assembly!;
    survey.measured_ceiling_height_um = 2500000;
    survey.minimum_front_space_um = 248000;
    survey.walls[0]!.measured_distance_um = 12000;
    const next = standingFitAlternative(site,check)!;
    expect(next.left_allowance_um).toBe(40000);
    expect(next.top_allowance_um).toBe(80000);
    survey.measured_front_space_um = 248000;
    expect(standingFitAlternative(site,check)).toBeNull();
  });
  it("bevarar oberoende takhöjd och nollmätning tills väggens datum ändras", () => {
    const measured = updateSurvey(updateSurvey(site, "ceiling_height", 2540000), "left_wall", 0);
    const withRight = updateSurvey(measured, "right_wall", 27500);
    const next = invalidateMovedDatums(withRight, {...withRight, left_allowance_um:30000});
    expect(measured.measured_left_wall_um).toBe(0);
    expect(next.surveyed_fields).toEqual(["ceiling_height","right_wall"]);
    expect(next.measured_right_wall_um).toBe(27500);
    expect(next.measured_left_wall_um).toBeNull();
    expect(next.measured_ceiling_height_um).toBe(2540000);
  });
});
