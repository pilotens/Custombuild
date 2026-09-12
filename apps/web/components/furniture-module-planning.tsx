"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { CustombuildApiClient } from "@/lib/api-client";
import { assertMatchingFurnitureModulePlan, type FurnitureModuleGrid, type FurnitureModulePlan, type FurnitureModuleRequirement } from "@/lib/furniture-module-plan";
import type { FurnitureWorkspace } from "@/lib/furniture-workspace";
import { exactMillimetreTextToMicrometres } from "@/lib/workshop-production-context";
import styles from "./furniture-workspace.module.css";

const mm = (value: number) => (value / 1_000).toLocaleString("sv-SE", { maximumFractionDigits: 3 });
const kg = (value: number) => (value / 1_000).toLocaleString("sv-SE", { maximumFractionDigits: 3 });
const integer = (text: string, minimum: number, maximum: number) =>
  /^\d+$/.test(text) && Number(text) >= minimum && Number(text) <= maximum ? Number(text) : null;

const requirementLabels: Record<string, string> = {
  MODULE_DESIGN_CHANGE_REVIEW_REQUIRED: "Granska de nya stommarna. Varje modul får egna sidor, topp och botten; hyllplaceringar, förvaringsutrymme och vikt ändras.",
  MODULE_CONNECTIONS_REQUIRED: "Bestäm och verifiera förbanden mellan modulerna, inklusive beslag, hål och monteringsordning.",
  WALL_ANCHORAGE_REQUIRED: "Verifiera väggunderlag, väggförankring och sidostabilitet för hela möbeln.",
  STACKING_LOAD_PATH_REQUIRED: "Kontrollera att underliggande moduler och underlaget bär överliggande modulers vikt och innehåll. Denna staplingslast är ännu inte beräknad.",
  VERTICAL_GAP_SUPPORT_REQUIRED: "Bestäm bärande distanser eller upphängning för mellanrummet mellan staplade moduler.",
  MATERIAL_AND_JOINT_QUALIFICATION_REQUIRED: "Verifiera det verkliga materialet och förbanden med ett representativt passningsprov.",
  WORKSHOP_QUALIFICATION_REQUIRED: "Låt verkstaden verifiera skivplacering, uppspänning, fräsprogram och maskin för de nya delarna.",
  SOURCE_INSTALLATION_REVIEW_REQUIRED: "Granska installation och montage för hela modulgruppen. Platsens mått och listkrav följer med i varje arbetsfil.",
  INSTALLATION_MEASUREMENTS_REQUIRED: "Mät plats, montageutrymme och eventuell list innan installationslösningen fastställs.",
};

function requirementText(requirement: FurnitureModuleRequirement) {
  return requirementLabels[requirement.code] ?? requirement.message;
}

function downloadJson(document: unknown, name: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(document, null, 2) + "\n"], { type: "application/json" }));
  try {
    const anchor = window.document.createElement("a");
    anchor.href = url;
    anchor.download = name.replace(/[^a-zA-Z0-9._-]/g, "-");
    window.document.body.appendChild(anchor);
    try { anchor.click(); } finally { anchor.remove(); }
  } finally {
    // Give the browser's download dispatch a turn before releasing the data.
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

type RequestScope = { key: string; api: CustombuildApiClient };
type RequestState = { scope: RequestScope; busy: boolean; result?: FurnitureModulePlan; error?: string };

export function FurnitureModulePlanningPanel({ api, workspace, designHash, disabled }: {
  api: CustombuildApiClient;
  workspace: FurnitureWorkspace;
  designHash: string;
  disabled: boolean;
}) {
  const [columns, setColumns] = useState("2");
  const [rows, setRows] = useState("1");
  const [gap, setGap] = useState("");
  const [dividers, setDividers] = useState("0");
  const [shelves, setShelves] = useState<string[]>(["", "", "", ""]);
  const [requestState, setRequestState] = useState<RequestState>();
  const epoch = useRef(0);
  const helpId = useId();
  const rowCount = integer(rows, 1, 4);
  const columnCount = integer(columns, 1, 8);
  const dividerCount = integer(dividers, 0, 16);
  const sourceShelfCount = workspace.design.intent.shelf_count ?? 4;
  const shelfCounts = shelves.slice(0, rowCount ?? 0).map(value => integer(value, 0, 40));
  let grid: FurnitureModuleGrid | undefined;
  let validation: string;
  if (workspace.design.intent.family !== "shelving") {
    validation = "Modulplanering finns för hyllsystem.";
  } else if (rowCount === null || columnCount === null || dividerCount === null) {
    validation = "Ange 1–8 kolumner, 1–4 rader och 0–16 avdelare per modul.";
  } else if (rowCount * columnCount < 2 || rowCount * columnCount > 16) {
    validation = "Välj sammanlagt 2–16 moduler.";
  } else if (!gap.trim()) {
    validation = "Ange mellanrummet i millimeter. Skriv 0 om modulerna ska stå direkt intill varandra.";
  } else if (shelfCounts.some(value => value === null)) {
    validation = "Ange 0–40 hyllplan per modul för varje rad, nerifrån och upp.";
  } else if (shelfCounts.reduce<number>((sum, value) => sum + (value ?? 0), 0) < sourceShelfCount) {
    validation = `Summan av hyllplanen över raderna måste vara minst ${sourceShelfCount}, som i ursprungsmöbeln.`;
  } else {
    try {
      const gapUm = exactMillimetreTextToMicrometres(gap, { minimumUm: 0, maximumUm: 100_000 });
      grid = { columns: columnCount, rows: rowCount, gap_um: gapUm,
        shelf_count_per_row: shelfCounts as number[], divider_count_per_module: dividerCount };
      validation = `${rowCount * columnCount} moduler. Bredd och höjd fördelas jämnt efter valt mellanrum.`;
    } catch {
      validation = "Ange ett mellanrum på 0–100 mm med högst tre decimaler.";
    }
  }

  // Bind both pending and completed state to all input values. A change hides
  // the old result immediately, even before the effect invalidates its promise.
  const key = JSON.stringify([workspace, designHash, columns, rows, gap, dividers, shelves, disabled]);
  const scope = useMemo(() => ({ key, api }), [key, api]);
  const current = requestState?.scope === scope ? requestState : undefined;
  const busy = current?.busy === true;
  const result = current?.result;
  useEffect(() => () => { epoch.current += 1; }, [scope]);

  const calculate = async () => {
    if (!grid || disabled || busy) return;
    const request = ++epoch.current;
    const requestedGrid = grid;
    setRequestState({ scope, busy: true });
    try {
      const next = await api.planFurnitureModules(workspace, requestedGrid);
      if (request !== epoch.current) return;
      assertMatchingFurnitureModulePlan(next, workspace, designHash, requestedGrid);
      setRequestState({ scope, busy: false, result: next });
    } catch (reason) {
      if (request === epoch.current) setRequestState({ scope, busy: false,
        error: reason instanceof Error && !(reason instanceof TypeError) ? reason.message : "Modulplanen kunde inte beräknas. Försök igen." });
    }
  };

  const canExport = !disabled && !!grid && !!result && result.state === "available"
    && result.can_export_drafts === true && result.modules.length === grid.rows * grid.columns
    && result.layout !== null && result.can_apply === false && result.production_qualified === false
    && result.physical_cutting_authorized === false;
  const save = (document: unknown, name: string) => {
    if (!canExport) return;
    try {
      downloadJson(document, name);
      if (current?.error) setRequestState({ scope, busy: false, result });
    }
    catch { setRequestState({ scope, busy: false, result, error: "Arbetsfilen kunde inte hämtas. Försök igen." }); }
  };
  const intent = workspace.design.intent;

  return <section className={styles.shelfSuggestion} aria-label="Planera stommoduler">
    <h3>Dela upp möbeln i stommoduler</h3>
    <p>Planera separata stommar för att korta genomgående delar. Ursprunglig möbel:
      {" "}{mm(intent.width_um)} × {mm(intent.height_um)} × {mm(intent.depth_um)} mm (bredd × höjd × djup).</p>
    <fieldset disabled={disabled} aria-describedby={helpId}>
      <legend>Välj modulindelning</legend>
      <label>Kolumner, från vänster<input type="number" min="1" max="8" step="1" value={columns}
        onChange={event => setColumns(event.target.value)} /></label>
      <label>Rader, nerifrån och upp<input type="number" min="1" max="4" step="1" value={rows}
        onChange={event => setRows(event.target.value)} /></label>
      <label>Mellanrum mellan moduler (mm)<input type="number" min="0" max="100" step="0.001"
        value={gap} placeholder="Ange även 0 uttryckligen" onChange={event => setGap(event.target.value)} /></label>
      <p>Samma mellanrum används i sidled och mellan staplade rader.</p>
      {Array.from({ length: rowCount ?? 0 }, (_, row) => <label key={row}>
        Hyllplan per modul, rad {row + 1}{row === 0 ? " (nederst)" : ""}
        <input type="number" min="0" max="40" step="1" value={shelves[row] ?? ""}
          onChange={event => setShelves(previous => previous.map((value, index) => index === row ? event.target.value : value))} />
      </label>)}
      <label>Invändiga avdelare per modul<input type="number" min="0" max="16" step="1" value={dividers}
        onChange={event => setDividers(event.target.value)} /></label>
      <p id={helpId}>{validation}</p>
      <button type="button" disabled={disabled || busy || !grid} onClick={() => { void calculate(); }}>
        {busy ? "Beräknar modulplan…" : "Beräkna modulplan"}
      </button>
    </fieldset>
    {current?.error ? <p role="alert" className={styles.error}>{current.error}</p> : null}
    {result && !disabled ? <div className={styles.proposal} aria-live="polite">
      <p><strong>{result.message}</strong></p>
      {result.state === "available" && result.layout ? <>
        <p>Totalmått: {mm(result.layout.envelope.width_um)} × {mm(result.layout.envelope.height_um)} × {mm(result.layout.envelope.depth_um)} mm.
          {" "}Modulernas beräknade sammanlagda vikt: {kg(result.modules.reduce((sum, module) => sum + module.weight_g, 0))} kg.</p>
        <p>Mellanrum: {mm(result.grid.gap_um)} mm. Rad 1 är nederst, kolumn 1 längst till vänster.</p>
        <div className={styles.tableScroll}>
          <svg role="img" aria-label="Modulindelning sedd framifrån; nedersta raden är rad 1"
            viewBox={`0 0 ${result.layout.envelope.width_um / 1_000} ${result.layout.envelope.height_um / 1_000}`}
            style={{ display: "block", width: "100%", maxHeight: 420, minWidth: 320 }}>
            <title>Modulernas mått och mellanrum visas i samma skala</title>
            {result.modules.map(module => {
              const width = module.dimensions_um.width_um / 1_000, height = module.dimensions_um.height_um / 1_000;
              const x = module.placement.x_um / 1_000;
              const y = (result.layout!.envelope.height_um - module.placement.z_um - module.dimensions_um.height_um) / 1_000;
              const fontSize = Math.min(width / 12, height / 5, 80);
              return <g key={module.module_id}>
                <rect x={x} y={y} width={width} height={height} fill="#fff" stroke="#698171" strokeWidth="2" vectorEffect="non-scaling-stroke" />
                <text x={x + width / 2} y={y + height / 2 - fontSize / 2} textAnchor="middle" fontSize={fontSize} fill="#263b2b">
                  R{module.row + 1} · K{module.column + 1}
                </text>
                <text x={x + width / 2} y={y + height / 2 + fontSize} textAnchor="middle" fontSize={fontSize * 0.8} fill="#263b2b">
                  {mm(module.dimensions_um.width_um)} × {mm(module.dimensions_um.height_um)} mm
                </text>
              </g>;
            })}
          </svg>
        </div>
        {result.load_distribution ? <p>
          Last per hel hyllrad: {result.load_distribution.source_shelf_row_payload_n} N i källan,
          {" "}{result.load_distribution.assembled_shelf_row_payload_n} N sammanlagt över modulkolumnerna.
          Totalt över alla hyllplan: {result.load_distribution.planned_total_shelf_payload_n} N.
          {result.grid.rows > 1 ? " Staplingslasten från överliggande moduler ingår inte i hyllkontrollen." : ""}
        </p> : null}
        <div className={styles.tableScroll}><table className={styles.comparisonTable}>
          <caption>Modulmått och kontroller</caption>
          <thead><tr><th scope="col">Modul</th><th scope="col">B × H × D (mm)</th><th scope="col">Vikt (kg)</th>
            <th scope="col">Geometri och bärighet</th><th scope="col">Råformat</th><th scope="col">Arbetsfil</th></tr></thead>
          <tbody>{result.modules.map(module => <tr key={module.module_id}>
            <th scope="row">Rad {module.row + 1}, kolumn {module.column + 1}</th>
            <td>{mm(module.dimensions_um.width_um)} × {mm(module.dimensions_um.height_um)} × {mm(module.dimensions_um.depth_um)}</td>
            <td>{kg(module.weight_g)}</td>
            <td>{module.preview.rules.overall_status === "BLOCK" ? "Kräver åtgärd"
              : module.preview.rules.overall_status === "WARNING" ? "Kräver granskning" : "Beräknade kontroller uppfyllda"}</td>
            <td>{module.preview.manufacturing.geometry_compatible === true ? "Delarna ryms enskilt"
              : module.preview.manufacturing.geometry_compatible === false ? "Format behöver ändras" : "Råformat inte kontrollerat"}</td>
            <td><button type="button" disabled={!canExport}
              aria-label={`Hämta arbetsfil för rad ${module.row + 1}, kolumn ${module.column + 1}`}
              onClick={() => save(module.workspace, `custombuild-${module.module_id}.json`)}>Hämta utkast</button></td>
          </tr>)}</tbody>
        </table></div>
        {result.modules.map(module => {
          const ownRequirements = module.requirements.filter(requirement => !result.requirements.some(global => global.code === requirement.code));
          return ownRequirements.length ? <details key={module.module_id}>
            <summary>Kontroller för rad {module.row + 1}, kolumn {module.column + 1}</summary>
            <ul>{ownRequirements.map((requirement, index) => <li key={`${requirement.code}-${index}`}>
              {requirementText(requirement)}
            </li>)}</ul>
          </details> : null;
        })}
      </> : null}
      {result.requirements.length ? <>
        <h4>Behöver lösas före tillverkning</h4>
        <ul>{result.requirements.map((requirement, index) => <li key={`${requirement.code}-${index}`}>
          {requirement.status === "blocked" ? <strong>Behöver åtgärdas: </strong> : null}{requirementText(requirement)}
        </li>)}</ul>
      </> : null}
      {result.state === "available" ? <>
        <p>Arbetsfilerna är nya modulutkast. Importera varje arbetsfil som ett separat projekt för fortsatt redigering.
          Planen innehåller alla moduler och deras placering. Källprojektet behålls.</p>
        <p>Planen är ett konstruktionsunderlag som behöver granskas före tillverkning.</p>
        <button type="button" disabled={!canExport} onClick={() => save(result, `custombuild-modulplan-${result.plan_hash.slice(0, 12)}.json`)}>
          Hämta hela modulplanen
        </button>
      </> : null}
    </div> : null}
  </section>;
}
