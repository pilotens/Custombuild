import type { components } from "./api-schema";
import { profileSvgOutline } from "./measured-profiles";

export type MeasuredWork = components["schemas"]["MeasuredAssembly"];
export type MeasuredResult = components["schemas"]["MeasuredReport"];
export type MeasuredPiece = components["schemas"]["MeasuredPart"];
export type MeasuredCut = components["schemas"]["MeasuredOperation"];
export const measuredMm = (v: number) =>
  (v / 1000).toLocaleString("sv-SE", { maximumFractionDigits: 3 });
export const measuredArea = (um2: number) =>
  (um2 / 1e6).toLocaleString("sv-SE", { maximumFractionDigits: 6 });
export const faceNames: Record<string, string> = {
  front: "fram",
  back: "bak",
  left: "vänster",
  right: "höger",
  top: "ovan",
  bottom: "under",
};
export const cutNames: Record<string, string> = {
  drill: "Borrning",
  pocket: "Ficka / tapphål",
  slot: "Slits / fönster",
  tenon: "Tapp",
  notch: "Urtag / fals",
};
const provenanceNames = {
  listed: "angiven i bearbetningslistan",
  drawing: "från arbetsritningen",
  transfer_template: "läge härlett från motdelen",
  edited: "manuellt ändrat i detta arbetsprov",
};
const esc = (v: string | number) =>
  String(v).replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&apos;",
      })[c]!,
  );

export function measuredViews(piece: MeasuredPiece) {
  const groups = new Map<
    string,
    { face: string; axes: string; cuts: MeasuredCut[] }
  >();
  for (const op of piece.operations ?? []) {
    const key = op.face + "-" + op.axes;
    if (!groups.has(key))
      groups.set(key, { face: op.face, axes: op.axes, cuts: [] });
    groups.get(key)!.cuts.push(op);
  }
  const n = piece.size_um.indexOf(Math.min(...piece.size_um));
  const primary = [
    ["left", "right", "yz"],
    ["front", "back", "xz"],
    ["top", "bottom", "xy"],
  ][n]!;
  for (const face of primary.slice(0, 2)) {
    if (![...groups.values()].some((g) => g.face === face))
      groups.set(face + "-" + primary[2], {
        face,
        axes: primary[2]!,
        cuts: [],
      });
  }
  for (const [face, axes] of [["left","yz"],["right","yz"],["front","xz"],["back","xz"],["top","xy"],["bottom","xy"]]) {
    if (![...groups.values()].some(g=>g.face===face))
      groups.set(face+"-"+axes,{face:face!,axes:axes!,cuts:[]});
  }
  return [...groups.values()];
}

export function measuredFaceSvg(
  piece: MeasuredPiece,
  view: ReturnType<typeof measuredViews>[number],
) {
  const [u, v] = view.axes.split("").map((a) => "xyz".indexOf(a));
  const w = piece.size_um[u!]! / 1000,
    h = piece.size_um[v!]! / 1000;
  const padding = Math.max(4, Math.max(w, h) * 0.025),
    stroke = Math.max(0.1, Math.min(0.7, Math.max(w, h) / 1000));
  const features = view.cuts
    .map((c) => {
      const x = c.u_um / 1000,
        y = c.v_um / 1000,
        a = c.width_um / 1000,
        b = c.height_um / 1000;
      const attrs = `data-feature-id="${esc(c.id)}" data-shape="${esc(c.shape)}" stroke="${c.transfer ? "#9c6520" : "#134f4d"}"`;
      const label = `<title>${esc(c.id)} · ${esc(cutNames[c.shape]!)} · U ${measuredMm(c.u_um)} / V ${measuredMm(c.v_um)} · ${measuredMm(c.depth_um)} mm</title>`;
      if (c.shape === "drill")
        return `<circle ${attrs} cx="${x}" cy="${y}" r="${a / 2}" fill="none">${label}</circle>`;
      return `<rect ${attrs} x="${x - a / 2}" y="${y - b / 2}" width="${a}" height="${b}" fill="${c.shape === "tenon" ? "#b8ddca" : "#f2e5bc"}">${label}</rect>`;
    })
    .join("");
  const outline = profileSvgOutline(piece, view.axes) ?? `<rect width="${w}" height="${h}" fill="#fffefa" stroke="#473e31"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${-padding} ${-padding} ${w + 2 * padding} ${h + 2 * padding}"><title>${esc(piece.id)} · ${esc(view.face)} · mm</title><g stroke-width="${stroke}">${outline}${features}</g></svg>`;
}

export function measuredHardware(report: MeasuredResult, id: string) {
  const counts = new Map<string, number>();
  for (const joint of report.assembly.joints)
    if (joint.a.part === id || joint.b.part === id) {
      for (const sku of [joint.article, ...joint.fittings])
        if (sku) counts.set(sku, (counts.get(sku) ?? 0) + 1);
    }
  return [...counts].sort(([a], [b]) => a.localeCompare(b));
}

// Only nominally matching contacts in the same coordinate frame can connect parts.
export function measuredGroups(
  report: MeasuredResult,
  frame: string | null,
  remove?: string,
) {
  const graph = new Map<string, Set<string>>();
  for (const part of report.assembly.parts) {
    if (
      part.id !== remove &&
      part.origin_um &&
      (part.frame_id ?? null) === frame
    )
      graph.set(part.id, new Set());
  }
  function connect(a: string, b: string) {
    if (graph.has(a) && graph.has(b)) {
      graph.get(a)!.add(b);
      graph.get(b)!.add(a);
    }
  }
  const matches = new Set(
    report.joints.filter((j) => j.state === "aligned").map((j) => j.id),
  );
  for (const j of report.assembly.joints)
    if (matches.has(j.id)) connect(j.a.part, j.b.part);
  const supported = new Set(
    report.bearings.filter((b) => b.state === "aligned").map((b) => b.id),
  );
  for (const b of report.assembly.bearings)
    if (supported.has(b.id)) connect(b.part, b.support);
  const groups: string[][] = [];
  const visited = new Set<string>();
  for (const id of graph.keys()) {
    if (visited.has(id)) continue;
    const group = [id];
    visited.add(id);
    for (const member of group) {
      for (const neighbour of graph.get(member)!) {
        if (!visited.has(neighbour)) {
          visited.add(neighbour);
          group.push(neighbour);
        }
      }
    }
    groups.push(group);
  }
  return groups.sort((a, b) => b.length - a.length);
}

export function measuredPartSheet(
  report: MeasuredResult,
  piece: MeasuredPiece,
) {
  const rows = (piece.operations ?? [])
    .map(
      (o) =>
        `<tr><td>${esc(o.id)}</td><td>${esc(cutNames[o.shape]!)}</td><td>${esc(faceNames[o.face]!)} / ${esc(o.axes.toUpperCase())}</td><td>${measuredMm(o.u_um)} / ${measuredMm(o.v_um)}</td><td>${measuredMm(o.width_um)} × ${measuredMm(o.height_um)} × ${measuredMm(o.depth_um)}</td><td>${esc(o.article ?? "—")} · ${esc(o.source)} · ${provenanceNames[o.provenance]}${o.transfer ? " · överförs vid provpassning" : ""}</td></tr>`,
    )
    .join("");
  const mates = (report.assembly.joints ?? [])
    .filter((j) => j.a.part === piece.id || j.b.part === piece.id)
    .map((j) => {
      const c = report.joints.find((o) => o.id === j.id)!;
      return `<li>${esc(j.id)} · ${esc(j.a.part)}/${esc(j.a.operation)} ↔ ${esc(j.b.part)}/${esc(j.b.operation)} · ${esc(c.reason)}${c.frame_id ? " · lokalt i " + esc(c.frame_id) : ""}${c.uses_transfer_template ? " · mottagarens läge är härlett från motdelen" : ""} · ${esc([j.article, ...(j.fittings ?? [])].filter(Boolean).join(", ") || "utan separat artikel")}</li>`;
    })
    .join("");
  const bearings = report.assembly.bearings
    .filter((b) => b.part === piece.id || b.support === piece.id)
    .map((b) => {
      const check = report.bearings.find((c) => c.id === b.id)!;
      return `<li>${esc(b.part)} på ${esc(b.support)} · ${esc(check.reason)} · ${(check.contact_area_um2 ?? 0) / 1e6} mm²</li>`;
    })
    .join("");
  const issues = [
    ...(report.issues[piece.id] ?? []),
    ...(report.assembly.open_items ?? []),
    ...report.assembly.changes.map((t) => "Manuell ändring: " + t),
  ];
  const backing = report.assembly.backing?.part_ids.includes(piece.id)
    ? report.back_coverage
        .filter((b) => (b.exposure_on_removal_um2[piece.id] ?? 0) > 0)
        .map(
          (b) =>
            `<li>${esc(b.id)} · ${measuredArea(b.exposure_on_removal_um2[piece.id]!)} mm² öppnas om delen tas ut.</li>`,
        )
        .join("")
    : "";
  const articles = measuredHardware(report, piece.id)
    .map(([sku, n]) => `<li>${n} × ${esc(sku)}</li>`)
    .join("");
  return `<!doctype html><html lang="sv"><meta charset="utf-8"><title>${esc(piece.id)} · delritning</title><style>body{max-width:800px;margin:auto;padding:24px;font:14px/1.5 system-ui;color:#243c33}svg{max-width:100%;height:230px}table{border-collapse:collapse}td,th{padding:5px;border:1px solid #b9c6bc;text-align:left}code{overflow-wrap:anywhere}@media print{.face{break-before:page}tr{break-inside:avoid}}</style><h1>${esc(piece.id)} · ${esc(piece.name)}</h1><p>Granskningsritning · ${esc(report.assembly.title)}<br>Råämne L/B/T ${piece.blank_lbt_um.map(measuredMm).join(" × ")} mm · ${esc(piece.material)}</p><p>${esc(piece.note ?? "")}</p><p>Källa: ${esc(report.assembly.source)}<br>Modell: <code>${esc(report.assembly_sha256)}</code></p><p>Koordinatsystem: ${esc(piece.frame_id ?? "hela monteringen")}.</p><h2>Bearbetningar</h2><table><tr><th>ID</th><th>Form</th><th>Yta / U,V</th><th>Centrum mm</th><th>B × H × djup mm</th><th>Artikel / källa</th></tr>${rows}</table><h2>Förband</h2><ul>${mates || "<li>Inga parade förband angivna.</li>"}</ul><h2>Beslag på registrerade förband</h2><ul>${articles || "<li>Inga artiklar angivna.</li>"}</ul><h2>Upplag</h2><ul>${bearings || "<li>Inga upplag angivna.</li>"}</ul>${backing ? "<h2>Ryggens täckning vid uttag</h2><ul>" + backing + "</ul>" : ""}<h2>Öppna punkter</h2><ul>${issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul><strong>Fysisk bearbetning är spärrad.</strong>${measuredViews(
    piece,
  )
    .map(
      (v) =>
        `<section class="face"><h2>${esc(faceNames[v.face]!)} · U/V ${esc(v.axes.toUpperCase())}</h2>${measuredFaceSvg(piece, v)}<p>Origo vid U-min/V-min. Måttsiffror gäller; slitsar visas med sitt omslutande mått.</p></section>`,
    )
    .join("")}</html>`;
}

// A small, public, fictitious example independent of any customer's drawings.
export function sampleMeasuredWork(): MeasuredWork {
  const op = (
    id: string,
    shape: MeasuredCut["shape"],
    face: MeasuredCut["face"],
    depth_um: number,
  ): MeasuredCut => ({
    id,
    shape,
    face,
    axes: "xz",
    through: false,
    transfer: false,
    provenance: "listed",
    u_um: 15000,
    v_um: 15000,
    width_um: 12000,
    height_um: 14000,
    depth_um,
    source: "Exempelritning",
  });
  return {
    schema_version: "custombuild.measured-assembly.v1",
    title: "Provfog i ett ramhörn",
    source: "Fiktiva millimetermått",
    bearings: [],
    changes: [],
    open_items: ["Provpassning och bärighet återstår."],
    parts: [
      {
        id: "post-1",
        code: "P",
        name: "Stolpe",
        material: "Provvirke",
        note: "Exempel med avsiktligt angivna mått.",
        size_um: [30000, 40000, 80000],
        blank_lbt_um: [80000, 40000, 30000],
        origin_um: [0, 0, 0],
        operations: [op("ficka", "pocket", "back", 16000)],
      },
      {
        id: "rail-1",
        code: "R",
        name: "Tvärstycke",
        material: "Provvirke",
        note: "Exempel med avsiktligt angivna mått.",
        size_um: [30000, 100000, 30000],
        blank_lbt_um: [100000, 30000, 30000],
        origin_um: [0, 25000, 0],
        operations: [op("tapp", "tenon", "front", 15000)],
      },
    ],
    joints: [
      {
        id: "fog-1",
        kind: "blind_tenon",
        fittings: [],
        interface_gap_um: 0,
        head_allowance_um: 0,
        a: { part: "rail-1", operation: "tapp" },
        b: { part: "post-1", operation: "ficka" },
        source: "Exempelritning",
      },
    ],
  };
}
