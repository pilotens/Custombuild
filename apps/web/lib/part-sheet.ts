import type { PartInspection } from "./part-inspection";

function escapeHtml(value: string | number) {
  return String(value).replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char]!,
  );
}
const mm = (um: number) =>
  (um / 1000).toLocaleString("sv-SE", { maximumFractionDigits: 3 });

function table(headings: string[], rows: (string | number)[][]) {
  return `<table><thead><tr>${headings.map((s) => `<th>${escapeHtml(s)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

/** Standalone printable review, generated from the same response as the part inspector. */
export function printablePartSheet(sheet: PartInspection) {
  const p = sheet.part;
  const extent = (s: typeof p.finished_size) =>
    [s.width_um, s.depth_um, s.height_um].map(mm).join(" × ");
  const head = `<p class="label">CUSTOMBUILD · DELGRANSKNING</p><h1>${escapeHtml(p.semantic_key)}</h1>
    <p>Del ${escapeHtml(p.part_id)} · revision ${p.revision}<br>Modell ${escapeHtml(sheet.design_hash)}<br>Delblad ${escapeHtml(sheet.sheet_sha256)}</p>`;
  const metrics = table(
    ["Material", "Fiber", "Färdigmått X/Y/Z (mm)", "Råmått X/Y/Z (mm)"],
    [
      [
        p.material_id + "@" + p.material_version,
        p.grain_direction,
        extent(p.finished_size),
        extent(p.raw_size),
      ],
    ],
  );
  const stock = sheet.stock.length
    ? table(
        [
          "Deklarerat format (mm)",
          "Marginal (mm)",
          "Fiber",
          "Delen ryms",
          "Källa",
        ],
        sheet.stock.map((s) => [
          [s.width_um, s.height_um].map(mm).join(" × "),
          mm(s.margin_um),
          s.grain ?? "okänd",
          s.fits === true ? "Ja" : s.fits === false ? "Nej" : "Ej verifierat",
          s.source,
        ]),
      )
    : "<p>Inget råformat verifierat.</p>";
  const mates = table(
    [
      "Motdel",
      "Förband",
      "Sidor (del → motdel)",
      "Bearbetnings-ID på delen",
      "Bearbetnings-ID på motdelen",
      "Beslag",
    ],
    sheet.mates.map((m) => {
      const own = m.joint.members.find((v) => v.part_id === p.part_id)!;
      const other = m.joint.members.find((v) => v.part_id === m.part_id)!;
      return [
        m.semantic_key + " · " + m.part_id,
        m.joint.joint_type + " · " + m.joint.joint_id,
        own.mating_face + " → " + other.mating_face,
        own.feature_ids.join(", "),
        other.feature_ids.join(", "),
        m.joint.hardware_sku
          ? m.joint.hardware_count + " × " + m.joint.hardware_sku
          : "Ingen angiven artikel",
      ];
    }),
  );
  const features = table(
    [
      "ID",
      "Typ / sida",
      "Lokalt XYZ (mm)",
      "Dimensioner (mm)",
      "Antal / delning",
      "Genomgående",
    ],
    p.features.map((f) => [
      f.feature_id,
      f.kind + " / " + f.face,
      [f.origin.x_um, f.origin.y_um, f.origin.z_um].map(mm).join(" / "),
      Object.entries(f.dimensions)
        .filter(([, v]) => typeof v === "number")
        .map(([k, v]) => k.replace("_um", "") + " " + mm(v!))
        .join(", "),
      (f.pattern_count ?? 1) +
        (f.pitch_um ? " / " + mm(f.pitch_um) + " mm" : ""),
      f.through ? "Ja" : "Nej",
    ]),
  );
  const reference = sheet.reference_objects.length
    ? "<h2>Referensens delar</h2>" +
      table(
        ["Referensobjekt", "Status", "Jämförda modellobjekt", "Observation"],
        sheet.reference_objects.map((o) => [
          o.label,
          {
            represented: "Representerad",
            different: "Avvikelse",
            unresolved: "Ej fastställt",
          }[o.state],
          o.compared_parts.map((id) => sheet.part_names[id] ?? id).join(", "),
          o.observation,
        ]),
      )
    : "";
  const drawings = sheet.drawings
    .map(
      (d) =>
        `<section class="face"><h2>Sida ${escapeHtml(d.view)} · ${escapeHtml(d.physical_face)}</h2><p>U = ${escapeHtml(d.u_axis)}, V = ${escapeHtml(d.v_axis)}. Mått i mm. Origo vid färdigdelens U-min/V-min.</p><img alt="Delritning" src="data:image/svg+xml;charset=utf-8,${escapeHtml(encodeURIComponent(d.svg))}">${
          d.holes.length
            ? table(
                [
                  "Hål-ID",
                  "U (mm)",
                  "V (mm)",
                  "Ø (mm)",
                  "Djup (mm)",
                  "Genomgående",
                ],
                d.holes.map((h) => [
                  h.feature_id,
                  mm(h.u_um),
                  mm(h.v_um),
                  mm(h.diameter_um),
                  mm(h.depth_um),
                  h.through ? "Ja" : "Nej",
                ]),
              )
            : ""
        }</section>`,
    )
    .join("");
  return `<!doctype html><html lang="sv"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Delblad · ${escapeHtml(p.semantic_key)}</title>
    <style>body{font:15px/1.45 system-ui,sans-serif;max-width:1000px;margin:auto;padding:26px;color:#24312b;overflow-wrap:anywhere}h1{margin:.1em 0}h2{font-size:1.1em;margin-top:1.6em}.label{font-weight:bold;letter-spacing:.12em}table{width:100%;border-collapse:collapse}td,th{text-align:left;vertical-align:top;border-bottom:1px solid #c8cec7;padding:6px;font-size:.85em}th{background:#edf1ed}img{width:100%;max-height:75vh}.face{break-before:page}@media print{@page{margin:14mm}body{padding:0}tr{break-inside:avoid}}</style>
    ${head}<strong>För granskning. Fysisk bearbetning är inte frisläppt.</strong>${metrics}
    <h2>Råämne</h2>${stock}<h2>Förband och motdelar</h2>${sheet.mates.length ? mates : "<p>Ingen förbindelse modellerad.</p>"}
    <h2>Beslag knutna till delen</h2>${table(
      ["Artikel", "Antal", "Bearbetnings-ID"],
      sheet.hardware.map((h) => [h.sku, h.quantity, h.feature_ids.join(", ")]),
    )}
    ${sheet.runner ? `<p>Skenprofil ${escapeHtml(sheet.runner.catalog_id)} · slag ${mm(sheet.runner.travel_um)} mm.</p>` : ""}
    <h2>Alla registrerade bearbetningar</h2>${features}${reference}${drawings}</html>`;
}
