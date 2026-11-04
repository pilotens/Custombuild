"use client";

import { useRef, useState } from "react";
import type { components } from "@/lib/api-schema";
import { CustombuildApiClient } from "@/lib/api-client";
import type { MeasuredWork, MeasuredResult } from "@/lib/measured-assembly";
import type { PublicRuntimeConfig } from "@/lib/runtime-config";
import css from "./measured-register.module.css";
import { TrestleExport } from "./trestle-export";

type Construction = components["schemas"]["TrestleConstruction"];
const number = (n: number) => n.toLocaleString("sv-SE", { maximumFractionDigits: 2 });

export function TrestleConstruction({ runtimeConfig, open, openVerified }: {
  runtimeConfig: PublicRuntimeConfig;
  open: (work: MeasuredWork) => void;
  openVerified?: (report: MeasuredResult) => void;
}) {
  const [dimensions, setDimensions] = useState(["2200", "950", "750", "300"]);
  const [confirmed, setConfirmed] = useState(false);
  const [result, setResult] = useState<Construction>();
  const [verified, setVerified] = useState<MeasuredResult>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const serial = useRef(0);
  function invalidate() { ++serial.current; setResult(undefined); setVerified(undefined); setError(""); setBusy(false); }
  async function construct() {
    const epoch = ++serial.current;
    setBusy(true); setError(""); setResult(undefined); setVerified(undefined);
    try {
      const client = new CustombuildApiClient(runtimeConfig.apiUrl, undefined, runtimeConfig.developmentToken);
      const next = await client.constructTrestle({
        length_mm: Number(dimensions[0]), width_mm: Number(dimensions[1]), height_mm: Number(dimensions[2]),
        central_load_n: Number(dimensions[3]), dimensions_confirmed: confirmed,
        material: "american-black-walnut", moisture_min_pct: 6, moisture_max_pct: 12,
      });
      if (next.physical_cutting_authorized !== false || !/^[a-f0-9]{64}$/.test(next.assembly_sha256)
        || !/^[a-f0-9]{64}$/.test(next.construction_sha256))
        throw Error("Konstruktionens identitet saknas.");
      if (epoch === serial.current) setResult(next);
    } catch (reason) {
      if (epoch === serial.current) setError(reason instanceof Error ? reason.message : "Konstruktionen kunde inte skapas.");
    } finally { if (epoch === serial.current) setBusy(false); }
  }
  function download() {
    if (!result) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url;
    link.download = "Custombuild-Bockbord-B1-konstruktion.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <section aria-labelledby="trestle-title">
    <h2 id="trestle-title">Konstruera ett bockbord</h2>
    <p>Välj yttermått och provlast. Programmet skapar delar, bultförband, hål, urtag,
      skivinfästning, kapbehov och beslag. Material: massiv amerikansk svartvalnöt.
      B1 är en vald konstruktion inspirerad av bordet, med synliga bultar och sidomonterade tvärbalkar.</p>
    <form onSubmit={e => { e.preventDefault(); void construct(); }}>
      <div className={css.toolbar}>
        {[ ["Längd (mm)", 1800, 2600], ["Bredd (mm)", 850, 1100],
          ["Höjd (mm)", 700, 800], ["Centrisk provlast (N)", 100, 1500] ].map(([label, min, max], i) =>
          <label key={label}>{label}<input type="number" required step="1" min={min} max={max}
            value={dimensions[i]} onChange={e => { invalidate(); setDimensions(prev => prev.map((v,j) => j === i ? e.target.value : v)); }} /></label>)}
      </div>
      <label><input type="checkbox" checked={confirmed} onChange={e => { invalidate(); setConfirmed(e.target.checked); }} />
        Jag har valt dessa yttermått för min möbel</label>{" "}
      <button type="submit" disabled={busy}>{busy ? "Konstruerar …" : "Skapa konstruktion"}</button>
    </form>
    {error && <p role="alert">{error}</p>}
    {result && <div aria-live="polite">
      <p><strong>{result.assembly.parts.length} delar · {result.assembly.joints.length} förband · {result.assembly.parts.reduce((n,p) => n + p.operations.length, 0)} bearbetningar</strong></p>
      <p>Vald skivtjocklek: {result.sizing.top_thickness_mm} mm. Uppskattad nedböjning:
        {" "}{number(result.sizing.predicted_deflection_mm)} mm, mål högst {number(result.sizing.deflection_target_mm)} mm.
        Beräkningen gäller en 300 mm bred skivremsa. Den är inte en lastklassning av bordet.</p>
      <p>Fyra rörelseslitsar: 9 × {result.sizing.mounting_slot_length_mm} mm.
        Beräknad breddändring vid 6–12 % fuktkvot: {number(result.sizing.width_change_mm)} mm.
        En centruminfästning styr skivan.</p>
      <button onClick={() => verified && openVerified ? openVerified(verified) : open(result.assembly)}>
        {verified ? "Öppna CAD-kontrollerade delar" : "Öppna delarna och bearbetningarna"}</button>{" "}
      <button onClick={download}>Spara hela konstruktionsunderlaget</button>
      <details><summary>Kapbehov och beslag</summary>
        <p>Inköpsmåtten inkluderar 20 mm längdmån och 4 mm i bredd och tjocklek. Leverantörens lagerformat är inte antagna.</p>
        <table><thead><tr><th>Del</th><th>Minsta inköpsämne L/B/T (mm)</th><th>Fiberriktning</th></tr></thead>
          <tbody>{result.purchase_blanks.map(p => <tr key={p.part_id}><td>{p.part_id}</td><td>{p.purchase_min_lbt_um.map(v => number(v/1000)).join(" × ")}</td><td>{p.grain}</td></tr>)}</tbody></table>
        <ul>{result.hardware.map(h => <li key={h.specification}>{h.quantity} st · {h.specification}</li>)}</ul>
      </details>
      <details><summary>Montering och beräkningsunderlag</summary>
        <ol>{result.assembly_sequence.map(s => <li key={s}>{s}</li>)}</ol>
        <ul>{result.sizing.assumptions.map(s => <li key={s}>{s}</li>)}</ul>
        <ul>{result.sizing.sources.map(s => <li key={s}><a href={s}>{s}</a></li>)}</ul>
      </details>
      <TrestleExport key={result.construction_sha256} construction={result} runtimeConfig={runtimeConfig} verified={setVerified} />
      <ul>{result.outstanding_acceptance.map(s => <li key={s}>{s}</li>)}</ul>
    </div>}
  </section>;
}
