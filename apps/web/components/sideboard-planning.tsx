"use client";

import { useState } from "react";
import type { components } from "@/lib/api-schema";
import css from "./measured-register.module.css";

type Schemas = components["schemas"];
type Review = Schemas["SideboardReviewInputs"];
type Assessment = Schemas["SideboardAssessment"];
const defaultShares = [21, 17, 23, 20, 19];

function numberField(form: FormData, name: string): number {
  const value = form.get(name);
  if (typeof value !== "string" || !value.trim() || !Number.isFinite(Number(value))) {
    throw new Error("Fyll i alla valda mått och laster.");
  }
  return Number(value);
}

export async function readSideboardPlan(form: FormData): Promise<{
  shelf_pin_article?: "282.43.905";
  front_percent?: number[];
  review: Review;
}> {
  const review: Review = {
    shelf_payload_kg: numberField(form, "shelf_payload"),
    top_payload_kg: numberField(form, "top_payload"),
    horizontal_force_n: numberField(form, "horizontal_force"),
    edge_error_mm: numberField(form, "edge_error"),
    bore_depth_error_mm: numberField(form, "bore_error"),
    hinge_paths: [],
  };
  if (form.has("base_source")) {
    review.base_test = {
      assembly_sha256: String(form.get("base_hash")).trim(), batch: String(form.get("base_batch")).trim(),
      source: String(form.get("base_source")).trim(), case: "S1-closed-combined-v1",
      total_vertical_n: numberField(form, "base_vertical"),
      horizontal_n: numberField(form, "base_horizontal"),
    };
  }
  if (form.has("material_source")) {
    review.shelf_material = {
      source: String(form.get("material_source")).trim(),
      density_kg_m3: numberField(form, "density"),
      service_modulus_mpa: numberField(form, "service_modulus"),
      allowable_bending_mpa: numberField(form, "allowable_bending"),
    };
  }
  if (form.has("batch")) {
    review.measurements = {
      batch: String(form.get("batch")).trim(),
      back_mm: numberField(form, "back_mm"),
      shelf_divider_mm: numberField(form, "shelf_divider_mm"),
      front_mm: numberField(form, "front_mm"),
      carcass_mm: numberField(form, "carcass_mm"),
    };
  }
  if (form.get("pin_n") || form.get("pin_source")) {
    review.pin_working_load_n = numberField(form, "pin_n");
    review.pin_source = String(form.get("pin_source")).trim();
  }
  const profile = form.get("hinge_file");
  if (profile instanceof File && profile.size > 0) {
    if (profile.size > 65536) throw new Error("Rörelseprofilen får vara högst 64 KiB.");
    let paths: unknown;
    try { paths = JSON.parse(await profile.text()); }
    catch { throw new Error("Rörelseprofilen måste vara en giltig JSON-fil."); }
    if (!Array.isArray(paths) || !paths.length || paths.length > 2)
      throw new Error("Rörelseprofilen ska innehålla en eller två artikelbanor.");
    review.hinge_paths = paths; // The API validates the complete shape and mounting configuration.
  }
  const support = form.get("pin_article") === "282.43.905" ? { shelf_pin_article: "282.43.905" as const } : {};
  if (!form.has("share_1")) return { review, ...support };
  const shares = defaultShares.map((_, i) => numberField(form, "share_" + (i + 1)));
  if (shares.reduce((a, b) => a + b, 0) !== 100) throw new Error("Luckandelarna måste summera till 100 %.");
  return { front_percent: shares, review, ...support };
}

function NumericField({ label, name, value, min, max, step = "any", optional = false }: {
  label: string; name: string; value?: number; min: number; max: number; step?: string; optional?: boolean;
}) {
  return <label>{label} <input type="number" name={name} defaultValue={value}
    min={min} max={max} step={step} required={!optional} /></label>;
}

export function SideboardPlanningFields({ invalidate }: { invalidate: () => void }) {
  const [pin, setPin] = useState("282.43.905");
  const [custom, setCustom] = useState(false);
  const [base, setBase] = useState(false);
  const [batch, setBatch] = useState(false);
  const [material, setMaterial] = useState(false);
  return <div className={css.planning} onChange={invalidate}>
    <label>Luckindelning <select aria-label="Luckindelning" value={custom ? "custom" : "standard"}
      onChange={e => setCustom(e.target.value === "custom")}>
      <option value="standard">Lika breda invändiga fack</option>
      <option value="custom">Egna luckandelar utifrån referensen</option>
    </select></label>
    {custom && <fieldset><legend>Andel av de fem luckornas träbredd</legend>
      <p>Förslaget 21 / 17 / 23 / 20 / 19 % är avrundat från fotografiet. Springorna räknas separat.</p>
      <div className={css.toolbar}>{defaultShares.map((value, i) => <NumericField key={i}
        label={`Lucka ${i+1} (%)`} name={`share_${i+1}`} value={value} min={10} max={32} step="1" />)}</div>
    </fieldset>}
    <details><summary>Provlaster, verkstadsmått och gångjärn</summary>
      <div className={css.toolbar}>
        <NumericField label="Nyttolast per hyllplan (kg)" name="shelf_payload" value={20} min={0} max={200}/>
        <NumericField label="Topplast vid kant (kg)" name="top_payload" value={10} min={0} max={200}/>
        <NumericField label="Horisontell kraft vid överkant (N)" name="horizontal_force" value={50} min={0} max={1000}/>
        <NumericField label="Lägesfel per färdig kant (± mm)" name="edge_error" value={0.2} min={0} max={3}/>
        <NumericField label="Borrdjupsfel (± mm)" name="bore_error" value={0.3} min={0} max={3}/>
      </div>
      <label><input type="checkbox" checked={material} onChange={e => setMaterial(e.target.checked)}/>
        Använd leverantörens materialdata för hyllplanen</label>
      {material && <div className={css.toolbar}>
        <label>Material och källa <input name="material_source" required maxLength={160}/></label>
        <NumericField label="Densitet (kg/m³)" name="density" min={100} max={1800}/>
        <NumericField label="Effektiv E-modul i fiberriktning inkl. krypning (MPa)" name="service_modulus" min={1} max={50000}/>
        <NumericField label="Tillåten böjspänning vid brukslast (MPa)" name="allowable_bending" min={0.001} max={200}/>
      </div>}
      <label><input type="checkbox" checked={batch} onChange={e => setBatch(e.target.checked)}/>
        Ange uppmätt skivbatch</label>
      {batch && <div className={css.toolbar}>
        <label>Batchbeteckning <input name="batch" required maxLength={120}/></label>
        {[["Rygg", "back_mm"], ["Hyllplan och mellanvägg", "shelf_divider_mm"],
          ["Front", "front_mm"], ["Stomme", "carcass_mm"]].map(([label, name]) =>
          <NumericField key={name} label={label+" (mm)"} name={name!} min={1} max={60}/>)}
      </div>}
      <label>Hyllbärare <select name="pin_article" value={pin} onChange={e=>setPin(e.target.value)}>
        <option value="282.43.905">Häfele 282.43.905 med krage</option>
        <option value="legacy">Cylindrisk Ø5 × 12 mm med egen lastuppgift</option>
      </select></label>
      {pin === "282.43.905" ? <p>Fyra bärare per hylla. Beräkningen jämför hyllans egenvikt och jämnt fördelade nyttolast med artikelns kataloggräns.</p> : <div className={css.toolbar}>
        <NumericField label="Tillåten brukslast per hyllbärare (N)" name="pin_n" min={0.01} max={10000} optional/>
        <label>Hyllbärarens artikel och datakälla <input name="pin_source" maxLength={120}/></label>
      </div>}
      <label><input type="checkbox" checked={base} onChange={e=>setBase(e.target.checked)}/>
        Jämför med underredets provprotokoll</label>
      {base && <fieldset><legend>Dokumenterat kombinerat provfall</legend>
        <p>Protokollet ska gälla samma montering och skivbatch med sex bärande fötter, stängda luckor,
          jämnt belastade hyllor, topplast vid ogynnsam kant och samtidig sidokraft vid överkant.</p>
        <div className={css.toolbar}>
          <label>Protokollreferens <input name="base_source" required maxLength={160}/></label>
          <label>Provad monterings SHA-256 <input name="base_hash" required pattern="[a-f0-9]{64}"/></label>
          <label>Provad skivbatch <input name="base_batch" required maxLength={120}/></label>
          <NumericField label="Provad total vertikallast inklusive egenvikt (N)" name="base_vertical" min={0.01} max={100000}/>
          <NumericField label="Samtidigt provad sidokraft (N)" name="base_horizontal" min={0} max={10000}/>
        </div>
      </fieldset>}
      <label>Rörelseprofil för gångjärn (.json) <input type="file" name="hinge_file" accept=".json,application/json"/></label>
      <small>Valfritt. Profilen anger läget för luckans främre vänstra hörn från 0° till 110°.
        Utan profil redovisas katalogens springkrav och vilken bana som saknas.</small>
    </details>
  </div>;
}

const motionLabels = { clear_in_model: "Fri längs angiven modellbana", missing_path: "Artikelbana saknas",
  envelope_contact: "Kontur eller tolerans träffar en motdel", inconclusive: "Otillräcklig säker marginal" };

export function SideboardPlanningResult({ report }: { report: Assessment }) {
  const mm = (v: number) => v.toLocaleString("sv-SE", {maximumFractionDigits: 2});
  return <section className={css.planning} aria-label="Konstruktionsprov">
    <h3>Konstruktionsprov</h3>
    <p>{mm(report.total_shelf_payload_kg)} kg vald nyttolast fördelad över fem hyllplan.
      Tabellen antar jämnt utbredd last. Materialunderlag: {report.material_profile}.</p>
    <table><thead><tr><th>Hyllplan</th><th>Nedböjning / gräns (mm)</th><th>Böjspänning</th><th>Hyllbärare</th></tr></thead>
      <tbody>{report.shelf_screens.map(s => <tr key={s.part_id}><td>{s.part_id}</td>
        <td>{mm(s.deflection_mm)} / {mm(s.limit_mm)} · {s.deflection_within_limit ? "inom gräns" : "överskriden"}</td>
        <td>{mm(s.bending_stress_mpa)} MPa · {s.bending_within_declared_limit === null ? "gräns saknas" :
          s.bending_within_declared_limit ? "inom angiven gräns" : "överskriden"}</td>
        <td>{s.support_set ? <>{s.support_set.article} · {mm(s.support_set.total_shelf_mass_kg)} / {mm(s.support_set.set_limit_kg)} kg för fyra · {s.support_set.within_set_limit ? "inom kataloggräns" : "över kataloggräns"}</> :
          <>{mm(s.support_demand_n)} N · {s.pin_within_declared_load === null ? "artikeldata saknas" :
          s.pin_within_declared_load ? "inom angiven kapacitet" : "för hög last"}</>}</td></tr>)}</tbody>
    </table>
    <table><thead><tr><th>Lucka</th><th>Minsta springa / krav (mm)</th><th>Öppning 0–110°</th></tr></thead>
      <tbody>{report.hinge_gaps.map(g => {
        const m = report.door_motion.find(row => row.door_id === g.part_id);
        return <tr key={g.part_id}><td>{g.part_id}</td><td>{mm(g.available_mm)} / {g.required_mm == null ? "utanför tabell" : mm(g.required_mm)}</td>
          <td>{m && motionLabels[m.state]} {m?.obstacle && `· ${m.obstacle} vid ${mm(m.angle_deg ?? 0)}°`}</td></tr>;
      })}</tbody>
    </table>
    <p>Minsta kvarvarande trä bakom hyllbärarnas hål i mellanväggarna: {mm(report.minimum_divider_web_mm)} mm.
      Kvar till luckans framsida: {mm(report.minimum_front_skin_mm)} mm.</p>
    <p>Skivbatch: {{ missing: "mätvärden saknas", matches_nominal: "matchar ritmåtten",
      revise_dimensions: "avviker från ritmåtten" }[report.batch_state]}.
      CAD-exporten beräknar tippmoment för fyra riktningar med stängda luckor.</p>
    <ul>{report.findings.map(s => <li key={s}>{s}</li>)}</ul>
  </section>;
}

export function SideboardStabilityResult({report}: { report: Schemas["ClosedStability"] }) {
  return <section className={css.planning} aria-label="Tippkontroll från CAD">
    <h3>Statisk tippkontroll</h3>
    <p>{report.positive_reserve ? "Positiv momentreserv vid alla fyra kanter i provfallen." :
      "Minst ett provfall når eller passerar tippgränsen."} {report.basis}</p>
    <p>Vertikallast inklusive trämassa: {report.total_vertical_n.toFixed(1)} N.
      Underredets provfall: {{ missing: "protokoll saknas", wrong_assembly: "annan monteringsrevision",
        wrong_batch: "annan eller obekräftad skivbatch", outside_recorded_case: "vald last över provfallet",
        within_recorded_case: "lasten ryms inom angivet provfall" }[report.base_test_comparison]}.</p>
    <table><thead><tr><th>Tippkant</th><th>Minsta momentreserv (Nm)</th></tr></thead>
      <tbody>{report.edges.map(row => <tr key={row.edge}><td>{row.edge}</td>
        <td>{row.minimum_reserve_nm.toLocaleString("sv-SE", {maximumFractionDigits: 2})}</td></tr>)}</tbody>
    </table>
  </section>;
}
