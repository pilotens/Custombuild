"use client";

import { useMemo, useState } from "react";
import type { PartView } from "@/lib/part-inspection";
import {
  cutNames,
  faceNames,
  measuredFaceSvg,
  measuredArea,
  measuredMm as mm,
  measuredHardware,
  measuredGroups,
  measuredPartSheet,
  measuredViews,
  type MeasuredCut,
  type MeasuredPiece,
  type MeasuredResult,
  type MeasuredWork,
} from "@/lib/measured-assembly";
import css from "./measured-register.module.css";
import { exactMillimetreTextToMicrometres } from "@/lib/workshop-production-context";
import { MeasuredStockEdit } from "./measured-planning";
import { orderSceneFaces, profileFaces } from "@/lib/measured-profiles";

const panelFaces = [
  [0, 1, 3, 2],
  [4, 6, 7, 5],
  [0, 4, 5, 1],
  [2, 3, 7, 6],
  [0, 2, 6, 4],
  [1, 5, 7, 3],
];
const colors = [
  "#aa815d",
  "#c4a17b",
  "#ae8967",
  "#bc9873",
  "#9f795a",
  "#c8a885",
];

// The normal faces the camera when its component along projection depth is negative.
function entryMarks(
  part: MeasuredPiece,
  viewDirection: number[],
  project: (coordinates: number[]) => number[],
) {
  return part.operations.flatMap((op) => {
    if (op.shape !== "drill" && op.shape !== "slot") return [];
    const normals: Record<MeasuredCut["face"], [number, number]> = {
      left: [0, -1],
      right: [0, 1],
      front: [1, -1],
      back: [1, 1],
      bottom: [2, -1],
      top: [2, 1],
    };
    const [axis, sign] = normals[op.face];
    if (viewDirection[axis]! * sign >= -1e-8) return [];
    const [u, v] = op.axes.split("").map((letter) => "xyz".indexOf(letter));
    const outline =
      op.shape === "drill"
        ? Array.from({ length: 32 }, (_, i) => [
            Math.cos((i * Math.PI) / 16),
            Math.sin((i * Math.PI) / 16),
          ])
        : [
            [-1, -1],
            [1, -1],
            [1, 1],
            [-1, 1],
          ];
    const points = outline
      .map(([du, dv]) => {
        const p = [0, 0, 0];
        p[axis] = sign > 0 ? part.size_um[axis]! : 0;
        p[u!] = op.u_um + (du! * op.width_um) / 2;
        p[v!] = op.v_um + (dv! * op.height_um) / 2;
        return project(p.map((n) => n / 1000))
          .slice(0, 2)
          .join(",");
      })
      .join(" ");
    return [{ id: op.id, points, title: cutNames[op.shape] + " · " + op.id }];
  });
}

function MeasuredScene({
  report,
  selected,
  mode,
  choose,
  yaw,
  pitch,
}: {
  report: MeasuredResult;
  selected: string;
  mode: PartView;
  choose: (id: string) => void;
  yaw: number;
  pitch: number;
}) {
  const polys = useMemo(() => {
    const a = (yaw * Math.PI) / 180,
      b = (pitch * Math.PI) / 180;
    const project = ([x, y, z]: number[]) => {
      const depth = -x! * Math.sin(a) + y! * Math.cos(a);
      return [
        x! * Math.cos(a) + y! * Math.sin(a),
        -z! * Math.cos(b) - depth * Math.sin(b),
        depth * Math.cos(b) - z! * Math.sin(b),
      ];
    };
    const picked = report.assembly.parts.find((p) => p.id === selected);
    const frame = picked?.frame_id;
    const marks =
      mode === "alone" && picked
        ? entryMarks(
            picked,
            [
              -Math.sin(a) * Math.cos(b),
              Math.cos(a) * Math.cos(b),
              -Math.sin(b),
            ],
            project,
          )
        : [];
    const faces: {
      points: number[][];
      depth: number;
      id: string;
      shade: number;
      key: string;
    }[] = [];
    for (const part of report.assembly.parts) {
      if (mode !== "alone" && (part.frame_id ?? null) !== (frame ?? null))
        continue;
      if (
        (mode === "alone" && part.id !== selected) ||
        (mode === "without" && part.id === selected) ||
        (!part.origin_um && mode !== "alone")
      )
        continue;
      const origin = mode === "alone" ? [0, 0, 0] : part.origin_um!;
      const profile = profileFaces(part);
      if (profile) {
        profile.forEach((face, n) => {
          const points = face.map(p => project(p.map((v,axis)=>(v+origin[axis]!)/1000)));
          faces.push({points,depth:points.reduce((s,p)=>s+p[2]!,0)/points.length,
            id:part.id,shade:n%6,key:part.id+"-profile-"+n});
        });
        continue;
      }
      for (const [index, box] of (report.cells[part.id] ?? []).entries()) {
        const verts = Array.from({ length: 8 }, (_, i) =>
          project(
            [0, 1, 2].map(
              (axis) =>
                (origin[axis]! + box[axis + ((i >> axis) & 1) * 3]!) / 1000,
            ),
          ),
        );
        for (const [n, indices] of panelFaces.entries()) {
          const points = indices.map((i) => verts[i]!);
          const depth = points.reduce((s, p) => s + p[2]!, 0) / 4;
          faces.push({
            points,
            depth,
            id: part.id,
            shade: n,
            key: part.id + "-" + index + "-" + n,
          });
        }
      }
    }
    if (report.assembly.parts.some(p=>p.profile)) orderSceneFaces(faces, pitch);
    else faces.sort((x,y)=>y.depth-x.depth);
    let minX = Infinity,
      maxX = -Infinity,
      minY = Infinity,
      maxY = -Infinity;
    for (const f of faces)
      for (const p of f.points) {
        minX = Math.min(minX, p[0]!);
        maxX = Math.max(maxX, p[0]!);
        minY = Math.min(minY, p[1]!);
        maxY = Math.max(maxY, p[1]!);
      }
    const pad = Math.max(maxX - minX, maxY - minY) * 0.05 + 1;
    return {
      faces,
      marks,
      viewBox: faces.length
        ? [
            minX - pad,
            minY - pad,
            maxX - minX + 2 * pad,
            maxY - minY + 2 * pad,
          ].join(" ")
        : "0 0 1 1",
    };
  }, [report, selected, mode, yaw, pitch]);
  return (
    <svg
      className={css.scene}
      viewBox={polys.viewBox}
      role="img"
      aria-label="Monterade, klickbara delar i arbetsritningen"
    >
      {polys.faces.map((f) => (
        <polygon
          key={f.key}
          points={f.points.map((p) => p.slice(0, 2).join(",")).join(" ")}
          fill={
            f.id === selected
              ? ["#347864", "#589a83", "#478974"][f.shade % 3]
              : colors[f.shade]
          }
          stroke={f.id === selected ? "#284f42" : "#775d47"}
          strokeWidth=".55"
          vectorEffect="non-scaling-stroke"
          onClick={() => choose(f.id)}
        >
          <title>{f.id}</title>
        </polygon>
      ))}
      {polys.marks.map((mark) => (
        <polygon
          key={mark.id}
          points={mark.points}
          data-entry-id={mark.id}
          fill="#345547"
          stroke="#173b31"
          strokeWidth=".65"
          vectorEffect="non-scaling-stroke"
        >
          <title>{mark.title}</title>
        </polygon>
      ))}
    </svg>
  );
}

function download(text: string, type: string, name: string) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const stateText = {
  aligned: "Nominell passning",
  mismatch: "Avviker",
  unresolved: "Ej fastställt",
};

function PieceDetail({
  piece,
  report,
  choose,
  onChange,
}: {
  piece: MeasuredPiece;
  report: MeasuredResult;
  choose: (p: string) => void;
  onChange?: (input: MeasuredWork, part: string) => void;
}) {
  const views = useMemo(() => measuredViews(piece), [piece]);
  const [viewIx, setViewIx] = useState(0);
  const view = views[viewIx] ?? views[0]!;
  const data = measuredFaceSvg(piece, view);
  const joints = (report.assembly.joints ?? []).filter(
    (j) => j.a.part === piece.id || j.b.part === piece.id,
  );
  const checks = new Map(report.joints.map((j) => [j.id, j]));
  const neighbours = report.overlaps.flatMap((pair) =>
    pair[0] === piece.id ? [pair[1]] : pair[1] === piece.id ? [pair[0]] : [],
  );
  return (
    <article className={css.sheet} aria-label="Måttsatt delblad">
      <header>
        <small>DEL / {piece.code}</small>
        <h2>
          {piece.id} · {piece.name}
        </h2>
      </header>
      <dl>
        <dt>Ämne L × B × T</dt>
        <dd>{piece.blank_lbt_um.map(mm).join(" × ")} mm</dd>
        <dt>Material</dt>
        <dd>{piece.material}</dd>
        <dt>Inköpsformat L × B × T</dt>
        <dd>
          {piece.stock_lbt_um
            ? piece.stock_lbt_um.map(mm).join(" × ") + " mm"
            : "Ej angivet"}
        </dd>
        <dt>Råmått mot inköpsformat</dt>
        <dd>
          {!piece.stock_lbt_um
            ? "Ej fastställt"
            : piece.blank_lbt_um.every((x, i) => x <= piece.stock_lbt_um![i]!)
              ? "Ryms måttmässigt i angivet format"
              : "Råämnet är för stort"}
        </dd>
        <dt>
          {piece.frame_id
            ? "Läge i " + piece.frame_id + " · X/Y/Z"
            : "Placering X/Y/Z"}
        </dt>
        <dd>
          {piece.origin_um
            ? piece.origin_um.map(mm).join(" / ") + " mm"
            : "Ej fastställd"}
        </dd>
      </dl>
      {piece.note && <p>{piece.note}</p>}
      {piece.profile && <p className={report.geometry_method === "exact_cad" ? undefined : css.attention}>
        {piece.profile.kind === "rounded_rectangle"
          ? `Hörnradie ${mm(piece.profile.radius_um)} mm.`
          : `Sned profil i ${piece.profile.axes.toUpperCase()}, ${piece.profile.points_um.length} hörn.`}
        {" "}{report.geometry_method === "exact_cad"
          ? "Profil, bearbetningar och nominell förbandspassning är kontrollerade i CAD."
          : "Konturen visas i ritningen. Förband och bearbetningar mot profilen är ej verifierade."}
      </p>}
      <label>
        Ritningsyta{" "}
        <select value={viewIx} onChange={(e) => setViewIx(+e.target.value)}>
          {views.map((v, i) => (
            <option key={v.face + v.axes} value={i}>
              {faceNames[v.face]} · {v.axes.toUpperCase()}
            </option>
          ))}
        </select>
      </label>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        className={css.drawing}
        src={"data:image/svg+xml;charset=utf-8," + encodeURIComponent(data)}
        alt={"Ritning för " + piece.id + " på ytan " + faceNames[view.face]}
      />
      <small>
        U och V räknas från delens minsta koordinater i angivna axlar. Slitsar
        visas med sitt omslutande mått.
      </small>
      <button
        onClick={() =>
          download(data, "image/svg+xml", piece.id + "-" + view.face + ".svg")
        }
      >
        Hämta denna yta som SVG
      </button>
      <details open>
        <summary>Bearbetningar på ytan ({view.cuts.length})</summary>
        {view.cuts.map((o) => (
          <div className={css.row} key={o.id}>
            <b>
              {cutNames[o.shape]} · {o.id}
            </b>
            <span>
              U {mm(o.u_um)} / V {mm(o.v_um)} mm ·{" "}
              {o.shape === "drill"
                ? "Ø " + mm(o.width_um)
                : mm(o.width_um) + " × " + mm(o.height_um)}{" "}
              · djup {mm(o.depth_um)} mm{o.through ? " · genomgående" : ""}
            </span>
            <small>
              {o.source}
              {o.article ? " · " + o.article : ""}
              {o.provenance === "transfer_template"
                ? " · härledd från motdelen"
                : ""}
              {o.transfer ? " · överförs efter provpassning" : ""}
            </small>
          </div>
        ))}
      </details>
      <details open>
        <summary>Förband och motdelar ({joints.length})</summary>
        {!joints.length && <p>Ingen parad infästning i arbetsfilen.</p>}
        {joints.map((j) => {
          const c = checks.get(j.id)!;
          const other = j.a.part === piece.id ? j.b : j.a;
          return (
            <div key={j.id} className={css.row}>
              <button onClick={() => choose(other.part)}>{other.part}</button>
              <b>
                {stateText[c.state]} · {j.id}
              </b>
              {c.frame_id && (
                <small>
                  Lokal kontroll inom {c.frame_id}. Delmontagets läge i möbeln
                  är öppet.
                </small>
              )}
              <span>{c.reason}</span>
              {c.uses_transfer_template && (
                <small>
                  En pilot är härledd från motdelen och behöver överföras efter
                  provpassning.
                </small>
              )}
              {c.offset_um && (
                <small>
                  Centrumavvikelse X/Y/Z {c.offset_um.map(mm).join(" / ")} mm.
                </small>
              )}
              {c.depth_reserve_um != null && (
                <small>Djupreserv {mm(c.depth_reserve_um)} mm.</small>
              )}
              <small>
                {j.a.part}/{j.a.operation} ↔ {j.b.part}/{j.b.operation} ·{" "}
                {j.source}
                {[j.article, ...(j.fittings ?? [])].filter(Boolean).length
                  ? " · " +
                    [j.article, ...(j.fittings ?? [])]
                      .filter(Boolean)
                      .join(", ")
                  : ""}
              </small>
            </div>
          );
        })}
      </details>
      <details open>
        <summary>Beslag i delens förband</summary>
        {measuredHardware(report, piece.id).length ? (
          <ul>
            {measuredHardware(report, piece.id).map(([sku, n]) => (
              <li key={sku}>
                {n} × {sku}
              </li>
            ))}
          </ul>
        ) : (
          <p>Inga separata artiklar angivna på de registrerade förbanden.</p>
        )}
      </details>
      <details>
        <summary>Upplag och bärkontakt</summary>
        {report.assembly.bearings
          .filter((b) => b.part === piece.id || b.support === piece.id)
          .map((b) => {
            const check = report.bearings.find((c) => c.id === b.id)!;
            const other = b.part === piece.id ? b.support : b.part;
            return (
              <div key={b.id} className={css.row}>
                <button onClick={() => choose(other)}>{other}</button>
                <strong>{check.reason}</strong>
                <span>
                  {((check.contact_area_um2 ?? 0) / 1e6).toLocaleString(
                    "sv-SE",
                  )}{" "}
                  mm² gemensam nominell anliggningsyta · {b.source}
                </span>
              </div>
            );
          })}
      </details>
      {onChange && (
        <>
          <MeasuredStockEdit report={report} piece={piece} change={onChange} />
          <OperationEdit report={report} piece={piece} change={onChange} />
        </>
      )}
      {(neighbours.length > 0 || report.issues[piece.id]?.length) && (
        <div className={css.attention}>
          <b>Att utreda på delen</b>
          {(report.issues[piece.id] ?? []).map((t) => (
            <p key={t}>{t}</p>
          ))}
          {neighbours.map((id) => (
            <button key={id} onClick={() => choose(id)}>
              Volymöverlapp med {id}
            </button>
          ))}
        </div>
      )}
      <button
        onClick={() =>
          download(
            measuredPartSheet(report, piece),
            "text/html;charset=utf-8",
            piece.id + "-delblad.html",
          )
        }
      >
        Hämta utskrivbart delblad med alla ytor
      </button>
    </article>
  );
}

function OperationEdit({
  report,
  piece,
  change,
}: {
  report: MeasuredResult;
  piece: MeasuredPiece;
  change: (input: MeasuredWork, part: string) => void;
}) {
  const [opIx, setOpIx] = useState(0),
    [error, setError] = useState("");
  const op = piece.operations[opIx];
  if (!op) return null;
  return (
    <details>
      <summary>Pröva en ändring av en bearbetning</summary>
      <label>
        Bearbetning{" "}
        <select value={opIx} onChange={(e) => setOpIx(+e.target.value)}>
          {piece.operations.map((o, i) => (
            <option key={o.id} value={i}>
              {o.id} · {cutNames[o.shape]}
            </option>
          ))}
        </select>
      </label>
      <form
        key={op.id}
        onSubmit={(e) => {
          e.preventDefault();
          const data = new FormData(e.currentTarget);
          try {
            const numbers = ["u_um", "v_um", "depth_um"].map((k) =>
              exactMillimetreTextToMicrometres(
                String(data.get(k) ?? "").replace(",", "."),
                {
                  minimumUm: k === "depth_um" ? 1 : -20_000_000,
                  maximumUm: 20_000_000,
                },
              ),
            );
            const edited = {
              ...op,
              provenance:
                op.provenance === "transfer_template"
                  ? op.provenance
                  : ("edited" as const),
              u_um: numbers[0]!,
              v_um: numbers[1]!,
              depth_um: numbers[2]!,
            };
            if (
              edited.u_um === op.u_um &&
              edited.v_um === op.v_um &&
              edited.depth_um === op.depth_um
            )
              return;
            const note = `${piece.id}/${op.id}: U/V/djup ${[op.u_um, op.v_um, op.depth_um].map(mm).join("/")} → ${numbers.map(mm).join("/")} mm.`;
            change(
              {
                ...report.assembly,
                changes: [...report.assembly.changes, note],
                parts: report.assembly.parts.map((p) =>
                  p.id === piece.id
                    ? {
                        ...p,
                        operations: p.operations.map((o) =>
                          o.id === op.id ? edited : o,
                        ),
                      }
                    : p,
                ),
              },
              piece.id,
            );
          } catch (err) {
            setError(
              err instanceof Error ? err.message : "Kontrollera måtten.",
            );
          }
        }}
      >
        {(["u_um", "v_um", "depth_um"] as const).map((k, i) => (
          <label key={k}>
            {["U (mm)", "V (mm)", "Djup (mm)"][i]}
            <input
              required
              name={k}
              inputMode="decimal"
              defaultValue={op[k] / 1000}
            />
          </label>
        ))}
        <button type="submit">Kontrollera ändringen i monteringen</button>
        {error && <p role="alert">{error}</p>}
      </form>
    </details>
  );
}

function RearCoverage({
  report,
  removed,
}: {
  report: MeasuredResult;
  removed?: string;
}) {
  const backing = report.assembly.backing;
  if (!backing) return null;
  return (
    <details open>
      <summary>Ryggens täckning ({backing.openings.length} ytor)</summary>
      <p>
        De registrerade ryggdelarnas material projiceras mot varje angivet fack
        inom det avsedda djupet. Procentsiffran avser skivornas täckning.
        Borrhål och slitsar räknas inte som öppna fack.
      </p>
      {report.unresolved_back_parts.length > 0 && (
        <p className={css.attention}>
          Okänt monteringsläge eller saknad ryggdel:{" "}
          {report.unresolved_back_parts.join(", ")}
        </p>
      )}
      {backing.openings.map((opening, i) => {
        const checked = report.back_coverage[i]!;
        const loss = removed
          ? (checked.exposure_on_removal_um2[removed] ?? 0)
          : 0;
        const covered = checked.covered_area_um2 - loss;
        const pct = (
          (100 * covered) /
          checked.required_area_um2
        ).toLocaleString("sv-SE", { maximumFractionDigits: 3 });
        const matches =
          opening.target === "open"
            ? covered === 0
            : covered === checked.required_area_um2;
        const status =
          checked.state === "unresolved"
            ? "Ej fastställt"
            : matches
              ? "Önskad täckning"
              : "Avviker";
        return (
          <div className={css.row} key={opening.id}>
            <strong>
              {opening.label} · {status}
            </strong>
            <span>
              {pct} % täckt · {measuredArea(covered)} av{" "}
              {measuredArea(checked.required_area_um2)} mm². Mål:{" "}
              {opening.target === "open" ? "öppet" : "täckt"}.
            </span>
            {loss > 0 && (
              <small>
                {removed} uttagen: {measuredArea(loss)} mm² blir öppet.
              </small>
            )}
          </div>
        );
      })}
    </details>
  );
}

export function MeasuredRegister({
  report,
  initialPart,
  onChange,
}: {
  report: MeasuredResult;
  initialPart?: string;
  onChange?: (input: MeasuredWork, part: string) => void;
}) {
  const [selected, setSelected] = useState(
    initialPart ?? report.assembly.parts[0]!.id,
  );
  const [mode, setMode] = useState<PartView>("whole"),
    [yaw, setYaw] = useState(18),
    [pitch, setPitch] = useState(13);
  const part =
    report.assembly.parts.find((p) => p.id === selected) ??
    report.assembly.parts[0]!;
  const [filter, setFilter] = useState("");
  const select = (id: string) => {
    setSelected(id);
    if (!report.assembly.parts.find((p) => p.id === id)?.origin_um)
      setMode("alone");
    else if (mode === "without") setMode("whole");
  };
  const attached = (report.assembly.joints ?? []).filter(
    (j) => j.a.part === part.id || j.b.part === part.id,
  );
  const groups = useMemo(
    () => measuredGroups(report, part.frame_id ?? null),
    [report, part.frame_id],
  );
  const remnants = useMemo(() => {
    if (mode !== "without") return [];
    const group = new Set(groups.find((g) => g.includes(part.id)) ?? []);
    return measuredGroups(report, part.frame_id ?? null, part.id).filter((g) =>
      group.has(g[0]!),
    );
  }, [mode, groups, part.id, part.frame_id, report]);
  const faults = report.joints.filter((j) => j.state !== "aligned");
  const matched = report.joints.length - faults.length;
  const supports = report.bearings.filter((b) => b.state === "aligned").length;
  return (
    <div className={css.register}>
      <p className={css.source}>{report.assembly.source}</p>
      <div className={css.stats}>
        <span>
          <b>{report.assembly.parts.length}</b> delar
        </span>
        <span>
          <b>
            {report.assembly.parts.reduce(
              (n, p) => n + (p.operations?.length ?? 0),
              0,
            )}
          </b>{" "}
          bearbetningar
        </span>
        <span>
          <b>
            {matched} / {report.joints.length}
          </b>{" "}
          nominellt passande förband
        </span>
        <span>
          <b>
            {supports} / {report.bearings.length}
          </b>{" "}
          nominella upplag
        </span>
      </div>
      <p>
        {
          report.joints.filter((j) => j.state === "aligned" && !j.frame_id)
            .length
        }{" "}
        förband har kontrollerats i hela möbeln och{" "}
        {
          report.joints.filter((j) => j.state === "aligned" && j.frame_id)
            .length
        }{" "}
        i lokala delmontage.{" "}
        {report.joints.filter((j) => j.uses_transfer_template).length} förband
        använder piloter härledda från motdelen.
      </p>
      <div className={css.columns}>
        <section>
          <MeasuredScene
            report={report}
            selected={part.id}
            mode={mode}
            choose={select}
            yaw={yaw}
            pitch={pitch}
          />
          <div className={css.controls}>
            <label>
              Vrid{" "}
              <input
                type="range"
                min="-180"
                max="180"
                value={yaw}
                onChange={(e) => setYaw(+e.target.value)}
              />
            </label>
            <label>
              Höjdvinkel{" "}
              <input
                type="range"
                min="-80"
                max="80"
                value={pitch}
                onChange={(e) => setPitch(+e.target.value)}
              />
            </label>
            <button
              onClick={() => {
                setYaw(0);
                setPitch(0);
              }}
            >
              Framvy
            </button>
            <button
              onClick={() => {
                setYaw(0);
                setPitch(-75);
              }}
            >
              Se undersidan
            </button>
          </div>
          <div className={css.controls}>
            {part.frame_id && (
              <button
                onClick={() => {
                  setSelected(
                    report.assembly.parts.find(
                      (p) => p.origin_um && !p.frame_id,
                    )?.id ?? part.id,
                  );
                  setMode("whole");
                }}
              >
                Till hela möbeln
              </button>
            )}
            {(
              [
                [
                  "whole",
                  part.frame_id ? "Visa delmontaget" : "Hela monteringen",
                ],
                ["alone", "Isolera delen"],
                ["without", "Visa utan delen"],
              ] as const
            ).map(([v, label]) => (
              <button
                key={v}
                aria-pressed={mode === v}
                onClick={() => setMode(v)}
              >
                {label}
              </button>
            ))}
          </div>
          <label>
            Sök i delregistret{" "}
            <input
              type="search"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Kod, delnamn eller exemplar"
            />
          </label>
          <label>
            Välj del i ritningen{" "}
            <select value={part.id} onChange={(e) => select(e.target.value)}>
              {report.assembly.parts
                .filter(
                  (p) =>
                    p.id === part.id ||
                    (p.id + " " + p.name)
                      .toLocaleLowerCase()
                      .includes(filter.toLocaleLowerCase()),
                )
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.id} · {p.name}
                  </option>
                ))}
            </select>
          </label>
          {mode === "without" && (
            <p className={css.attention} role="status">
              {attached.length} registrerade förband till {part.id} öppnas.{" "}
              {
                report.assembly.bearings.filter(
                  (b) => b.part === part.id || b.support === part.id,
                ).length
              }{" "}
              upplag berörs.{" "}
              {remnants.length === 0
                ? "Inga andra delar fanns i den valda delens sammanhängande grupp."
                : remnants.length === 1
                  ? "De återstående delarna i gruppen har fortsatt kontakt."
                  : "Den tidigare sammanhängande gruppen delas upp i " +
                    remnants.length +
                    " grupper (" +
                    remnants.map((g) => g.length).join(", ") +
                    " delar)."}{" "}
              Geometrisk simulering. Hållfastheten efter uttag är inte beräknad.
            </p>
          )}
          {report.assembly.changes.length > 0 && (
            <div className={css.attention}>
              <b>Ändrat arbetsprov</b>
              {report.assembly.changes.map((t, i) => (
                <p key={i}>{t}</p>
              ))}
            </div>
          )}
          <button
            onClick={() =>
              download(
                JSON.stringify(report.assembly, null, 2),
                "application/json",
                "Custombuild-mattsatt-arbetsritning.json",
              )
            }
          >
            Hämta arbetsfilen för appen
          </button>
          <RearCoverage
            report={report}
            removed={mode === "without" ? part.id : undefined}
          />
          {groups.length > 1 && (
            <details open className={css.attention}>
              <summary>{groups.length} separata delgrupper i denna vy</summary>
              <p>
                Delar i olika grupper saknar en kedja av passande förband eller
                verifierade upplag mellan sig.
              </p>
              <ul>
                {groups.map((g) => (
                  <li key={g[0]}>
                    <button onClick={() => select(g[0]!)}>{g[0]}</button> ·{" "}
                    {g.length} {g.length === 1 ? "del" : "delar"}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {(report.fastener_interferences ?? []).length > 0 && (
            <details open className={css.attention}>
              <summary>
                Beslag med volymkonflikt ({report.fastener_interferences.length}{" "}
                par)
              </summary>
              <p>
                Kontrollen jämför nominella cylindrar för måttsatta skruvar och
                styrtappar i samma montering.
              </p>
              <ul>
                {report.fastener_interferences.map((c) => (
                  <li key={c.joint_a + ":" + c.joint_b}>
                    {c.joint_a} ↔ {c.joint_b} ·{" "}
                    {c.state === "mismatch"
                      ? "överlappande nominell volym"
                      : "beror på styrtappens sättdjup"}
                  </li>
                ))}
              </ul>
            </details>
          )}
          <details open>
            <summary>Öppna konstruktionsfrågor</summary>
            <p>
              {report.assembly.parts.filter((p) => !p.stock_lbt_um).length} av{" "}
              {report.assembly.parts.length} delar saknar angivet inköpsformat.
              Deklarerade format behöver bekräftas av leverantören.
            </p>
            <ul>
              {(report.assembly.open_items ?? []).map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
            {faults.map((f) => (
              <p key={f.id}>
                <b>{f.id}</b> · {f.reason}
              </p>
            ))}
            <p>
              {report.assembly.parts.filter((p) => !p.origin_um).length} delar
              saknar monteringsläge.{" "}
              {report.assembly.parts.filter((p) => p.frame_id).length} delar har
              lokala mått i ett delmontage med öppet läge i möbeln.{" "}
              {report.overlaps.length}
              {report.overlaps_truncated ? " eller fler" : ""} delpar har
              överlappande material i kontrollen.
            </p>
            {report.bearings
              .filter((b) => b.state !== "aligned")
              .map((b) => (
                <p key={b.id}>
                  <b>{b.id}</b> · {b.reason}
                </p>
              ))}
            <small>
              {report.geometry_method === "exact_cad"
                ? "CAD-kontrollen använder bearbetade solider med hål, slitsar och urtag. 3D-vyn är en förenklad översikt; STEP innehåller den färdiga geometrin."
                : "Rektangulära tappar, fickor, urtag och falsar är subtraherade. Runda hål och slitsar redovisas i delritningarna och är inte subtraherade vid volymkontrollen."}
            </small>
          </details>
          <small>
            Fysisk tillverkning spärrad · <code>{report.assembly_sha256}</code>
          </small>
        </section>
        <PieceDetail
          key={part.id}
          piece={part}
          report={report}
          choose={select}
          onChange={onChange}
        />
      </div>
    </div>
  );
}
