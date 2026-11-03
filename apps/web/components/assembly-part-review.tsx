"use client";

import { useEffect, useState } from "react";
import type { PartInspection, PartView } from "@/lib/part-inspection";
import { printablePartSheet } from "@/lib/part-sheet";
import styles from "./assembly-part-review.module.css";

const mm = (um: number) =>
  (um / 1000).toLocaleString("sv-SE", { maximumFractionDigits: 3 });
const size = (p: PartInspection["part"]["finished_size"]) =>
  [p.width_um, p.depth_um, p.height_um].map(mm).join(" × ");
const faces: Record<string, string> = {
  left: "vänster",
  right: "höger",
  front: "fram",
  back: "bak",
  top: "ovan",
  bottom: "under",
  a: "A",
  b: "B",
};
const kind: Record<string, string> = {
  drill: "Borrning",
  drill_pattern: "Borrserie",
  countersink: "Försänkning",
  pocket: "Ficka",
  groove: "Spår",
  rabbet: "Fals",
  tenon: "Tapp",
  edge_relief: "Kantavlastning",
  outer_contour: "Ytterkontur",
  mark: "Märkning",
};

export function AssemblyPartReview({
  load,
  mode,
  changeMode,
  selectMate,
  concept = false,
  onInspection,
}: {
  load: (signal: AbortSignal) => Promise<PartInspection>;
  mode: PartView;
  changeMode: (next: PartView) => void;
  selectMate: (id: string, key: string) => void;
  concept?: boolean;
  onInspection?: (sheet: PartInspection) => void;
}) {
  const [fetched, setFetched] = useState<{
    loader: typeof load;
    result?: PartInspection;
    error?: string;
  }>();
  const inspection = fetched?.loader === load ? fetched.result : undefined;
  const error = fetched?.loader === load ? fetched.error : undefined;
  const [faceIndex, setFaceIndex] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.resolve()
      .then(() => load(controller.signal))
      .then((result) => {
        if (!controller.signal.aborted) {
          setFetched({ loader: load, result });
          onInspection?.(result);
        }
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setFetched({
            loader: load,
            error:
              reason instanceof Error
                ? reason.message
                : "Delbladet kunde inte läsas.",
          });
      });
    return () => controller.abort();
  }, [load, onInspection]);
  if (!inspection)
    return (
      <p role={error ? "alert" : "status"}>
        {error || "Hämtar delens ritningar och förband…"}
      </p>
    );
  const { part, drawings, mates, hardware, removal } = inspection;
  const drawing = drawings[faceIndex] ?? drawings[0];
  if (!drawing) return <p role="alert">Delritningen saknas.</p>;
  const source =
    "data:image/svg+xml;charset=utf-8," + encodeURIComponent(drawing.svg);
  const filePrefix = `${part.semantic_key}-${part.part_id}-${inspection.sheet_sha256.slice(0, 12)}-granskning`;
  return (
    <section className={styles.sheet} aria-label="Delritning och förband">
      <header>
        <small>
          DELGRANSKNING · {concept ? "REFERENSKONCEPT" : "SERVERMODELL"}
        </small>
        <h3>{part.semantic_key}</h3>
        <code>{part.part_id}</code>
      </header>
      {onInspection && drawings.some((view) => view.holes.length > 0) && (
        <small>
          3D-vyn markerar borrinträden på den valda delen. Diametrar och
          borrdjup finns i delritningarna.
        </small>
      )}
      <div className={styles.actions} aria-label="Visning av monteringen">
        {(
          [
            ["whole", "Hela möbeln"],
            ["alone", "Isolera delen"],
            ["without", "Visa utan delen"],
          ] as const
        ).map(([value, label]) => (
          <button
            type="button"
            key={value}
            aria-pressed={mode === value}
            onClick={() => changeMode(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {mode === "without" && (
        <div className={styles.impact} role="status">
          <strong>Simulering · delen uttagen</strong>
          <p>
            {mates.length} modellerade förband bryts.{" "}
            {removal.remaining_frame_feet != null
              ? `${removal.remaining_frame_feet.length} ramben har kvar sin kontakt med golvet. `
              : ""}
            {removal.separated_groups.length > 1 &&
              `De tidigare sammanhängande delarna delas i ${removal.separated_groups.length} grupper. `}
            {removal.affected_moving_groups.length > 0 &&
              `Berörda lådor: ${removal.affected_moving_groups.join(", ")}. `}
            Hållfasthet och lastförmåga för varianten är inte beräknade.
          </p>
          {removal.remaining_frame_feet != null && (
            <small>
              Återstående golvstöd:{" "}
              {removal.remaining_frame_feet
                .map((id) => inspection.part_names[id])
                .join(", ") || "inga"}
              .
            </small>
          )}
          {removal.separated_groups.length > 1 && (
            <ul>
              {removal.separated_groups.map((g) => (
                <li key={g[0]}>
                  {g.map((id) => inspection.part_names[id]).join(", ")}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <dl className={styles.metrics}>
        <dt>Färdigmått X/Y/Z</dt>
        <dd>{size(part.finished_size)} mm</dd>
        <dt>Råmått X/Y/Z</dt>
        <dd>{size(part.raw_size)} mm</dd>
        <dt>Material / fiber</dt>
        <dd>
          {part.material_id}@{part.material_version} · {part.grain_direction}
        </dd>
        <dt>Råformat och marginal</dt>
        <dd>
          {inspection.stock.length
            ? inspection.stock.map((s, i) => (
                <div key={i}>
                  {mm(s.width_um)} × {mm(s.height_um)} mm · kant{" "}
                  {mm(s.margin_um)} mm · fiber {s.grain ?? "okänd"}·{" "}
                  {s.fits === true
                    ? "delen ryms"
                    : s.fits === false
                      ? "delen ryms inte"
                      : "ej verifierat"}{" "}
                  · {s.source}
                </div>
              ))
            : "Inget råformat verifierat i delbladet"}
        </dd>
      </dl>
      <div className={styles.faces}>
        <label>
          Ritningssida{" "}
          <select
            value={drawing.view}
            onChange={(e) =>
              setFaceIndex(drawings.findIndex((v) => v.view === e.target.value))
            }
          >
            {drawings.map((f) => (
              <option key={f.view} value={f.view}>
                {f.view} · {faces[f.physical_face]}
              </option>
            ))}
          </select>
        </label>
        {/* The SVG is generated from this part's exact manufacturing features. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={source}
          alt={`Delritning av ${part.semantic_key}, sida ${drawing.view}`}
        />
        <small>
          U = {drawing.u_axis.toUpperCase()}, V = {drawing.v_axis.toUpperCase()}
          . Origo vid färdigdelens U-min/V-min. Vyn är inte speglad.
        </small>
        {drawing.holes.length > 0 && (
          <details>
            <summary>Borrpositioner på sidan ({drawing.holes.length})</summary>
            <ul>
              {drawing.holes.map((h, i) => (
                <li key={i}>
                  U {mm(h.u_um)} / V {mm(h.v_um)} mm · Ø {mm(h.diameter_um)} mm
                  · djup {mm(h.depth_um)} mm {h.through && " · genomgående"}{" "}
                  <code>{h.feature_id}</code>
                </li>
              ))}
            </ul>
          </details>
        )}
        <a download={`${filePrefix}-${drawing.view}.svg`} href={source}>
          Hämta visad delritning (SVG)
        </a>
      </div>
      <details>
        <summary>Bearbetningar på delen ({part.features.length})</summary>
        {part.features.length === 0 ? (
          <p>Ingen bearbetning registrerad i modellen.</p>
        ) : (
          <ul>
            {part.features.map((f) => (
              <li key={f.feature_id}>
                <strong>
                  {kind[f.kind]} · {faces[f.face]}
                </strong>
                <div>
                  Lokalt XYZ {mm(f.origin.x_um)} / {mm(f.origin.y_um)} /{" "}
                  {mm(f.origin.z_um)} mm
                  {f.dimensions.diameter_um != null &&
                    ` · Ø ${mm(f.dimensions.diameter_um)} mm`}
                  {f.dimensions.depth_um != null &&
                    ` · djup ${mm(f.dimensions.depth_um)} mm`}
                  {f.dimensions.width_um != null &&
                    ` · bredd ${mm(f.dimensions.width_um)} mm`}
                  {f.dimensions.length_um != null &&
                    ` · längd ${mm(f.dimensions.length_um)} mm`}
                  {f.pattern_count > 1 &&
                    ` · ${f.pattern_count} lägen med delning ${mm(f.pitch_um!)} mm`}
                  {f.through && " · genomgående"}
                </div>
                <code>{f.feature_id}</code>
              </li>
            ))}
          </ul>
        )}
      </details>
      <details open>
        <summary>Förband och motdelar ({mates.length})</summary>
        {mates.length === 0 ? (
          <p>Ingen förbindelse modellerad. Infästningen behöver utredas.</p>
        ) : (
          <ul>
            {mates.map((m) => {
              const own = m.joint.members.find(
                (a) => a.part_id === part.part_id,
              )!;
              const other = m.joint.members.find(
                (a) => a.part_id === m.part_id,
              )!;
              return (
                <li key={m.joint.joint_id}>
                  <button
                    type="button"
                    onClick={() => selectMate(m.part_id, m.semantic_key)}
                  >
                    {m.semantic_key}
                  </button>{" "}
                  · {m.joint.joint_type}
                  <div>
                    {faces[own.mating_face]} ↔ {faces[other.mating_face]} ·{" "}
                    {m.joint.hardware_sku
                      ? `${m.joint.hardware_count} × ${m.joint.hardware_sku}`
                      : "ingen separat beslagspost"}
                  </div>
                  {m.joint.retention_application_class ===
                    "load_bearing_carcass_dado" &&
                    !m.joint.retention && (
                      <p>
                        Spåret är modellerat. Låsningen saknar verifierat
                        beslag.
                      </p>
                    )}
                  <small>
                    Denna del:{" "}
                    {own.feature_ids.join(", ") || "ingen bearbetningspost"}
                    <br />
                    Motdel:{" "}
                    {other.feature_ids.join(", ") || "ingen bearbetningspost"}
                  </small>
                </li>
              );
            })}
          </ul>
        )}
      </details>
      <details>
        <summary>
          Beslag som hör till delen (
          {hardware.reduce((n, h) => n + h.quantity, 0)})
        </summary>
        {hardware.length ? (
          <ul>
            {hardware.map((h) => (
              <li key={h.sku}>
                {h.quantity} × {h.sku}
              </li>
            ))}
          </ul>
        ) : (
          <p>Inga artiklar är angivna för den här delen.</p>
        )}
        <small>
          Ett gemensamt förbandsbeslag visas på båda motdelarna och räknas en
          gång i möbelns stycklista.
        </small>
      </details>
      {inspection.runner && (
        <p>
          Berörd skena: {inspection.runner.catalog_id} · slag{" "}
          {mm(inspection.runner.travel_um)} mm.
        </p>
      )}
      {inspection.reference_objects.length > 0 && (
        <details open>
          <summary>Referens jämförd med modell</summary>
          <ul>
            {inspection.reference_objects.map((o) => (
              <li key={o.key}>
                <strong>
                  {o.label} ·{" "}
                  {
                    {
                      different: "avviker",
                      unresolved: "ej fastställt",
                      represented: "representerad i modellen",
                    }[o.state]
                  }
                </strong>
                <div>{o.observation}</div>
                {o.compared_parts.map((id) => (
                  <button
                    type="button"
                    key={id}
                    onClick={() => selectMate(id, inspection.part_names[id]!)}
                  >
                    {inspection.part_names[id]}
                  </button>
                ))}
              </li>
            ))}
          </ul>
        </details>
      )}
      {inspection.rear_openings.length > 0 && (
        <details>
          <summary>Rygg bakom möbelns fack</summary>
          <ul>
            {inspection.rear_openings.map((o) => (
              <li key={o.zone + o.bay}>
                {o.zone === "base" ? "Underskåp" : "Överdel"} {o.bay} ·{" "}
                {mm(o.bottom_um)}–{mm(o.top_um)} mm över golv ·{" "}
                <strong>
                  {
                    {
                      open: "öppet",
                      partial: "delvis täckt",
                      covered: "täckt",
                    }[o.state]
                  }
                </strong>
              </li>
            ))}
          </ul>
        </details>
      )}
      <footer>
        <button
          type="button"
          onClick={() => {
            const blob = new Blob([printablePartSheet(inspection)], {
              type: "text/html;charset=utf-8",
            });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = filePrefix + ".html";
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          }}
        >
          Hämta utskrivbart delblad med alla sidor
        </button>
        Granskningsunderlag · modell{" "}
        <code>{inspection.design_hash.slice(0, 12)}</code>
        <a
          download={`${filePrefix}.json`}
          href={
            "data:application/json;charset=utf-8," +
            encodeURIComponent(JSON.stringify(inspection))
          }
        >
          Hämta delblad med alla sidor och förband (JSON)
        </a>
      </footer>
    </section>
  );
}
