"use client";

import type { FurnitureManufacturingSelection, FurnitureMaterialStock, FurniturePreview,
  FurnitureStockFormat, FurnitureWorkspace } from "@/lib/furniture-workspace";
import { exactMillimetreTextToMicrometres } from "@/lib/workshop-production-context";
import styles from "./furniture-workspace.module.css";

const stockKey = (stock: Pick<FurnitureMaterialStock, "material_id" | "material_version" | "measured_thickness_um">) =>
  `${stock.material_id}:${stock.material_version}:${stock.measured_thickness_um}`;
const mm = (value: number) => (value / 1_000).toLocaleString("sv-SE", { maximumFractionDigits: 3 });

export function FurnitureStockEditor({ workspace, inputDrafts, onChange, onInputError }: {
  workspace: FurnitureWorkspace;
  inputDrafts: Record<string, string>;
  onChange: (selection: FurnitureManufacturingSelection, field?: string) => void;
  onInputError: (field: string, reason: unknown, raw: string) => void;
}) {
  const selection = workspace.manufacturing;
  if (!selection) return null;
  const { material, back_material: back, intent } = workspace.design;
  const materials = [material, ...(intent.family === "chest_of_drawers"
    || (intent.family === "shelving" && intent.back_panel !== "none") ? [back] : [])];
  const groups = [...new Map(materials.map(m => {
    const binding = { material_id: m.material_id, material_version: m.version,
      measured_thickness_um: m.measured_thickness_um };
    return [stockKey(binding), binding] as const;
  })).values()];
  const overrides = selection.material_stocks ?? [];
  const setOverrides = (next: FurnitureMaterialStock[], field: string) => {
    if (next.length > 16) {
      onInputError(field, new Error("Högst 16 egna råformat kan sparas. Ta bort ett oanvänt råformat och försök igen, eller återställ profilförslaget."), "");
      return;
    }
    const result: FurnitureManufacturingSelection = { ...selection, material_stocks: next };
    if (!next.length) delete result.material_stocks;
    onChange(result, field);
  };
  const fields = (stock: FurnitureStockFormat, prefix: string, apply: (stock: FurnitureStockFormat, field?: string) => void) => <>
    {([
      ["width", "stock_width_um", "Råskivans bredd (mm)", 1, 6_000_000],
      ["height", "stock_height_um", "Råskivans höjd (mm)", 1, 6_000_000],
      ["margin", "edge_margin_um", "Kantmarginal per sida (mm)", 0, 100_000],
    ] as const).map(([suffix, key, label, minimumUm, maximumUm]) => {
      const field = `${prefix}.${suffix}`;
      return <label key={key}>{label}<input type="number" step="0.001" min={minimumUm/1_000} max={maximumUm/1_000}
        value={inputDrafts[field] ?? (stock[key] ?? 0)/1_000} onChange={e => {
          try { apply({ ...stock, [key]: exactMillimetreTextToMicrometres(e.target.value, { minimumUm, maximumUm }) }, field); }
          catch (reason) { onInputError(field, reason, e.target.value); }
        }} /></label>;
    })}
    <label>Fiberriktning på råskivan<select value={stock.stock_grain_axis ?? ""}
      onChange={e => apply({ ...stock, stock_grain_axis: e.target.value === "x" ? "x" : e.target.value === "y" ? "y" : null })}>
      <option value="">Inte angiven</option><option value="x">Längs bredden</option><option value="y">Längs höjden</option>
    </select></label>
  </>;
  return <div className={styles.stockEditor}>
    <div role="group" aria-label="Gemensamt råformat">
      <h3>Gemensamt råformat</h3>
      <p>Gäller material utan eget format. Bredd är maskinens X-led, höjd är Y-led.</p>
      {fields(selection, "manufacturing", (stock, field) => onChange({ ...selection, ...stock }, field))}
    </div>
    {groups.map(binding => {
      const key = stockKey(binding);
      const override = overrides.find(stock => stockKey(stock) === key);
      const prefix = `manufacturing.material:${key}`;
      const label = `${binding.material_id} · ${mm(binding.measured_thickness_um)} mm`;
      return <div className={styles.stockFormat} key={key} role="group" aria-label={label}>
        <label><input type="checkbox" checked={!!override} onChange={e => {
          const remaining = overrides.filter(stock => stockKey(stock) !== key);
          setOverrides(e.target.checked ? [...remaining, { ...binding,
            stock_width_um: selection.stock_width_um, stock_height_um: selection.stock_height_um,
            stock_grain_axis: selection.stock_grain_axis, edge_margin_um: selection.edge_margin_um ?? 0 }] : remaining, prefix);
        }} />Eget råformat för {label}</label>
        {override ? fields(override, prefix, (stock, field) => setOverrides(overrides.map(item =>
          stockKey(item) === key ? { ...binding, ...stock } : item), field ?? `${prefix}.grain`))
          : <p>Använder gemensamt råformat.</p>}
      </div>;
    })}
    {overrides.filter(stock => !groups.some(group => stockKey(stock) === stockKey(group))).map(stock =>
      <div className={styles.stockFormat} key={stockKey(stock)}>
        <p>Råformat för {stock.material_id} · {stock.material_version} · {mm(stock.measured_thickness_um)} mm
          saknar motsvarande material och tjocklek i möbeln.</p>
        <button type="button" onClick={() => setOverrides(overrides.filter(item => stockKey(item) !== stockKey(stock)),
          `manufacturing.material:${stockKey(stock)}`)}>Ta bort oanvänt råformat</button>
      </div>)}
  </div>;
}

export function FurnitureStockPlan({ manufacturing }: { manufacturing: FurniturePreview["manufacturing"] }) {
  if (!manufacturing.stock_groups?.length) return null;
  return <div className={styles.stockPlan}>
    <h3>Råformat per material</h3>
    <p>{manufacturing.format_scope}</p>
    {manufacturing.stock_groups.map(group => <details key={stockKey(group)}>
      <summary>{group.material_id} · {mm(group.measured_thickness_um)} mm · {group.geometry_compatible
        ? "Delarna ryms enskilt" : "Format behöver granskas"}</summary>
      <p>{group.selection_source === "material" ? "Eget format" : "Gemensamt format"}: {mm(group.stock.stock_width_um)} × {mm(group.stock.stock_height_um)} mm.
        Kantmarginal {mm(group.stock.edge_margin_um ?? 0)} mm per sida.</p>
      {group.required_formats.length ? <><p>Minimiformat (X × Y) för att varje del ska rymmas enskilt:</p>
        <ul>{group.required_formats.map(format => <li key={`${format.stock_width_um}-${format.stock_height_um}`}>
          {mm(format.stock_width_um)} × {mm(format.stock_height_um)} mm · {format.fits_machine
            ? "inom valt arbetsområde" : "kräver större arbetsområde"}</li>)}</ul></>
        : <p>Ange fiberriktning för att beräkna minimiformat.</p>}
      {group.parts.filter(part => part.fits_stock !== true).map(part => <div key={part.part_id}>
        <strong>{part.semantic_key}</strong>
        {part.orientations.length ? <ul>{part.orientations.map(option => <li key={option.rotation_deg}>
          {option.rotation_deg}°: kräver {mm(option.required_stock_width_um)} × {mm(option.required_stock_height_um)} mm.
          Saknas i X: {mm(option.width_shortfall_um)} mm; i Y: {mm(option.height_shortfall_um)} mm.
        </li>)}</ul> : <p>Fiberriktning saknas.</p>}
      </div>)}
    </details>)}
  </div>;
}
