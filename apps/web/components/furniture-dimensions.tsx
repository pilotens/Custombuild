"use client";

import { useState } from "react";
import { INSTALLATION_ALLOWANCES, installationCarcassDimensions, parseFurniturePercentages } from "@/lib/furniture-dimensions";
import type { FurnitureInstallation, FurniturePreview, FurnitureWorkspace } from "@/lib/furniture-workspace";
import { exactMillimetreTextToMicrometres } from "@/lib/workshop-production-context";

type EditorProps = {
  workspace: FurnitureWorkspace;
  onChange: (value: FurnitureWorkspace, field: string) => void;
  onError: (message: string, field: string) => void;
};

export function InstallationEditor({ workspace, onChange, onError }: EditorProps) {
  const installation = workspace.design.installation;
  const apply = (value: FurnitureInstallation | null, field = "installation") => onChange({ ...workspace,
    design: { ...workspace.design, installation: value } }, field);
  const changeMm = (key: keyof FurnitureInstallation, raw: string, nullable = false) => {
    if (!installation) return;
    try {
      const value = nullable && !raw.trim() ? null : exactMillimetreTextToMicrometres(raw,
        { minimumUm: nullable ? 0 : 1, maximumUm: nullable ? 500_000 : 6_000_000 });
      apply({ ...installation, [key]: value }, `installation.${key}`);
    } catch (reason) { onError(reason instanceof Error ? reason.message : "Kontrollera måttet.", `installation.${key}`); }
  };
  let dimensions: ReturnType<typeof installationCarcassDimensions> = null;
  try { if (installation) dimensions = installationCarcassDimensions(installation); } catch { /* Server reports invalid geometry. */ }
  return <details>
    <summary>Kundens yttermått och listutrymme</summary>
    <label><input type="checkbox" checked={Boolean(installation)} onChange={e => apply(e.target.checked ? {
      width_um: workspace.design.intent.width_um, height_um: workspace.design.intent.height_um,
      depth_um: workspace.design.intent.depth_um, width_includes_trim: false,
      left_allowance_um: null, right_allowance_um: null, top_allowance_um: null,
      bottom_allowance_um: null, front_allowance_um: null, rear_allowance_um: null,
    } : null)} />Ange separata kundmått</label>
    {installation ? <>
      <p>Stommåtten ovan styr CAD-delarna. Kundmåtten nedan inkluderar det utrymme som reserveras för list och montage.</p>
      {([['width_um', 'Kundlängd inklusive reserverat utrymme'], ['height_um', 'Kundhöjd'], ['depth_um', 'Kunddjup']] as const).map(([key, label]) =>
        <label key={key}>{label} (mm)<input type="number" min="1" max="6000" step="0.001"
          value={installation[key]/1_000} onChange={e => changeMm(key, e.target.value)} /></label>)}
      <label><input type="checkbox" checked={installation.width_includes_trim}
        onChange={e => apply({ ...installation, width_includes_trim: e.target.checked })} />Längden inkluderar list</label>
      <p>Reserverat utrymme per sida. Tomt betyder okänt. Ange 0 där inget utrymme ska reserveras.</p>
      {INSTALLATION_ALLOWANCES.map(([key, label]) => <label key={key}>{label} · reserverat (mm)
        <input type="number" min="0" max="500" step="0.001" value={installation[key] === null ? "" : installation[key]/1_000}
          onChange={e => changeMm(key, e.target.value, true)} /></label>)}
      <button type="button" disabled={!dimensions} onClick={() => {
        if (dimensions) onChange({ ...workspace, design: { ...workspace.design,
          intent: { ...workspace.design.intent, ...dimensions } } }, "carcass");
      }}>Räkna om stommen från kundmåtten</button>
      {dimensions ? <p>Beräknad stomme: {dimensions.width_um/1_000} × {dimensions.height_um/1_000} × {dimensions.depth_um/1_000} mm (längd × höjd × djup).</p> : null}
      {installation.width_includes_trim ? <p>Listdelar och deras mekaniska infästning saknas ännu i modellen. Reserverat utrymme är inte en listkonstruktion.</p> : null}
    </> : null}
  </details>;
}

function PercentageEditor({ label, values, count, kind, onChange, onError }: {
  label: string; values: number[]; count: number; kind: "bays" | "shelves";
  onChange: (value: number[]) => void; onError: (message: string) => void;
}) {
  const canonical = values.map(v => v/10_000).join("; ");
  const [draft, setDraft] = useState({ raw: canonical, base: canonical });
  return <label>{label}<input type="text" value={draft.base === canonical ? draft.raw : canonical} placeholder="Tomt = jämn fördelning"
    onChange={e => {
      setDraft({ raw: e.target.value, base: canonical });
      try { onChange(parseFurniturePercentages(e.target.value, count, kind)); }
      catch (reason) { onError(reason instanceof Error ? reason.message : "Kontrollera indelningen."); }
    }} /></label>;
}

export function ShelvingLayoutEditor({ workspace, onChange, onError }: EditorProps) {
  const intent = workspace.design.intent;
  if (intent.family !== "shelving") return null;
  const patch = (value: Partial<typeof intent>, field: string) => onChange({ ...workspace, design: { ...workspace.design,
    intent: { ...intent, ...value } } }, field);
  return <details><summary>Fack, hyllhöjder och rygg</summary>
    <p>Ange procent med semikolon mellan värdena. Fackbredderna ska summera till 100 % av den fria bredden.</p>
    <PercentageEditor label="Fackbredder (%)" values={intent.bay_width_ratios_ppm ?? []} count={(intent.divider_count ?? 0)+1}
      kind="bays" onChange={bay_width_ratios_ppm => patch({ bay_width_ratios_ppm }, "layout.bays")}
      onError={message => onError(message, "layout.bays")} />
    <p>Hyllcentrens höjd mäts från botten av den fria hyllzonen. Proportionerna följer med när kunden ändrar totalhöjden.</p>
    <PercentageEditor label="Hyllcentrum från botten (%)" values={intent.shelf_height_ratios_ppm ?? []} count={intent.shelf_count ?? 0}
      kind="shelves" onChange={shelf_height_ratios_ppm => patch({ shelf_height_ratios_ppm }, "layout.shelves")}
      onError={message => onError(message, "layout.shelves")} />
    <label>Hyllornas infästning<select value={intent.shelf_mount ?? "fixed"}
      onChange={e => patch({ shelf_mount: e.target.value as "fixed" | "adjustable" }, "layout.mount")}>
      <option value="fixed">Fasta hyllor</option><option value="adjustable">Flyttbara hyllor med hyllbärare</option>
    </select></label>
    <label>Ryggkonstruktion<select value={intent.back_panel ?? "inset_groove"}
      onChange={e => patch({ back_panel: e.target.value as "none" | "surface_mounted" | "inset_groove" }, "layout.back")}>
      <option value="inset_groove">Rygg i spår</option><option value="none">Utan rygg</option>
      <option value="surface_mounted">Utanpåliggande rygg</option>
    </select></label>
    <label>Sockelhöjd (mm)<input type="number" min="0" max="300" step="0.001" value={(intent.plinth_height_um ?? 0)/1_000}
      onChange={e => { try { patch({ plinth_height_um: exactMillimetreTextToMicrometres(e.target.value, { minimumUm: 0, maximumUm: 300_000 }) }, "layout.plinth"); }
        catch (reason) { onError(reason instanceof Error ? reason.message : "Kontrollera sockelhöjden.", "layout.plinth"); } }} /></label>
  </details>;
}

export function FurnitureStockRequirements({ preview }: { preview: FurniturePreview }) {
  const handoff = preview.workshop_handoff;
  if (!handoff) return null;
  return <details><summary>Råformat att stämma av med verkstaden</summary>
    <p>Måtten kommer från CAD-delarna. Verkstaden behöver lägga till kantmarginal, verktygsutrymme och spill vid nesting. Långa delar skarvas inte automatiskt.</p>
    {handoff.stock_requirements.map(group => <section key={`${group.material_id}-${group.measured_thickness_um}`}>
      <h3>{group.material_id} · {group.measured_thickness_um/1_000} mm · {group.part_count} delar</h3>
      <p>Råämnenas sammanlagda yta: {(group.raw_area_um2/1e12).toFixed(3)} m². Detta är ingen beställningskvantitet.</p>
      {group.minimum_along_grain_um ? <p>Formatet behöver minst {group.minimum_along_grain_um/1_000} mm längs fibern och {group.minimum_across_grain_um/1_000} mm tvärs fibern för att varje råämne ska få plats.</p>
        : <p>Minst {group.minimum_long_edge_um/1_000} × {group.minimum_short_edge_um/1_000} mm för att varje råämne ska få plats med fri rotation.</p>}
      <table><caption>Delarnas råmått i millimeter</caption><thead><tr><th>Del</th><th>U</th><th>V</th><th>Fiber</th></tr></thead>
        <tbody>{group.parts.map(part => <tr key={part.part_id}><td>{part.semantic_key}</td>
          <td>{part.raw_width_um/1_000}</td><td>{part.raw_height_um/1_000}</td>
          <td>{part.grain_direction === "NONE" ? "Ingen riktning" : part.grain_direction === "X" ? "Längs U" : "Längs V"}</td></tr>)}</tbody></table>
    </section>)}
  </details>;
}
