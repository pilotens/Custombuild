"use client";

import { useEffect, useState } from "react";
import type { ReferencePost, ResolvedPart } from "@/lib/design-types";
import type { PartInspection, PartView } from "@/lib/part-inspection";
import { exactMillimetreTextToMicrometres } from "@/lib/workshop-production-context";
import styles from "./assembly-part-review.module.css";

const fields = [
  ["width_um", "Bredd X"],
  ["depth_um", "Djup Y"],
  ["height_um", "Längd Z"],
  ["x_um", "Placering X"],
  ["y_um", "Placering Y"],
  ["z_um", "Underkant Z"],
] as const;
const mm = (um: number) =>
  (um / 1000).toLocaleString("sv-SE", { maximumFractionDigits: 3 });

export function referencePostVisual(post: ReferencePost): ResolvedPart {
  return {
    part_id: post.key,
    name: post.name + " · referensdel",
    kind: "divider",
    orientation: "XY",
    width_mm: post.width_um / 1000,
    depth_mm: post.depth_um / 1000,
    thickness_mm: post.height_um / 1000,
    position_mm: {
      x: (post.x_um + post.width_um / 2) / 1000,
      y: (post.y_um + post.depth_um / 2) / 1000,
      z: (post.z_um + post.height_um / 2) / 1000,
    },
    circular_outline: post.shape === "round",
    color: "#408a86",
    material_id: "reference-only",
    weight_kg: 0,
    features: [],
  };
}

export function ReferencePostEditor({
  posts,
  change,
}: {
  posts: ReferencePost[];
  change: (posts: ReferencePost[]) => void;
}) {
  const [editing, setEditing] = useState<ReferencePost | null>();
  const [error, setError] = useState("");
  function commit(form: HTMLFormElement) {
    const values = new FormData(form);
    try {
      const name = String(values.get("name") ?? "").trim();
      if (!name) throw Error("Ange ett namn för stolpen.");
      const shape = values.get("shape") === "round" ? "round" : "rectangular";
      const integers = Object.fromEntries(
        fields.map(([field]) => {
          const position =
            field === "x_um" || field === "y_um" || field === "z_um";
          const raw = String(values.get(field) ?? "").replace(",", ".");
          return [
            field,
            exactMillimetreTextToMicrometres(raw, {
              minimumUm: position ? -10_000_000 : 1,
              maximumUm:
                position || field === "height_um" ? 10_000_000 : 1_000_000,
            }),
          ];
        }),
      ) as Pick<ReferencePost, (typeof fields)[number][0]>;
      if (shape === "round" && integers.width_um !== integers.depth_um)
        throw Error("Ange samma diameter för bredd och djup.");
      const newKey = Array.from(
        { length: 99 },
        (_, i) => "ref-post-" + (i + 1),
      ).find((key) => !posts.some((p) => p.key === key));
      if (!editing && (!newKey || posts.length >= 32))
        throw Error("Högst 32 separata referensstolpar kan granskas.");
      const post: ReferencePost = {
        key: editing?.key ?? newKey!,
        name,
        shape,
        ...integers,
      };
      change(
        editing
          ? posts.map((p) => (p.key === post.key ? post : p))
          : [...posts, post],
      );
      setEditing(undefined);
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Kontrollera måtten.",
      );
    }
  }
  return (
    <details>
      <summary>Separata uppmätta stolpar ({posts.length})</summary>
      <p><a href="/assembly">Öppna en arbetsritning med flera delar och förband</a></p>
      <p>
        Ange delens egna mått. X mäts från stommens vänsterkant, Y från
        framkanten och Z från golvet till delens lägsta hörn. Negativa Y ligger
        framför stommen.
      </p>
      {posts.map((p) => (
        <div key={p.key}>
          <button
            type="button"
            onClick={() => {
              setEditing(p);
              setError("");
            }}
          >
            {p.name} · ändra
          </button>{" "}
          <button
            type="button"
            onClick={() => {
              change(posts.filter((o) => o.key !== p.key));
              setEditing(undefined);
            }}
          >
            Ta bort {p.name}
          </button>
        </div>
      ))}
      {editing === undefined ? (
        <button
          type="button"
          onClick={() => {
            setEditing(null);
            setError("");
          }}
        >
          Lägg till uppmätt stolpe
        </button>
      ) : (
        <form
          key={editing?.key ?? "new"}
          onSubmit={(e) => {
            e.preventDefault();
            commit(e.currentTarget);
          }}
        >
          <label>
            Stolpens namn{" "}
            <input
              name="name"
              maxLength={70}
              required
              defaultValue={editing?.name}
            />
          </label>
          <label>
            Tvärsnitt{" "}
            <select name="shape" defaultValue={editing?.shape ?? "rectangular"}>
              <option value="rectangular">Rektangulärt</option>
              <option value="round">Runt</option>
            </select>
          </label>
          {fields.map(([field, label]) => (
            <label key={field}>
              {label} (mm){" "}
              <input
                name={field}
                required
                inputMode="decimal"
                defaultValue={editing ? editing[field] / 1000 : undefined}
              />
            </label>
          ))}
          <button type="submit">Spara stolpens mått</button>{" "}
          <button type="button" onClick={() => setEditing(undefined)}>
            Avbryt
          </button>
          {error && <p role="alert">{error}</p>}
        </form>
      )}
    </details>
  );
}

function postDrawing(p: ReferencePost) {
  const w = p.width_um / 1000,
    d = p.depth_um / 1000,
    h = p.height_um / 1000;
  const scale = Math.min(260 / Math.max(w, d), 215 / h, 105 / Math.max(d, 1)),
    a = w * scale,
    b = d * scale,
    c = h * scale;
  const section =
    p.shape === "round"
      ? `<ellipse cx="${175 + a / 2}" cy="${57 + b / 2}" rx="${a / 2}" ry="${b / 2}"/>`
      : `<rect x="175" y="57" width="${a}" height="${b}"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 335"><title>Referensstolpe, måttsatt granskningsritning</title>
    <g fill="#d2e4dd" stroke="#295749" stroke-width="1.2"><rect x="25" y="57" width="${a}" height="${c}"/>${section}</g>
    <g font-family="sans-serif" font-size="13" fill="#1e3730"><text x="22" y="24">GRANSKNING · ${p.key} · mm</text>
    <text x="22" y="47">Framifrån · X/Z</text><text x="175" y="47">Tvärsnitt · X/Y</text>
    <text x="22" y="295">X ${mm(p.width_um)} · Y ${mm(p.depth_um)} · Z ${mm(p.height_um)}</text>
    <text x="22" y="318">Placering ${mm(p.x_um)} / ${mm(p.y_um)} / ${mm(p.z_um)} mm · Infästning ej fastställd</text></g></svg>`;
}

export function ReferencePostReview({
  post,
  load,
  mode,
  changeMode,
  select,
}: {
  post: ReferencePost;
  load: (signal: AbortSignal) => Promise<PartInspection>;
  mode: PartView;
  changeMode: (mode: PartView) => void;
  select: (id: string, semantic: string) => void;
}) {
  const [server, setServer] = useState<{
    data?: PartInspection;
    error?: string;
  }>();
  useEffect(() => {
    const abort = new AbortController();
    Promise.resolve()
      .then(() => load(abort.signal))
      .then((data) => {
        if (!abort.signal.aborted) setServer({ data });
      })
      .catch((err: unknown) => {
        if (!abort.signal.aborted)
          setServer({
            error:
              err instanceof Error
                ? err.message
                : "Servern kunde inte granska stolpen.",
          });
      });
    return () => abort.abort();
  }, [load]);
  const result = server?.data?.reference_objects.find(
    (o) => o.key === post.key,
  );
  const svg =
    "data:image/svg+xml;charset=utf-8," + encodeURIComponent(postDrawing(post));
  return (
    <section className={styles.sheet} aria-label="Uppmätt referensstolpe">
      <header>
        <small>SEPARAT REFERENSDEL</small>
        <h3>{post.name}</h3>
        <code>{post.key}</code>
      </header>
      <div className={styles.actions}>
        {(
          [
            ["whole", "Hela möbeln"],
            ["alone", "Isolera delen"],
            ["without", "Visa utan delen"],
          ] as const
        ).map(([v, label]) => (
          <button
            type="button"
            aria-pressed={v === mode}
            key={v}
            onClick={() => changeMode(v)}
          >
            {label}
          </button>
        ))}
      </div>
      <p>
        {mm(post.width_um)} × {mm(post.depth_um)} × {mm(post.height_um)} mm ·{" "}
        {post.shape === "round" ? "runt" : "rektangulärt"} tvärsnitt.
      </p>
      <div className={styles.faces}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={svg} alt="Stolpens framvy och tvärsnitt" />
        <a href={svg} download={post.key + "-referens.svg"}>
          Hämta stolpens måttritning (SVG)
        </a>
      </div>
      <p>
        Stolpen är en egen referensdel. Hål, beslag och infästningar är ej
        fastställda. Den ingår inte i stommens kaplista.
      </p>
      {result && server?.data ? (
        <>
          <strong>{result.observation}</strong>
          {(
            [
              ["Berör", result.touching],
              ["Överlappar", result.overlapping],
            ] as const
          ).map(([title, ids]) => (
            <div key={title}>
              {title}:{" "}
              {ids?.length
                ? ids.map((id) => (
                    <button
                      key={id}
                      type="button"
                      onClick={() =>
                        select(id, server.data!.part_names[id] ?? id)
                      }
                    >
                      {server.data!.part_names[id] ?? id}
                    </button>
                  ))
                : "inga delar"}
            </div>
          ))}
        </>
      ) : (
        <p role={server?.error ? "alert" : "status"}>
          {server?.error ?? "Inväntar serverns jämförelse med monterad stomme."}
        </p>
      )}
    </section>
  );
}
