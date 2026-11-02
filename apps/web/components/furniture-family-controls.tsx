import { exactMillimetreTextToMicrometres } from "@/lib/workshop-production-context";
import type { FurnitureIntent } from "@/lib/furniture-workspace";

interface Props {
  intent: FurnitureIntent;
  update: (patch: Partial<FurnitureIntent>, field?: string) => void;
  onError: (message: string, field: string) => void;
}

type Dimension = "leg_section_um" | "end_inset_um" | "stretcher_height_um" | "seat_height_um" | "seat_depth_um";

export function FurnitureFamilyControls({ intent, update, onError }: Props) {
  const exact = (raw: string, key: string, apply: (value: number) => void) => {
    try {
      apply(exactMillimetreTextToMicrometres(raw, { minimumUm: 0, maximumUm: 6_000_000 }));
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Kontrollera måttet.", `family.${key}`);
    }
  };

  const field = (key: Dimension, label: string, min: number, max: number) => (
    <label>{label} (mm)
      <input type="number" min={min} max={max} step="0.001" value={(intent[key] ?? 0) / 1000}
        onChange={e => exact(e.target.value, key, value => update({ [key]: value }, key))} />
    </label>
  );

  if (intent.family === "dining_table") return <>
    <label>Bordsskivans form
      <select value={intent.top_shape ?? "rectangle"} onChange={e => update(e.target.value === "round"
        ? { top_shape: "round", depth_um: intent.width_um } : { top_shape: "rectangle" })}>
        <option value="rectangle">Rektangulär</option>
        <option value="round">Rund</option>
      </select>
    </label>
    {field("leg_section_um", "Benens fyrkantsmått", 35, 80)}
    {field("end_inset_um", "Benens indrag", 0, 500)}
    {field("stretcher_height_um", "Sargens höjd", 65, 250)}
    <p>Fyra separata ben i hyvlad björk. För rund skiva mäts indraget från den inskrivna kvadraten.</p>
  </>;

  if (intent.family === "chair") return <>
    {field("seat_height_um", "Sitthöjd", 250, 700)}
    {field("seat_depth_um", "Sittdjup", 200, 700)}
    {field("leg_section_um", "Benens fyrkantsmått", 35, 75)}
    <label>Rygglutning (grader)
      <input type="number" min="0" max="18" step="0.001" value={(intent.back_angle_mdeg ?? 0) / 1000}
        onChange={e => exact(e.target.value, "angle", value => update({ back_angle_mdeg: value }, "angle"))} />
    </label>
  </>;

  if (intent.family !== "chest_of_drawers") return null;
  const columns = intent.drawer_columns ?? 1;
  const rows = Math.floor((intent.drawer_count ?? 1) / columns);
  const heights = intent.row_front_heights_um ?? [];
  const changeColumns = (value: 1 | 2) => {
    const count = Math.ceil((intent.drawer_count ?? 1) / value) * value;
    update({ drawer_columns: value, drawer_count: Math.max(value, Math.min(8, count)), row_front_heights_um: [] });
  };
  const changeHeight = (raw: string, index: number) => {
    const key = `row${index}`;
    if (!raw) {
      update({ row_front_heights_um: heights.slice(0, index) }, key);
      return;
    }
    exact(raw, key, value => {
      const next = heights.slice();
      next[index] = value;
      update({ row_front_heights_um: next }, key);
    });
  };
  return <>
    <label>Lådkolumner
      <select value={columns} onChange={e => changeColumns(e.target.value === "2" ? 2 : 1)}>
        <option value="1">En kolumn</option><option value="2">Två kolumner</option>
      </select>
    </label>
    <label>Definition av frontspel
      <select value={intent.gap_basis ?? "per_edge"} onChange={e => update({
        gap_basis: e.target.value === "clear_reveal" ? "clear_reveal" : "per_edge",
      })}>
        <option value="clear_reveal">Vertikal springa mellan lådfronter</option>
        <option value="per_edge">Indrag per kant (äldre modell)</option>
      </select>
    </label>
    {Array.from({ length: Math.max(0, Math.min(rows - 1, heights.length + 1)) }, (_, i) =>
      <label key={i}>Fronthöjd rad {i + 1} nedifrån (mm)
        <input type="number" min="70" step="0.001" placeholder="Automatisk"
          value={heights[i] === undefined ? "" : heights[i] / 1000} onChange={e => changeHeight(e.target.value, i)} />
      </label>)}
    <p>Återstående höjd delas mellan automatiska rader.</p>
  </>;
}
