"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { CustombuildApiClient, type CurrentPrincipal, type ProjectRead } from "@/lib/api-client";
import {
  FURNITURE_FAMILY_LABELS, furnitureViewerParts, newFurnitureWorkspace, verifiedFurnitureDownload,
  type FurnitureCatalog, type FurnitureFamily, type FurnitureHistory, type FurniturePreview,
  type FurnitureProfileComparison, type FurnitureWorkspace,
} from "@/lib/furniture-workspace";
import type { PublicRuntimeConfig } from "@/lib/runtime-config";
import { exactMillimetreTextToMicrometres } from "@/lib/workshop-production-context";
import styles from "./furniture-workspace.module.css";
import { FurnitureProduction } from "./furniture-production";
import { FurnitureStockRequirements, InstallationEditor, ShelvingLayoutEditor } from "./furniture-dimensions";

const Viewer = dynamic(() => import("./furniture-viewer"), {
  ssr: false, loading: () => <p>Öppnar 3D-vyn…</p>,
});
const errorText = (error: unknown) => error instanceof Error ? error.message : "Åtgärden misslyckades.";
const fingerprint = (workspace: FurnitureWorkspace) => JSON.stringify(workspace);

export function FurnitureWorkspacePage({ runtimeConfig }: { runtimeConfig: PublicRuntimeConfig }) {
  const api = useMemo(() => new CustombuildApiClient(runtimeConfig.apiUrl, undefined,
    runtimeConfig.developmentToken), [runtimeConfig.apiUrl, runtimeConfig.developmentToken]);
  const [principal, setPrincipal] = useState<CurrentPrincipal>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    let active = true;
    void api.getCurrentPrincipal().then(value => { if (active) setPrincipal(value); })
      .catch(reason => { if (active) setError(errorText(reason)); });
    return () => { active = false; };
  }, [api]);
  return <main className={styles.page}>
    <header className={styles.header}>
      <Link href="/" className={styles.brand}>Custombuild</Link>
      <span>Formge din möbel</span>
      <Link href="/">Till startsidan</Link>
    </header>
    {principal ? <FurnitureStudio key={`${principal.organization_id}:${principal.user_id}`}
      api={api} principal={principal} /> : <section className={styles.intro}>
      <h1>Bord, byråer och hyllsystem</h1>
      <p>Forma möbeln, jämför material och välj tillverkning när du är redo.</p>
      {error ? <><p role="alert">{error}</p><Link href="/">Logga in från startsidan</Link></>
        : <p role="status">Öppnar din arbetsyta…</p>}
    </section>}
  </main>;
}

export function FurnitureStudio({ api, principal }: { api: CustombuildApiClient; principal: CurrentPrincipal }) {
  const [workspace, setWorkspace] = useState(() => newFurnitureWorkspace("table"));
  const [baseline, setBaseline] = useState(() => fingerprint(newFurnitureWorkspace("table")));
  const [catalog, setCatalog] = useState<FurnitureCatalog>();
  const [projects, setProjects] = useState<ProjectRead[]>([]);
  const [projectId, setProjectId] = useState<string>();
  const [revision, setRevision] = useState(0);
  const [name, setName] = useState("Mitt bord");
  const [preview, setPreview] = useState<FurniturePreview>();
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [inputErrors, setInputErrors] = useState<Record<string, string>>({});
  const [layoutEpoch, setLayoutEpoch] = useState(0);
  const [productionOpen, setProductionOpen] = useState(false);
  const [selectedPart, setSelectedPart] = useState<string>();
  const [drawersOpen, setDrawersOpen] = useState(false);
  const [exploded, setExploded] = useState(false);
  const [history, setHistory] = useState<FurnitureHistory>({ items: [], next_offset: null });
  const [pendingNavigation, setPendingNavigation] = useState<(() => void) | null>(null);
  const [exportJob, setExportJob] = useState<{ id: string; project: string; revision: number; hash: string }>();
  const [download, setDownload] = useState<{ url: string; name: string; revision: number }>();
  const [exportMessage, setExportMessage] = useState<string>();
  const mutationEpoch = useRef(0);
  const invalidInput = Object.keys(inputErrors).length > 0;
  const dirty = fingerprint(workspace) !== baseline || invalidInput;
  const mayEdit = ["owner", "admin", "designer"].includes(principal.role);
  const intent = workspace.design.intent;
  const update = (next: FurnitureWorkspace) => {
    mutationEpoch.current += 1;
    setWorkspace(next); setPreview(undefined); setError(undefined); setNotice(undefined);
    setExportJob(undefined); setDownload(undefined); setExportMessage(undefined);
  };
  const inputError = (message: string, field: string) => {
    mutationEpoch.current += 1; setPreview(undefined); setInputErrors(previous => ({ ...previous, [field]: message }));
  };
  const clearInputError = (field: string) => {
    setInputErrors(previous => Object.fromEntries(Object.entries(previous)
      .filter(([key]) => key !== field && !key.startsWith(`${field}.`))));
  };
  const updateInput = (next: FurnitureWorkspace, field: string) => {
    clearInputError(field); update(next);
  };

  useEffect(() => {
    let active = true;
    void Promise.all([api.furnitureCatalog(), api.listProjects()]).then(([c, p]) => {
      if (active) { setCatalog(c); setProjects(p.filter(item => item.furniture_type in FURNITURE_FAMILY_LABELS)); }
    }).catch(reason => { if (active) setError(errorText(reason)); });
    return () => { active = false; };
  }, [api]);

  useEffect(() => {
    let active = true;
    const epoch = mutationEpoch.current;
    const timer = setTimeout(() => {
      void api.previewFurniture(workspace).then(result => {
        if (active && epoch === mutationEpoch.current) { setPreview(result); setError(undefined); }
      }).catch(reason => {
        if (active && epoch === mutationEpoch.current) { setPreview(undefined); setError(errorText(reason)); }
      });
    }, 180);
    return () => { active = false; clearTimeout(timer); };
  }, [api, workspace]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (!download) return;
    return () => URL.revokeObjectURL(download.url);
  }, [download]);

  useEffect(() => {
    if (!exportJob) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await api.furnitureExportResult(exportJob.project, exportJob.id);
        if (!active) return;
        if (result.state === "succeeded") {
          if (result.design_hash !== exportJob.hash) throw new Error("Exporten gäller en annan design.");
          const bytes = await verifiedFurnitureDownload(result);
          if (!active) return;
          setDownload({ url: URL.createObjectURL(new Blob([bytes], { type: "application/zip" })),
            name: result.file_name ?? "custombuild-design-review.zip", revision: exportJob.revision });
          setExportMessage("Granskningspaketet är klart.");
        } else if (result.state === "failed" || result.state === "expired") {
          setExportMessage(result.message ?? "Exporten kunde inte slutföras.");
        } else {
          setExportMessage("Skapar och kontrollerar CAD-modeller, delritningar och kaplista…");
          timer = setTimeout(() => { void poll(); }, 2_000);
        }
      } catch (reason) { if (active) setExportMessage(errorText(reason)); }
    };
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [api, exportJob]);

  const navigate = (action: () => void) => {
    if (dirty) setPendingNavigation(() => action); else action();
  };
  const chooseFamily = (family: FurnitureFamily) => navigate(() => {
    const next = newFurnitureWorkspace(family);
    setInputErrors({});
    update(next); setBaseline(fingerprint(next)); setProjectId(undefined); setRevision(0);
    setHistory({ items: [], next_offset: null }); setName(`Min ${FURNITURE_FAMILY_LABELS[family].toLowerCase()}`);
    setDrawersOpen(false); setSelectedPart(undefined);
  });
  const changeDimension = (key: string, value: number) => {
    if (!Number.isSafeInteger(value)) return;
    clearInputError(`carcass.${key}`);
    update({ ...workspace, design: { ...workspace.design, intent: { ...intent, [key]: value } } });
  };
  const changeMillimetres = (key: string, raw: string) => {
    try { changeDimension(key, exactMillimetreTextToMicrometres(raw, { maximumUm: 6_000_000 })); }
    catch (reason) { inputError(errorText(reason), `carcass.${key}`); }
  };
  const openProject = (id: string) => navigate(() => {
    setBusy(true); const epoch = ++mutationEpoch.current;
    void Promise.all([api.loadFurnitureDraft(id), api.furnitureHistory(id)]).then(([draft, history]) => {
      if (epoch !== mutationEpoch.current) return;
      if (!draft.workspace) throw new Error("Projektet saknar ett möbelutkast.");
      setInputErrors({}); update(draft.workspace); setBaseline(fingerprint(draft.workspace)); setProjectId(id);
      setRevision(draft.revision); setPreview(draft.preview ?? undefined); setHistory(history);
      setName(projects.find(p => p.id === id)?.name ?? "Min möbel");
      setDrawersOpen(false); setSelectedPart(undefined);
    }).catch(reason => setError(errorText(reason))).finally(() => setBusy(false));
  });
  const save = async () => {
    if (invalidInput || !preview) return;
    setBusy(true); setError(undefined);
    try {
      let id = projectId;
      if (!id) {
        const project = await api.createProject(name.trim());
        id = project.id; setProjectId(id);
        setProjects(items => [...items, { ...project, furniture_type: intent.family }]);
      }
      const saved = await api.saveFurnitureDraft(id, revision, workspace);
      if (!saved.workspace) throw new Error("Servern returnerade inget sparat utkast.");
      update(saved.workspace); setBaseline(fingerprint(saved.workspace)); setRevision(saved.revision);
      setPreview(saved.preview ?? undefined); setNotice(`Revision ${saved.revision} är sparad.`);
      setHistory(await api.furnitureHistory(id));
    } catch (reason) { setError(errorText(reason)); }
    finally { setBusy(false); }
  };
  const exportReview = async () => {
    if (!projectId || !preview || dirty) return;
    setBusy(true); setDownload(undefined);
    try {
      const result = await api.requestFurnitureExport(projectId, revision, preview.design.design_hash);
      setExportJob({ id: result.job_id, project: projectId, revision, hash: preview.design.design_hash });
    } catch (reason) { setExportMessage(errorText(reason)); }
    finally { setBusy(false); }
  };
  const importWorkspace = async (file: File) => {
    setBusy(true); const epoch = ++mutationEpoch.current;
    try {
      if (file.size > 256 * 1024) throw new Error("Arbetsfilen får vara högst 256 kB.");
      const document = JSON.parse(await file.text()) as FurnitureWorkspace;
      const proposed = { ...document, design: { ...document.design, design_id: "furniture", revision: 1 } };
      const checked = await api.previewFurniture(proposed);
      if (epoch !== mutationEpoch.current) return;
      setInputErrors({}); update(checked.workspace); setBaseline(""); setProjectId(undefined); setRevision(0);
      setPreview(checked); setHistory({ items: [], next_offset: null });
      setName(file.name.replace(/\.json$/i, "").slice(0, 180));
      setNotice("Arbetsfilen har kontrollerats. Spara som ett nytt projekt för att skapa underlag.");
    } catch (reason) { setError(errorText(reason)); }
    finally { setBusy(false); }
  };
  const exportWorkspace = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(workspace, null, 2)+"\n"], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url;
    link.download = `custombuild-${intent.family}-revision-${revision || 1}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1_000);
  };
  const viewerParts = useMemo(() => preview ? furnitureViewerParts(preview, drawersOpen) : [], [preview, drawersOpen]);

  if (productionOpen && preview && projectId && !dirty) {
    return <FurnitureProduction api={api} principal={principal} workspace={workspace}
      designHash={preview.design.design_hash} projectName={name} onClose={() => setProductionOpen(false)} />;
  }

  return <>
    <section className={styles.titleRow}>
      <div><p className={styles.eyebrow}>Din design · {revision ? `senast sparad revision ${revision}` : "nytt utkast"}</p>
        <h1>{FURNITURE_FAMILY_LABELS[intent.family]}</h1><p>Ändra formen. Välj material och tillverkning separat.</p></div>
      <div className={styles.projectActions}>
        <label>Läs arbetsfil (JSON)<input type="file" accept=".json,application/json" disabled={busy || !mayEdit}
          onChange={e => { const file = e.target.files?.[0]; e.target.value = "";
            if (file) navigate(() => { void importWorkspace(file); }); }} /></label>
        <button disabled={busy || !preview || invalidInput} onClick={exportWorkspace}>Spara arbetsfil</button>
        <label>Öppna projekt<select aria-label="Öppna möbelprojekt" value={projectId ?? ""}
          disabled={busy} onChange={e => { if (e.target.value) openProject(e.target.value); }}>
          <option value="">Ny design</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select></label>
        <label>Projektnamn<input value={name} maxLength={180} disabled={Boolean(projectId) || busy || !mayEdit}
          onChange={e => setName(e.target.value)} /></label>
        <button className={styles.primary} disabled={!mayEdit || busy || !preview || invalidInput || !name.trim()}
          onClick={() => { void save(); }}>{busy ? "Arbetar…" : "Spara revision"}</button>
      </div>
    </section>
    {pendingNavigation ? <section role="alertdialog" aria-label="Osparad design" className={styles.notice}>
      <p>Du har osparade ändringar. Spara revisionen för att behålla dem.</p>
      <button onClick={() => setPendingNavigation(null)}>Tillbaka</button>
      <button onClick={() => { pendingNavigation(); setPendingNavigation(null); }}>Fortsätt utan att spara</button>
    </section> : null}
    {error ? <p role="alert" className={styles.error}>{error}</p> : null}
    {Object.entries(inputErrors).map(([field, message]) => <p key={field} role="alert" className={styles.error}>{message}</p>)}
    {notice ? <p role="status" className={styles.notice}>{notice}</p> : null}
    <div className={styles.layout}>
      <aside className={styles.controls}>
        <h2>Form och funktion</h2>
        <label>Möbeltyp<select aria-label="Möbeltyp" value={intent.family} disabled={busy || !mayEdit}
          onChange={e => chooseFamily(e.target.value as FurnitureFamily)}>
          {Object.entries(FURNITURE_FAMILY_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select></label>
        <fieldset disabled={busy || !mayEdit}><legend>Stommått i millimeter</legend>
          {([['width_um', 'Bredd'], ['height_um', 'Höjd'], ['depth_um', 'Djup']] as const).map(([key, label]) =>
            <label key={key}>{label}<input aria-label={`${label} (mm)`} type="number" step="0.001" min="1" max="6000"
              value={intent[key]/1_000} onChange={e => changeMillimetres(key, e.target.value)} /></label>)}
          {intent.family === "table" ? <>
            <label>Gavelns indrag (mm)<input type="number" step="1" min="0" value={(intent.end_inset_um ?? 0)/1_000}
              onChange={e => changeMillimetres("end_inset_um", e.target.value)} /></label>
            <label>Sargens höjd (mm)<input type="number" step="1" min="40" max="300" value={(intent.stretcher_height_um ?? 0)/1_000}
              onChange={e => changeMillimetres("stretcher_height_um", e.target.value)} /></label>
          </> : intent.family === "chest_of_drawers" ? <>
            <label>Antal lådor<input type="number" min="1" max="8" value={intent.drawer_count}
              onChange={e => changeDimension("drawer_count", Number(e.target.value))} /></label>
            <label>Frontspel (mm)<input type="number" min="1" max="10" step="0.1" value={(intent.front_gap_um ?? 0)/1_000}
              onChange={e => changeMillimetres("front_gap_um", e.target.value)} /></label>
          </> : <>
            <label>Hyllrader<input type="number" min="0" max="40" value={intent.shelf_count}
              onChange={e => changeDimension("shelf_count", Number(e.target.value))} /></label>
            <label>Avdelare<input type="number" min="0" max="16" value={intent.divider_count}
              onChange={e => changeDimension("divider_count", Number(e.target.value))} /></label>
          </>}
          <ShelvingLayoutEditor key={`layout-${projectId}-${revision}-${layoutEpoch}`} workspace={workspace} onChange={updateInput} onError={inputError} />
          <InstallationEditor workspace={workspace} onChange={updateInput} onError={inputError} />
          <label>{intent.family === "table" ? "Last på skivan" : intent.family === "chest_of_drawers" ? "Last per låda" : "Last per hyllplan i ett fack"} (kg)
            <input type="number" min="0" max="500" step="0.1"
              value={((intent.top_load_n ?? intent.drawer_load_n ?? intent.shelf_load_n ?? 0)/9.80665).toFixed(1)}
              onChange={e => changeDimension(intent.family === "table" ? "top_load_n" : intent.family === "chest_of_drawers" ? "drawer_load_n" : "shelf_load_n",
                Math.round(Number(e.target.value)*9.80665))} /></label>
        </fieldset>
      </aside>
      <section className={styles.model} aria-label="Möbelns förhandsvisning">
        <div className={styles.viewerToolbar}><strong>{preview ? `${preview.design.parts.length} delar` : "Beräknar…"}</strong>
          <button aria-pressed={exploded} onClick={() => setExploded(v => !v)}>Sprängvy</button>
          {intent.family === "chest_of_drawers" ? <button aria-pressed={drawersOpen}
            onClick={() => setDrawersOpen(v => !v)}>{drawersOpen ? "Stäng lådorna" : "Öppna lådorna"}</button> : null}
        </div>
        <div className={styles.canvas}>
          {preview ? <Viewer parts={viewerParts} designSize={{ widthMm: intent.width_um/1_000,
            heightMm: intent.height_um/1_000, depthMm: intent.depth_um/1_000 }}
            viewMode="perspective" exploded={exploded} transparent={false} isolateSelection={false}
            selectedPartId={selectedPart} onSelectPart={setSelectedPart} presentation="studio" />
            : <p role="status">{error ? "Kontrollera måtten för att visa möbeln." : "Räknar om delarna…"}</p>}
        </div>
        <p className={styles.caption}>3D-vyn visar delarnas placering. Bearbetningsdetaljer finns i delritningarna.</p>
      </section>
      <aside className={styles.profiles}>
        <h2>Material, beslag och verkstad</h2>
        <p>Du kan spara designen innan du väljer verkstad.</p>
        {catalog ? <ProfileEditor key={fingerprint(workspace)}
          api={api} workspace={workspace} catalog={catalog} disabled={busy || !mayEdit} onApply={update} /> : <p>Läser profiler…</p>}
      </aside>
    </div>
    <section className={styles.review}>
      <div><h2>Kontroll och underlag</h2>
        <p>Designunderlag för granskning. Skärande CAM kräver verifierade beslag, konstruktion och maskin.</p>
        {preview ? <p>{(preview.design.total_weight_g/1_000).toFixed(1)} kg beräknad bruttovikt · {dirty ? "Osparade ändringar" : revision ? "Sparad revision" : "Nytt utkast"}</p> : null}
      </div>
      {intent.family === "shelving" ? <div>
        <button className={styles.primary} disabled={busy || dirty || !projectId || !preview || !workspace.manufacturing
          || preview.workshop_handoff?.dimensions.state === "requires_resolution"}
          onClick={() => setProductionOpen(true)}>Förbered tillverkning</button>
        <p>{dirty || !revision ? "Spara möbeln först." : !workspace.manufacturing
          ? "Välj och spara en planeringsprofil för att börja bereda. Verklig verkstad kan väljas senare."
          : preview?.workshop_handoff?.dimensions.state === "requires_resolution"
            ? "Kundmått och listkonstruktion behöver lösas innan tillverkningsberedningen öppnas."
            : "Öppna råmaterial, nesting, foggranskning och maskinens CAM-flöde för den sparade möbeln."}</p>
      </div> : null}
      <button className={styles.primary} disabled={!mayEdit || busy || dirty || !projectId || !preview}
        onClick={() => { void exportReview(); }}>Skapa granskningspaket</button>
      {exportMessage ? <p role="status">{exportMessage}</p> : null}
      {download ? <a href={download.url} download={download.name}>Hämta STEP, GLB, DXF, delritningar och kaplista · revision {download.revision}</a> : null}
      <div className={styles.rules}>{preview?.rules.evaluations.map(rule => <details key={rule.rule_id}>
        <summary><span className={rule.status === "PASS" ? styles.pass : styles.requiresReview}>
          {rule.status === "PASS" ? "Kontrollerat" : "Behöver granskas"}</span>{rule.title}</summary>
        <p>{rule.detail}</p>
      </details>)}</div>
      {preview?.manufacturing.issues.map((issue, index) => <p className={styles.error} key={`${issue.code}-${index}`}>{issue.message}</p>)}
      {preview ? <FurnitureStockRequirements preview={preview} /> : null}
    </section>
    {history.items.length ? <section className={styles.history}>
      <h2>Sparade revisioner</h2><p>Öppna en tidigare design och spara fortsatta ändringar som en ny revision.</p>
      {history.items.map(item => <button key={item.id} disabled={busy || !mayEdit} onClick={() => navigate(() => {
        setInputErrors({}); setLayoutEpoch(v => v+1); update(item.workspace); setNotice(`Revision ${item.revision} öppnad. Nästa sparning skapar en ny revision.`);
      })}>Revision {item.revision} · {new Date(item.created_at).toLocaleString("sv-SE")}</button>)}
      {history.next_offset !== null && projectId ? <button onClick={() => {
        void api.furnitureHistory(projectId, history.next_offset ?? 0).then(next => setHistory(previous => ({
          items: [...previous.items, ...next.items], next_offset: next.next_offset,
        }))).catch(reason => setError(errorText(reason)));
      }}>Visa äldre revisioner</button> : null}
    </section> : null}
  </>;
}

function ProfileEditor({ api, workspace, catalog, disabled, onApply }: {
  api: CustombuildApiClient; workspace: FurnitureWorkspace; catalog: FurnitureCatalog;
  disabled: boolean; onApply: (next: FurnitureWorkspace) => void;
}) {
  const [proposed, setProposed] = useState(workspace);
  const [comparison, setComparison] = useState<FurnitureProfileComparison>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);
  const change = (next: FurnitureWorkspace) => {
    epoch.current += 1; setProposed(next); setComparison(undefined); setError(undefined); setBusy(false);
  };
  const withMillimetres = (raw: string, apply: (value: number) => FurnitureWorkspace) => {
    try { change(apply(exactMillimetreTextToMicrometres(raw, { minimumUm: 1, maximumUm: 6_000_000 }))); }
    catch (reason) { epoch.current += 1; setBusy(false); setComparison(undefined); setError(errorText(reason)); }
  };
  const check = async (event: FormEvent) => {
    event.preventDefault(); const currentEpoch = ++epoch.current;
    setBusy(true); setError(undefined);
    try {
      const result = await api.compareFurnitureProfiles(workspace, proposed);
      if (epoch.current === currentEpoch) setComparison(result);
    } catch (reason) { if (epoch.current === currentEpoch) setError(errorText(reason)); }
    finally { if (epoch.current === currentEpoch) setBusy(false); }
  };
  return <form onSubmit={event => { void check(event); }}>
    <fieldset disabled={disabled}><legend>Jämför ett profilbyte</legend>
      {(["material", ...(workspace.design.intent.family === "table" ? [] : ["back_material"])] as ("material" | "back_material")[]).map(key => <div key={key}>
        <label>{key === "material" ? "Skivmaterial" : "Rygg och lådbotten"}<select value={proposed.design[key].material_id}
          onChange={e => {
            const material = catalog.materials.find(item => item.material_id === e.target.value);
            if (!material) return;
            change({ ...proposed, design: { ...proposed.design, [key]: { ...proposed.design[key],
              material_id: material.material_id, version: material.version,
              measured_thickness_um: material.nominal_thickness_um, batch_id: null } } });
          }}>{catalog.materials.filter(m => m.nominal_thickness_um === (key === "material" ? 18_000 : 6_000))
            .map(m => <option key={m.material_id} value={m.material_id}>{m.name}</option>)}</select></label>
        <label>{key === "material" ? "Uppmätt skivtjocklek (mm)" : "Uppmätt rygg-/bottentjocklek (mm)"}
          <input type="number" min="1" step="0.001" value={proposed.design[key].measured_thickness_um/1_000}
            onChange={e => withMillimetres(e.target.value, value => ({ ...proposed, design: { ...proposed.design, [key]: { ...proposed.design[key],
              measured_thickness_um: value } } }))} /></label>
        <label>Batch-ID <span>(valfritt)</span><input maxLength={80} value={proposed.design[key].batch_id ?? ""}
          onChange={e => change({ ...proposed, design: { ...proposed.design, [key]: { ...proposed.design[key],
            batch_id: e.target.value || null } } })} /></label>
      </div>)}
      {workspace.design.hardware ? <label>Beslagslayout<select value={proposed.design.hardware?.catalog_id}
        onChange={e => {
          const hardware = catalog.hardware.find(h => h.catalog_id === e.target.value);
          if (hardware) change({ ...proposed, design: { ...proposed.design,
            hardware: { catalog_id: hardware.catalog_id, version: hardware.version } } });
        }}>{catalog.hardware.filter(h => h.family === workspace.design.intent.family)
          .map(h => <option key={h.catalog_id} value={h.catalog_id}>{h.name}</option>)}</select></label> : null}
      <label>Tillverkningsprofil<select value={proposed.manufacturing?.machine_profile_id ?? ""}
        onChange={e => {
          const machine = catalog.machines.find(m => m.profile_id === e.target.value);
          change({ ...proposed, manufacturing: machine ? { machine_profile_id: machine.profile_id,
            machine_profile_version: machine.version, stock_width_um: 1_220_000,
            stock_height_um: 2_440_000, stock_grain_axis: null } : null });
        }}><option value="">Välj verkstad senare</option>{catalog.machines.map(m =>
          <option key={m.profile_id} value={m.profile_id}>{m.name} · referensprofil</option>)}</select></label>
      {proposed.manufacturing ? <>
        <label>Råskivans bredd (mm)<input type="number" min="1" step="0.001" value={proposed.manufacturing.stock_width_um/1_000}
          onChange={e => withMillimetres(e.target.value, value => ({ ...proposed, manufacturing: { ...proposed.manufacturing!, stock_width_um: value } }))} /></label>
        <label>Råskivans höjd (mm)<input type="number" min="1" step="0.001" value={proposed.manufacturing.stock_height_um/1_000}
          onChange={e => withMillimetres(e.target.value, value => ({ ...proposed, manufacturing: { ...proposed.manufacturing!, stock_height_um: value } }))} /></label>
        <label>Kantmarginal per sida (mm)<input type="number" min="0" max="100" step="0.001" value={(proposed.manufacturing.edge_margin_um ?? 0)/1_000}
          onChange={e => {
            try { change({ ...proposed, manufacturing: { ...proposed.manufacturing!,
              edge_margin_um: exactMillimetreTextToMicrometres(e.target.value, { minimumUm: 0, maximumUm: 100_000 }) } }); }
            catch (reason) { epoch.current += 1; setComparison(undefined); setError(errorText(reason)); }
          }} /></label>
        <label>Fiberriktning på råskivan<select value={proposed.manufacturing.stock_grain_axis ?? ""}
          onChange={e => change({ ...proposed, manufacturing: { ...proposed.manufacturing!,
            stock_grain_axis: e.target.value === "x" ? "x" : e.target.value === "y" ? "y" : null } })}>
          <option value="">Inte angiven</option><option value="x">Längs bredden</option><option value="y">Längs höjden</option>
        </select></label>
      </> : null}
      <button type="submit" disabled={busy || fingerprint(workspace) === fingerprint(proposed)}>
        {busy ? "Kontrollerar…" : "Kontrollera profilbyte"}</button>
    </fieldset>
    {error ? <p role="alert" className={styles.error}>{error}</p> : null}
    {comparison ? <div className={styles.proposal} aria-live="polite">
      <strong>{comparison.can_apply ? "Konsekvenser av profilbytet" : "Profilen fungerar inte med designen"}</strong>
      <p>{comparison.message}</p>
      {comparison.can_apply ? <><p>{comparison.changed_part_ids?.length ?? 0} delar räknas om. Yttermåtten behålls.</p>
        <p>{comparison.invalidated_reviews.includes("construction") ? "Konstruktion och tillverkning behöver granskas på nytt."
          : comparison.invalidated_reviews.length ? "Tillverkningen behöver beredas och granskas på nytt." : "Inga tillverkningsberoenden ändras."}</p>
        {comparison.proposed?.manufacturing.issues.map((issue, i) => <p key={i}>{issue.message}</p>)}
        <button type="button" className={styles.primary} disabled={disabled}
          onClick={() => { if (comparison.proposed) onApply(comparison.proposed.workspace); }}>Använd profilbytet i designen</button>
      </> : null}
    </div> : null}
  </form>;
}
