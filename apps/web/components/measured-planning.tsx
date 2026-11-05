"use client";

import { useState } from "react";
import {
  measuredMm as mm,
  type MeasuredPiece,
  type MeasuredResult,
  type MeasuredWork,
} from "@/lib/measured-assembly";
import { exactMillimetreTextToMicrometres } from "@/lib/workshop-production-context";
import css from "./measured-register.module.css";

const status = { fits: "Ryms nominellt", blocked: "Ryms inte", unresolved: "Ej verifierat" };

function size(data: FormData, names: string[]): [number, number, number] {
  const values = names.map((name) => exactMillimetreTextToMicrometres(
    String(data.get(name) ?? "").replace(",", "."),
    { minimumUm: 1, maximumUm: 20_000_000 },
  ));
  return [values[0]!, values[1]!, values[2]!];
}

function Dimensions({ names, labels, values }: {
  names: string[]; labels: string[]; values?: number[] | null;
}) {
  return names.map((name, i) => <label key={name}>
    {labels[i]} (mm)
    <input name={name} inputMode="decimal" required
      defaultValue={values?.[i] == null ? "" : String(values[i]! / 1000)} />
  </label>);
}

export function MeasuredRoomPlanning({ report, change }: {
  report: MeasuredResult; change: (input: MeasuredWork) => void;
}) {
  const [error, setError] = useState("");
  const room = report.assembly.room;
  const result = report.installation;
  const stocks = report.stock_checks ?? [];
  return <section className={css.sheet} aria-label="Monteringsutrymme">
    <h2>Rum och montering</h2>
    <p>Ange fria mått efter lister och andra hinder. Kontrollen placerar möbelns
      nederkant på golvet och framkant i nischens framkant.</p>
    <form onSubmit={(event) => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      try {
        const dimensions = size(data, ["room-width", "room-depth", "room-height"]);
        const tool = String(data.get("tool") ?? "").trim();
        const rearTool = tool ? exactMillimetreTextToMicrometres(tool.replace(",", "."),
          { minimumUm: 1, maximumUm: 20_000_000 }) : null;
        change({ ...report.assembly,
          room: { size_um: dimensions, rear_tool_depth_um: rearTool,
            source: String(data.get("source") ?? "").trim() },
          changes: [...report.assembly.changes,
            `Rumsprov B/D/H ${dimensions.map(mm).join("/")} mm. Verktygsdjup bak ${rearTool == null ? "ej angivet" : mm(rearTool) + " mm"}.`],
        });
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Kontrollera rumsmåtten.");
      }
    }}>
      <Dimensions names={["room-width", "room-depth", "room-height"]}
        labels={["Fri bredd", "Fritt djup", "Fri takhöjd"]} values={room?.size_um} />
      <label>Verktygsdjup bakom skruvhuvudet (mm, valfritt)
        <input name="tool" inputMode="decimal"
          defaultValue={room?.rear_tool_depth_um == null ? "" : String(room.rear_tool_depth_um / 1000)} />
      </label>
      <label>Måttkälla
        <input name="source" required maxLength={600} defaultValue={room?.source ?? "Inmatade fria rumsmått"} />
      </label>
      <button type="submit">Kontrollera monteringsutrymmet</button>
      {error && <p role="alert">{error}</p>}
    </form>
    {room && <button onClick={() => change({ ...report.assembly, room: null,
      changes: [...report.assembly.changes, "Rumsuppgifterna borttagna. Monteringsutrymmet behöver anges på nytt."],
    })}>Ta bort rumsmåtten</button>}
    {!result ? <p>Monteringsutrymmet är ej verifierat. Ange rumsmåtten ovan.</p> : <>
      <p><b>Slutläge · {status[result.state]}</b></p>
      {result.margin_xyz_um && <p>Återstående bredd / djup / höjd {result.margin_xyz_um.map(mm).join(" / ")} mm.</p>}
      {result.raise_height_um ? <ul>{result.raise_height_um.map((height, i) => <li key={i}>
        Resning {i === 0 ? "från framsidan" : "från baksidan"} · {status[result.raise_states[i]!]}
        {" · "}kräver minst {mm(height)} mm takhöjd.
      </li>)}</ul> : <p>Resningshöjden kunde inte verifieras.</p>}
      <p>Resningen gäller en hel, styv stomme med fritt utrymme framför nischen
        och plant golv och tak. Passage, personer, andra hinder och montering
        i sektioner behöver egen kontroll.</p>
      {result.unresolved_parts.length > 0 && <p className={css.attention}>
        {result.unresolved_parts.length} delar saknar känt läge i hela möbeln.
        Måtten ovan avser de {result.known_parts} placerade delarna.
      </p>}
      {result.rear_access.length > 0 && <details open>
        <summary>Skruvning bakifrån på slutplats ({result.rear_access.length} förband)</summary>
        <p>Kontrollen gäller avståndet till bakväggen. Skruvlängden och angivet
          verktygsdjup behöver få plats samtidigt. Hinder inne i möbeln behöver kontrolleras separat.</p>
        <ul>{result.rear_access.map((entry) => <li key={entry.joint}>
          {entry.part} · {entry.joint} · {status[entry.state]} · {mm(entry.gap_um)} mm
          {" "}till bakvägg, behöver minst {mm(entry.required_um)} mm.
        </li>)}</ul>
        {room?.rear_tool_depth_um == null && <p>Verktygsdjup saknas. Tillräckligt
          utrymme för själva skruven räcker inte för att verifiera monteringen.</p>}
      </details>}
    </>}
    {stocks.length > 0 && <p>Inköpsformat för delarnas enskilda ämnen ·
      {" "}{stocks.filter(s => s.state === "fits").length} ryms,
      {" "}{stocks.filter(s => s.state === "blocked").length} är för stora,
      {" "}{stocks.filter(s => s.state === "unresolved").length} saknar format.</p>}
  </section>;
}

export function MeasuredStockEdit({ report, piece, change }: {
  report: MeasuredResult; piece: MeasuredPiece;
  change: (input: MeasuredWork, part: string) => void;
}) {
  const [error, setError] = useState("");
  function save(stock: [number, number, number] | null) {
    change({ ...report.assembly,
      parts: report.assembly.parts.map(p => p.id === piece.id ? { ...p, stock_lbt_um: stock } : p),
      changes: [...report.assembly.changes,
        `${piece.id}: inköpsformat L/B/T ${stock ? stock.map(mm).join("/") + " mm" : "borttaget"}.`],
    }, piece.id);
  }
  return <details>
    <summary>Ändra inköpsformat för {piece.id}</summary>
    <p>Ange leverantörens mått. Längden följer ådringen. Kontrollen jämför ett
      ämne mot formatet. Antal skivor, sågspår och kapspill behöver en kapplan.</p>
    <form onSubmit={event => {
      event.preventDefault();
      try { save(size(new FormData(event.currentTarget), ["stock-length", "stock-width", "stock-thickness"])); }
      catch (reason) { setError(reason instanceof Error ? reason.message : "Kontrollera inköpsformatet."); }
    }}>
      <Dimensions names={["stock-length", "stock-width", "stock-thickness"]}
        labels={["Inköpslängd", "Inköpsbredd", "Inköpstjocklek"]} values={piece.stock_lbt_um} />
      <button type="submit">Kontrollera inköpsformatet</button>
      {error && <p role="alert">{error}</p>}
    </form>
    {piece.stock_lbt_um && <button onClick={() => save(null)}>Ta bort inköpsformatet</button>}
  </details>;
}
