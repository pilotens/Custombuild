"use client";

import { useRef, useState } from "react";
import { CustombuildApiClient } from "@/lib/api-client";
import type { components } from "@/lib/api-schema";
import type { MeasuredResult, MeasuredWork } from "@/lib/measured-assembly";
import type { PublicRuntimeConfig } from "@/lib/runtime-config";
import css from "./measured-register.module.css";
import { TrestleExport } from "./trestle-export";

export function SideboardConstruction({runtimeConfig,open,openVerified}: {
  runtimeConfig: PublicRuntimeConfig;
  open: (work: MeasuredWork) => void;
  openVerified: (report: MeasuredResult) => void;
}) {
  const [values,setValues]=useState(["2400","450","750","180"]);
  const [confirmed,setConfirmed]=useState(false);
  const [result,setResult]=useState<components["schemas"]["SideboardConstruction"]>();
  const [proof,setProof]=useState<MeasuredResult>();
  const [error,setError]=useState("");const [busy,setBusy]=useState(false);const serial=useRef(0);
  function invalidate(){++serial.current;setResult(undefined);setProof(undefined);setError("");setBusy(false)}
  async function construct(){
    const epoch=++serial.current;setBusy(true);setError("");setResult(undefined);setProof(undefined);
    try{
      const client=new CustombuildApiClient(runtimeConfig.apiUrl,undefined,runtimeConfig.developmentToken);
      const next=await client.constructSideboard({width_mm:Number(values[0]),depth_mm:Number(values[1]),height_mm:Number(values[2]),leg_height_mm:Number(values[3]),
        door_count:5,material:"walnut-veneered-plywood",dimensions_confirmed:confirmed});
      if(next.physical_cutting_authorized!==false||!/^[a-f0-9]{64}$/.test(next.construction_sha256)||!/^[a-f0-9]{64}$/.test(next.assembly_sha256))throw Error("Konstruktionens identitet saknas.");
      if(epoch===serial.current)setResult(next);
    }catch(reason){if(epoch===serial.current)setError(reason instanceof Error?reason.message:"Skänken kunde inte skapas.")}
    finally{if(epoch===serial.current)setBusy(false)}
  }
  return <section aria-labelledby="sideboard-title">
    <h2 id="sideboard-title">Konstruera skänken med fem luckor</h2>
    <p>S1 följer referensens låga proportioner, fem släta fronter och smala springor. Valnötsfanerad plywood med ben och underrede i massiv valnöt.
      Två extra mittben stödjer den långa stommen. Måtten är valda provmått.</p>
    <form onSubmit={e=>{e.preventDefault();void construct()}}><div className={css.toolbar}>
      {[["Skänkens bredd (mm)",1800,2600],["Skänkens djup (mm)",400,550],["Skänkens höjd (mm)",650,850],["Benhöjd (mm)",140,220]].map(([label,min,max],i)=><label key={label}>{label}
        <input type="number" min={min} max={max} step="1" required value={values[i]} onChange={e=>{invalidate();setValues(prev=>prev.map((v,j)=>j===i?e.target.value:v))}} /></label>)}
    </div><label><input type="checkbox" checked={confirmed} onChange={e=>{invalidate();setConfirmed(e.target.checked)}} /> Jag har valt skänkens yttermått</label>{" "}
      <button type="submit" disabled={busy}>{busy?"Konstruerar skänk …":"Skapa skänk"}</button></form>
    {error&&<p role="alert">{error}</p>}
    {result&&<div aria-live="polite">
      <p><strong>{result.assembly.parts.length} trädelar · {result.assembly.joints.length} plugg-/skruvförband · {result.fittings.length} beslagsposter · {result.assembly.parts.reduce((n,p)=>n+p.operations.length,0)} bearbetningar</strong></p>
      <p>Fem heltäckande ryggfält och fem lösa hyllplan. Tio fjäderlösa Blum-gångjärn, fem långa TIP-ON och 20 hyllbärare.
        Alla har namngivna motdelar och borrningar. Borrbildskontroll är separat från full gångjärnsrörelse och bärförmåga.</p>
      <button onClick={()=>proof?openVerified(proof):open(result.assembly)}>{proof?"Öppna skänkens CAD-kontrollerade delar":"Öppna skänkens delar"}</button>
      <details><summary>Material, konstruktion och antaganden</summary><ul>{result.assumptions.map(a=><li key={a}>{a}</li>)}</ul></details>
      <details><summary>Skänkens kapbehov och beslag</summary><table><thead><tr><th>Del</th><th>Minsta inköpsämne L/B/T (mm)</th></tr></thead>
        <tbody>{result.purchase_blanks.map(b=><tr key={b.part_id}><td>{b.part_id}</td><td>{b.purchase_min_lbt_um.map(x=>x/1000).join(" × ")}</td></tr>)}</tbody></table>
        <ul>{Object.entries(result.hardware).map(([article,n])=><li key={article}>{n} st · {article}</li>)}</ul></details>
      <details><summary>Skänkens monteringsordning</summary><ol>{result.assembly_sequence.map(s=><li key={s}>{s}</li>)}</ol></details>
      <TrestleExport key={result.construction_sha256} construction={result} runtimeConfig={runtimeConfig} verified={setProof}/>
      <ul>{result.outstanding_acceptance.map(a=><li key={a}>{a}</li>)}</ul>
    </div>}
  </section>;
}
