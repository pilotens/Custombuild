"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { CustombuildApiClient, type CurrentPrincipal, type ProjectRead } from "@/lib/api-client";
import {
  FURNITURE_FAMILY_LABELS, furnitureViewerParts, newFurnitureWorkspace, verifiedFurnitureDownload,
  type FurnitureCatalog, type FurnitureFamily, type FurnitureHistory, type FurniturePreview,
  type FurnitureProfileComparison, type FurnitureWorkspace,
} from "@/lib/furniture-workspace";
import { beginOidcLogin, oidcConfigured } from "@/lib/auth-client";
import { furnitureDraftRecoveryKey, parseFurnitureDraftRecovery, replaceFurnitureRecovery,
  type FurnitureDraftRecovery, type FurnitureProfileDraft } from "@/lib/furniture-draft-recovery";
import { FurnitureTrialReadinessPanel } from "./furniture-trial-readiness";
import type { PublicRuntimeConfig } from "@/lib/runtime-config";
import { exactMillimetreTextToMicrometres } from "@/lib/workshop-production-context";
import styles from "./furniture-workspace.module.css";
import { FurnitureProduction } from "./furniture-production";
import { FurnitureStockRequirements, InstallationEditor, ShelvingLayoutEditor } from "./furniture-dimensions";
import { FurnitureRuleValues } from "./furniture-rule-values";
import { FurnitureShelfSuggestionPanel } from "./furniture-shelf-suggestion";
import { FurnitureStockEditor, FurnitureStockPlan } from "./furniture-stock-planning";

const Viewer = dynamic(() => import("./furniture-viewer"), {
  ssr: false, loading: () => <p>Öppnar 3D-vyn…</p>,
});
const errorText = (error: unknown) => error instanceof Error ? error.message : "Åtgärden misslyckades.";
const fingerprint = (workspace: FurnitureWorkspace) => JSON.stringify(workspace);
const recoveredInputErrors = (drafts?: Record<string, string>, errors?: Record<string, string>) => ({
  ...errors, ...Object.fromEntries(Object.keys(drafts ?? {}).map(key => [key, errors?.[key] ?? "Kontrollera det återställda fältet innan du sparar."])),
});

export function FurnitureWorkspacePage({ runtimeConfig }: { runtimeConfig: PublicRuntimeConfig }) {
  const api = useMemo(() => new CustombuildApiClient(runtimeConfig.apiUrl, undefined,
    runtimeConfig.developmentToken), [runtimeConfig.apiUrl, runtimeConfig.developmentToken]);
  const [principal, setPrincipal] = useState<CurrentPrincipal>();
  const [error, setError] = useState<string>();
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    void api.getCurrentPrincipal().then(value => { if (active) setPrincipal(value); })
      .catch(reason => { if (active) setError(errorText(reason)); });
    return () => { active = false; };
  }, [api, retry]);
  const login = oidcConfigured(runtimeConfig) ? () => {
    void beginOidcLogin(runtimeConfig, "/furniture").catch(reason => setError(errorText(reason)));
  } : undefined;
  return <main className={styles.page}>
    <header className={styles.header}>
      <Link href="/" className={styles.brand}>Custombuild</Link>
      <span>Formge din möbel</span>
      <Link href="/">Till startsidan</Link>
    </header>
    {principal ? <FurnitureStudio key={`${api.baseUrl}:${principal.organization_id}:${principal.user_id}`}
      api={api} principal={principal} onLogin={login} /> : <section className={styles.intro}>
      <h1>Bord, byråer och hyllsystem</h1>
      <p>Forma möbeln, jämför material och välj tillverkning när du är redo.</p>
      {error ? <><p role="alert">{error}</p><button onClick={() => setRetry(value => value+1)}>Försök igen</button>
        {login ? <button onClick={login}>Logga in</button> : <Link href="/">Logga in från startsidan</Link>}</>
        : <p role="status">Öppnar din arbetsyta…</p>}
    </section>}
  </main>;
}

export function FurnitureStudio({ api, principal, onLogin }: {
  api: CustombuildApiClient; principal: CurrentPrincipal; onLogin?: () => void;
}) {
  const recoveryKey = furnitureDraftRecoveryKey(api.baseUrl, principal);
  const [initialRecovery] = useState(() => {
    try { return { raw: window.localStorage.getItem(recoveryKey), error: undefined as string | undefined }; }
    catch { return { raw: null, error: "Webbläsaren kan inte spara återställningskopior. Hämta en återställningsfil för att behålla ditt arbete." }; }
  });
  const [pendingRecovery, setPendingRecovery] = useState(initialRecovery.raw);
  const persistedRecovery = useRef(initialRecovery.raw);
  const [storageError, setStorageError] = useState(initialRecovery.error);
  const [refresh, setRefresh] = useState(0);
  const [inputDrafts, setInputDrafts] = useState<Record<string, string>>({});
  const [profileDraft, setProfileDraft] = useState<FurnitureProfileDraft>();
  const [restoredProfile, setRestoredProfile] = useState<FurnitureProfileDraft>();
  const [nameBaseline, setNameBaseline] = useState("Mitt bord");
  const navigationApproved = useRef(false);
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
  const [inputEpoch, setInputEpoch] = useState(0);
  const [profileDirty, setProfileDirty] = useState(false);
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
  const dirty = fingerprint(workspace) !== baseline || invalidInput || profileDirty || (!projectId && name !== nameBaseline);
  const profileDirtyChanged = useCallback((value: boolean) => setProfileDirty(value), []);
  const profileDraftChanged = useCallback((value: FurnitureProfileDraft | undefined) => setProfileDraft(value), []);
  const mayEdit = ["owner", "admin", "designer"].includes(principal.role);
  const intent = workspace.design.intent;
  const linearShelfLoad = intent.family === "shelving" && intent.shelf_load_basis === "per_metre";
  const loadKey = intent.family === "table" ? "top_load_n" : intent.family === "chest_of_drawers" ? "drawer_load_n"
    : linearShelfLoad ? "shelf_load_per_metre_n" : "shelf_load_n";
  const update = (next: FurnitureWorkspace) => {
    mutationEpoch.current += 1;
    setWorkspace(next); setPreview(undefined); setError(undefined); setNotice(undefined);
    setProfileDirty(false); setProfileDraft(undefined); setRestoredProfile(undefined);
    setExportJob(undefined); setDownload(undefined); setExportMessage(undefined);
  };
  const inputError = (message: string, field: string, raw?: string) => {
    mutationEpoch.current += 1; setPreview(undefined); setInputErrors(previous => ({ ...previous, [field]: message }));
    if (raw !== undefined) setInputDrafts(previous => ({ ...previous, [field]: raw }));
  };
  const clearInputError = (field: string) => {
    const remaining = (previous: Record<string, string>) => Object.fromEntries(Object.entries(previous)
      .filter(([key]) => key !== field && !key.startsWith(`${field}.`)));
    setInputErrors(remaining); setInputDrafts(remaining);
  };
  const resetInputDrafts = () => {
    setInputErrors({}); setInputDrafts({}); setInputEpoch(value => value+1);
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
  }, [api, refresh]);

  useEffect(() => {
    if (pendingRecovery) return;
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
  }, [api, workspace, refresh, pendingRecovery]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      if (!navigationApproved.current) { event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (!dirty || productionOpen) return;
    const guard = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!(anchor instanceof HTMLAnchorElement) || anchor.hasAttribute("download") || anchor.target === "_blank") return;
      const target = new URL(anchor.href, window.location.href);
      if (target.origin !== window.location.origin || target.pathname === window.location.pathname) return;
      event.preventDefault(); event.stopPropagation();
      setPendingNavigation(() => () => { navigationApproved.current = true; window.location.assign(target.href); });
    };
    document.addEventListener("click", guard, true);
    return () => document.removeEventListener("click", guard, true);
  }, [dirty, productionOpen]);

  useEffect(() => {
    if (pendingRecovery) return;
    let active = true;
    void Promise.resolve().then(() => {
      if (!active) return;
      try {
        const snapshot: FurnitureDraftRecovery = { version: 1, workspace, projectId, revision,
          name: name.trim() || "Min möbel", updatedAt: new Date().toISOString(), inputDrafts, inputErrors, profile: profileDraft };
        const next = dirty ? JSON.stringify(snapshot) : null;
        if (next && new TextEncoder().encode(next).length > 256 * 1024) throw new Error("Återställningskopian är för stor.");
        replaceFurnitureRecovery(window.localStorage, recoveryKey, persistedRecovery.current, next);
        persistedRecovery.current = next; setStorageError(undefined);
      } catch (reason) { setStorageError(`${errorText(reason)} Hämta en återställningsfil för att behålla ditt arbete.`); }
    });
    return () => { active = false; };
  }, [dirty, workspace, projectId, revision, name, inputDrafts, inputErrors, profileDraft, recoveryKey, pendingRecovery]);

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
    resetInputDrafts();
    update(next); setBaseline(fingerprint(next)); setProjectId(undefined); setRevision(0);
    setHistory({ items: [], next_offset: null });
    const nextName = `Min ${FURNITURE_FAMILY_LABELS[family].toLowerCase()}`; setName(nextName); setNameBaseline(nextName);
    setDrawersOpen(false); setSelectedPart(undefined);
  });
  const changeDimension = (key: string, value: number) => {
    if (!Number.isSafeInteger(value)) return;
    clearInputError(`carcass.${key}`);
    update({ ...workspace, design: { ...workspace.design, intent: { ...intent, [key]: value } } });
  };
  const changeCount = (key: string, raw: string, minimum: number, maximum: number) => {
    const value = Number(raw);
    if (!raw.trim() || !Number.isSafeInteger(value) || value < minimum || value > maximum) {
      inputError(`Ange ett heltal mellan ${minimum} och ${maximum}.`, `carcass.${key}`, raw);
      return;
    }
    changeDimension(key, value);
  };
  const changeLoad = (raw: string) => {
    const kilograms = Number(raw);
    if (!raw.trim() || !Number.isFinite(kilograms) || kilograms < 0 || kilograms > 500) {
      inputError("Ange en last mellan 0 och 500 kg. Tomt betyder inte noll last.", `carcass.${loadKey}`, raw);
      return;
    }
    changeDimension(loadKey, Math.round(kilograms * 9.80665));
  };
  const changeMillimetres = (key: string, raw: string) => {
    try {
      const limits = key === "end_inset_um" ? { minimumUm: 0, maximumUm: 500_000 }
        : key === "stretcher_height_um" ? { minimumUm: 40_000, maximumUm: 300_000 }
        : key === "front_gap_um" ? { minimumUm: 1_000, maximumUm: 10_000 }
        : { minimumUm: 1, maximumUm: 6_000_000 };
      changeDimension(key, exactMillimetreTextToMicrometres(raw, limits));
    }
    catch (reason) { inputError(errorText(reason), `carcass.${key}`, raw); }
  };
  const openProject = (id: string) => navigate(() => {
    setBusy(true); const epoch = ++mutationEpoch.current;
    void Promise.all([api.loadFurnitureDraft(id), api.furnitureHistory(id)]).then(([draft, history]) => {
      if (epoch !== mutationEpoch.current) return;
      if (!draft.workspace) throw new Error("Projektet saknar ett möbelutkast.");
      resetInputDrafts(); update(draft.workspace); setBaseline(fingerprint(draft.workspace)); setProjectId(id);
      setRevision(draft.revision); setPreview(draft.preview ?? undefined); setHistory(history);
      setName(projects.find(p => p.id === id)?.name ?? "Min möbel");
      setDrawersOpen(false); setSelectedPart(undefined);
    }).catch(reason => setError(errorText(reason))).finally(() => setBusy(false));
  });
  const save = async () => {
    if (invalidInput || profileDirty || !preview) return;
    setBusy(true); setError(undefined);
    try {
      let id = projectId;
      if (!id) {
        const project = await api.createProject(name.trim());
        id = project.id; setProjectId(id); setBaseline("");
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
      const raw = await file.text();
      const parsed = JSON.parse(raw) as FurnitureWorkspace | FurnitureDraftRecovery;
      const recovered = "version" in parsed ? parseFurnitureDraftRecovery(raw) : undefined;
      const document = recovered?.workspace ?? parsed as FurnitureWorkspace;
      const proposed = { ...document, design: { ...document.design, design_id: "furniture", revision: 1 } };
      const checked = await api.validateFurnitureWorkspace(proposed);
      const checkedProfile = recovered?.profile ? await api.validateFurnitureWorkspace({ ...recovered.profile.proposed,
        design: { ...recovered.profile.proposed.design, design_id: "furniture", revision: 1 } }) : undefined;
      if (epoch !== mutationEpoch.current) return;
      resetInputDrafts(); update(checked.workspace); setBaseline(""); setProjectId(undefined); setRevision(0);
      setPreview(undefined); setHistory({ items: [], next_offset: null });
      setName(file.name.replace(/\.json$/i, "").slice(0, 180));
      if (recovered) {
        setInputDrafts(recovered.inputDrafts ?? {}); setInputErrors(recoveredInputErrors(recovered.inputDrafts, recovered.inputErrors));
        if (recovered.profile && checkedProfile) setRestoredProfile({ ...recovered.profile,
          proposed: checkedProfile.workspace,
          inputErrors: recoveredInputErrors(recovered.profile.inputDrafts, recovered.profile.inputErrors) });
      }
      setNotice("Arbetsfilen har kontrollerats och öppnats som ett nytt utkast. Rätta eventuella formfel innan du sparar och skapar underlag.");
    } catch (reason) { setError(errorText(reason)); }
    finally { setBusy(false); }
  };
  const exportWorkspace = () => {
    if (profileDirty || invalidInput) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(workspace, null, 2)+"\n"], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url;
    link.download = `custombuild-${intent.family}-${preview ? "arbetsfil" : "ogranskat-utkast"}-revision-${revision || 1}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1_000);
  };
  const downloadRecovery = (raw?: string) => {
    const content = raw ?? JSON.stringify({ version: 1, workspace, projectId, revision, name: name.trim() || "Min möbel",
      updatedAt: new Date().toISOString(), inputDrafts, inputErrors, profile: profileDraft } satisfies FurnitureDraftRecovery, null, 2);
    const url = URL.createObjectURL(new Blob([content], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "custombuild-aterstallning.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1_000);
  };
  const restoreRecovery = async () => {
    if (!pendingRecovery) return;
    setBusy(true); setError(undefined);
    try {
      const recovered = parseFurnitureDraftRecovery(pendingRecovery);
      const checked = await api.validateFurnitureWorkspace(recovered.workspace);
      const checkedProfile = recovered.profile ? await api.validateFurnitureWorkspace(recovered.profile.proposed) : undefined;
      if (recovered.projectId) await api.loadFurnitureDraft(recovered.projectId);
      resetInputDrafts(); update(checked.workspace); setBaseline("");
      setProjectId(recovered.projectId); setRevision(recovered.revision); setName(recovered.name);
      setInputDrafts(recovered.inputDrafts ?? {}); setInputErrors(recoveredInputErrors(recovered.inputDrafts, recovered.inputErrors));
      setRestoredProfile(recovered.profile && checkedProfile ? { ...recovered.profile, proposed: checkedProfile.workspace,
        inputErrors: recoveredInputErrors(recovered.profile.inputDrafts, recovered.profile.inputErrors) } : undefined);
      setPreview(undefined); setHistory({ items: [], next_offset: null }); setPendingRecovery(null);
      setNotice("Utkastet är återställt och behöver sparas. Profilförslag och felaktiga fält måste kontrolleras innan de används.");
    } catch (reason) { setError(errorText(reason)); }
    finally { setBusy(false); }
  };
  const discardRecovery = () => {
    try {
      replaceFurnitureRecovery(window.localStorage, recoveryKey, persistedRecovery.current, null);
      persistedRecovery.current = null; setPendingRecovery(null); setStorageError(undefined); setError(undefined);
    } catch (reason) { setStorageError(errorText(reason)); }
  };
  const viewerParts = useMemo(() => preview ? furnitureViewerParts(preview, drawersOpen) : [], [preview, drawersOpen]);

  if (productionOpen && preview && projectId && !dirty) {
    return <FurnitureProduction api={api} principal={principal} workspace={workspace}
      designHash={preview.design.design_hash} projectName={name} onClose={() => setProductionOpen(false)} />;
  }

  return <>
    {pendingRecovery ? <section role="region" aria-label="Återställ ditt utkast" className={styles.notice}>
      <h2>Återställ ditt utkast</h2><p>Det finns arbete från ett tidigare besök. Återställningskopian är inte en sparad revision eller ett tillverkningsgodkännande.</p>
      <button disabled={busy} onClick={() => { void restoreRecovery(); }}>Återställ utkast</button>
      <button onClick={() => downloadRecovery(pendingRecovery)}>Hämta återställningskopian</button>
      <button disabled={busy} onClick={discardRecovery}>Ta bort återställningskopian</button>
    </section> : null}
    {storageError ? <p role="alert" className={styles.error}>{storageError}</p> : null}
    {error ? <p role="alert" className={styles.error}>{error} <button onClick={() => setRefresh(value => value+1)}>Försök igen</button>
      {onLogin ? <button onClick={onLogin}>Logga in igen</button> : null}</p> : null}
    <fieldset className={styles.editorFrame} disabled={Boolean(pendingRecovery)}>
    <section className={styles.titleRow}>
      <div><p className={styles.eyebrow}>Din design · {revision ? `senast sparad revision ${revision}` : "nytt utkast"}</p>
        <h1>{FURNITURE_FAMILY_LABELS[intent.family]}</h1><p>Ändra formen. Välj material och tillverkning separat.</p>
        <p>{intent.family === "shelving" ? "Hyllsystem har ett flöde för tillverkningsberedning. Verkligt material, fogar och maskin måste verifieras."
          : "Bord och byråer kan formges och exporteras för granskning. Tillverkningsstöd med verifierade beslag och bearbetningar saknas ännu."}</p></div>
      <div className={styles.projectActions}>
        <label>Läs arbetsfil (JSON)<input type="file" accept=".json,application/json" disabled={busy || !mayEdit}
          onChange={e => { const file = e.target.files?.[0]; e.target.value = "";
            if (file) navigate(() => { void importWorkspace(file); }); }} /></label>
        <button disabled={busy || invalidInput || profileDirty} onClick={exportWorkspace}>Spara arbetsfil</button>
        <button onClick={() => downloadRecovery()}>Spara återställningsfil</button>
        <label>Öppna projekt<select aria-label="Öppna möbelprojekt" value={projectId ?? ""}
          disabled={busy} onChange={e => { if (e.target.value) openProject(e.target.value); }}>
          <option value="">Ny design</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select></label>
        <label>Projektnamn<input value={name} maxLength={180} disabled={Boolean(projectId) || busy || !mayEdit}
          onChange={e => setName(e.target.value)} /></label>
        <button className={styles.primary} disabled={!mayEdit || busy || !preview || invalidInput || profileDirty || !name.trim()}
          onClick={() => { void save(); }}>{busy ? "Arbetar…" : "Spara revision"}</button>
      </div>
    </section>
    {pendingNavigation ? <section role="alertdialog" aria-label="Osparad design" className={styles.notice}>
      <p>{profileDirty
        ? "Du har ett profilförslag som inte är tillämpat. Gå tillbaka, använd profilbytet och spara revisionen för att behålla det."
        : "Du har osparade ändringar. Spara revisionen för att behålla dem."}</p>
      <button onClick={() => setPendingNavigation(null)}>Tillbaka</button>
      <button onClick={() => { pendingNavigation(); setPendingNavigation(null); }}>Fortsätt utan att spara</button>
    </section> : null}
    {Object.entries(inputErrors).map(([field, message]) => <p key={field} role="alert" className={styles.error}>{message}</p>)}
    {notice ? <p role="status" className={styles.notice}>{notice}</p> : null}
    {profileDirty ? <p role="status" className={styles.notice}>
      Kontrollera och använd profilbytet, eller återställ profilförslaget, innan du ändrar formen, sparar eller skapar underlag.
    </p> : null}
    <div className={styles.layout}>
      <aside className={styles.controls}>
        <h2>Form och funktion</h2>
        <label>Möbeltyp<select aria-label="Möbeltyp" value={intent.family} disabled={busy || !mayEdit}
          onChange={e => chooseFamily(e.target.value as FurnitureFamily)}>
          {Object.entries(FURNITURE_FAMILY_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select></label>
        <fieldset disabled={busy || !mayEdit || profileDirty}><legend>Stommått i millimeter</legend>
          {([['width_um', 'Bredd'], ['height_um', 'Höjd'], ['depth_um', 'Djup']] as const).map(([key, label]) =>
            <label key={key}>{label}<input aria-label={`${label} (mm)`} type="number" step="0.001" min="1" max="6000"
              value={inputDrafts[`carcass.${key}`] ?? intent[key]/1_000} onChange={e => changeMillimetres(key, e.target.value)} /></label>)}
          {intent.family === "table" ? <>
            <label>Gavelns indrag (mm)<input type="number" step="1" min="0" max="500" value={inputDrafts["carcass.end_inset_um"] ?? (intent.end_inset_um ?? 0)/1_000}
              onChange={e => changeMillimetres("end_inset_um", e.target.value)} /></label>
            <label>Sargens höjd (mm)<input type="number" step="1" min="40" max="300" value={inputDrafts["carcass.stretcher_height_um"] ?? (intent.stretcher_height_um ?? 0)/1_000}
              onChange={e => changeMillimetres("stretcher_height_um", e.target.value)} /></label>
          </> : intent.family === "chest_of_drawers" ? <>
            <label>Antal lådor<input type="number" min="1" max="8" value={inputDrafts["carcass.drawer_count"] ?? intent.drawer_count}
              onChange={e => changeCount("drawer_count", e.target.value, 1, 8)} /></label>
            <label>Frontspel (mm)<input type="number" min="1" max="10" step="0.1" value={inputDrafts["carcass.front_gap_um"] ?? (intent.front_gap_um ?? 0)/1_000}
              onChange={e => changeMillimetres("front_gap_um", e.target.value)} /></label>
          </> : <>
            <label>Hyllrader<input type="number" min="0" max="40" value={inputDrafts["carcass.shelf_count"] ?? intent.shelf_count}
              onChange={e => changeCount("shelf_count", e.target.value, 0, 40)} /></label>
            <label>Avdelare<input type="number" min="0" max="16" value={inputDrafts["carcass.divider_count"] ?? intent.divider_count}
              onChange={e => changeCount("divider_count", e.target.value, 0, 16)} /></label>
          </>}
          <ShelvingLayoutEditor key={`layout-${projectId}-${revision}-${inputEpoch}`} workspace={workspace} inputDrafts={inputDrafts} onChange={updateInput} onError={inputError} />
          <InstallationEditor workspace={workspace} inputDrafts={inputDrafts} onChange={updateInput} onError={inputError} />
          {intent.family === "shelving" ? <label>Hur anges hyllasten?<select value={intent.shelf_load_basis ?? "per_row"}
            onChange={e => updateInput({ ...workspace, design: { ...workspace.design, intent: {
              ...intent, shelf_load_basis: e.target.value as "per_row" | "per_metre",
              shelf_load_per_metre_n: intent.shelf_load_per_metre_n ?? Math.ceil((intent.shelf_load_n ?? 0)*1_000_000/intent.width_um),
            } } }, "shelf-load")}>
            <option value="per_row">Total last per hel hyllrad</option>
            <option value="per_metre">Last per meter hyllrad</option>
          </select></label> : null}
          <label>{intent.family === "table" ? "Last på skivan" : intent.family === "chest_of_drawers" ? "Last per låda"
            : linearShelfLoad ? "Last per meter hyllrad" : "Last per hel hyllrad"} ({linearShelfLoad ? "kg/m" : "kg"})
            <input type="number" min="0" max="500" step="0.1"
              value={inputDrafts[`carcass.${loadKey}`] ?? ((intent[loadKey] ?? 0)/9.80665).toFixed(1)}
              onChange={e => changeLoad(e.target.value)} /></label>
          {intent.family === "shelving" ? <p>Ange jämnt fördelad nyttig last. Radens last fördelas mellan facken efter deras bredd. Hyllornas egenvikt räknas till separat.
            {preview?.workshop_handoff?.shelf_load ? ` Beräknad last per hel rad: ${(preview.workshop_handoff.shelf_load.total_row_load_n/9.80665).toFixed(1)} kg.` : ""}
            {linearShelfLoad ? " Lasten räknas om när stommens bredd ändras." : " Angiven vikt gäller hela raden, inte varje fack."}</p> : null}
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
        {catalog ? <ProfileEditor key={`${fingerprint(workspace)}-${inputEpoch}`}
          api={api} workspace={workspace} catalog={catalog} disabled={busy || !mayEdit}
          initialDraft={restoredProfile} onDraftChange={profileDraftChanged} onApply={update} onDirtyChange={profileDirtyChanged} /> : <p>Läser profiler…</p>}
      </aside>
    </div>
    <FurnitureTrialReadinessPanel report={preview?.trial_readiness} designHash={preview?.design.design_hash} dirty={dirty} />
    <section className={styles.review}>
      <div><h2>Kontroll och underlag</h2>
        <p>Designunderlag för granskning. Skärande CAM kräver verifierade beslag, konstruktion och maskin.</p>
        {preview ? <p>{(preview.design.total_weight_g/1_000).toFixed(1)} kg beräknad bruttovikt · {dirty ? "Osparade ändringar" : revision ? "Sparad revision" : "Nytt utkast"}</p> : null}
      </div>
      {intent.family === "shelving" ? <div>
        <button className={styles.primary} disabled={busy || dirty || !projectId || !preview || !workspace.manufacturing
          || preview.workshop_handoff?.dimensions.state === "requires_resolution"}
          onClick={() => setProductionOpen(true)}>Förbered tillverkning</button>
        <p>{profileDirty ? "Använd eller återställ profilförslaget först." : dirty || !revision ? "Spara möbeln först." : !workspace.manufacturing
          ? "Välj och spara en planeringsprofil för att börja bereda. Verklig verkstad kan väljas senare."
          : preview?.workshop_handoff?.dimensions.state === "requires_resolution"
            ? "Kundmått och listkonstruktion behöver lösas innan tillverkningsberedningen öppnas."
            : "Öppna råmaterial, nesting, foggranskning och maskinens CAM-flöde för den sparade möbeln."}</p>
      </div> : null}
      <button className={styles.primary} disabled={!mayEdit || busy || dirty || !projectId || !preview}
        onClick={() => { void exportReview(); }}>Skapa granskningspaket</button>
      {exportMessage ? <p role="status">{exportMessage}</p> : null}
      {download && !profileDirty ? <a href={download.url} download={download.name}>Hämta STEP, GLB, DXF, delritningar och kaplista · revision {download.revision}</a> : null}
      {intent.family === "shelving" && preview ? <FurnitureShelfSuggestionPanel
        key={`${fingerprint(workspace)}-${inputEpoch}`} api={api} workspace={workspace}
        designHash={preview.design.design_hash} disabled={busy || invalidInput || profileDirty || !mayEdit}
        onApply={next => { update(next); setNotice("Fackindelningen är ändrad. Granska möbeln och spara en ny revision."); }} /> : null}
      <div className={styles.rules}>{preview?.rules.evaluations.map(rule => <details key={rule.rule_id}>
        <summary><span className={rule.status === "PASS" ? styles.pass : rule.status === "BLOCK" ? styles.blocked : styles.requiresReview}>
          {rule.status === "PASS" ? "Kontrollerat" : rule.status === "BLOCK" ? "Blockerar tillverkning" : "Behöver granskas"}</span>{rule.title}</summary>
        <FurnitureRuleValues rule={rule} />
        <p>{rule.detail}</p>
      </details>)}</div>
      {preview?.manufacturing.issues.map((issue, index) => <p className={styles.error} key={`${issue.code}-${index}`}>{issue.message}</p>)}
      {preview ? <><FurnitureStockPlan manufacturing={preview.manufacturing} />
        <FurnitureStockRequirements preview={preview} /></> : null}
    </section>
    {history.items.length ? <section className={styles.history}>
      <h2>Sparade revisioner</h2><p>Öppna en tidigare design och spara fortsatta ändringar som en ny revision.</p>
      {history.items.map(item => <button key={item.id} disabled={busy || !mayEdit} onClick={() => navigate(() => {
        resetInputDrafts(); update(item.workspace); setNotice(`Revision ${item.revision} öppnad. Nästa sparning skapar en ny revision.`);
      })}>Revision {item.revision} · {new Date(item.created_at).toLocaleString("sv-SE")}</button>)}
      {history.next_offset !== null && projectId ? <button onClick={() => {
        void api.furnitureHistory(projectId, history.next_offset ?? 0).then(next => setHistory(previous => ({
          items: [...previous.items, ...next.items], next_offset: next.next_offset,
        }))).catch(reason => setError(errorText(reason)));
      }}>Visa äldre revisioner</button> : null}
    </section> : null}
    </fieldset>
  </>;
}

function ProfileEditor({ api, workspace, catalog, disabled, onApply, onDirtyChange, initialDraft, onDraftChange }: {
  api: CustombuildApiClient; workspace: FurnitureWorkspace; catalog: FurnitureCatalog;
  disabled: boolean; onApply: (next: FurnitureWorkspace) => void; onDirtyChange: (dirty: boolean) => void;
  initialDraft?: FurnitureProfileDraft; onDraftChange: (draft: FurnitureProfileDraft | undefined) => void;
}) {
  const [proposed, setProposed] = useState(initialDraft?.proposed ?? workspace);
  const [comparison, setComparison] = useState<FurnitureProfileComparison>();
  const [error, setError] = useState<string>();
  const [inputErrors, setInputErrors] = useState<Record<string, string>>(initialDraft?.inputErrors ?? {});
  const [inputDrafts, setInputDrafts] = useState<Record<string, string>>(initialDraft?.inputDrafts ?? {});
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);
  const invalidInput = Object.keys(inputErrors).length > 0;
  const changed = fingerprint(workspace) !== fingerprint(proposed);
  const dirty = changed || invalidInput;
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onDraftChange(dirty ? { proposed, inputDrafts, inputErrors } : undefined); },
    [dirty, proposed, inputDrafts, inputErrors, onDraftChange]);
  const change = (next: FurnitureWorkspace, field?: string) => {
    epoch.current += 1; setProposed(next); setComparison(undefined); setError(undefined); setBusy(false);
    if (field) {
      const keepOtherFields = (previous: Record<string, string>) => Object.fromEntries(Object.entries(previous)
        .filter(([key]) => key !== field && !key.startsWith(`${field}.`)));
      setInputErrors(keepOtherFields); setInputDrafts(keepOtherFields);
    }
  };
  const inputError = (field: string, reason: unknown, raw: string) => {
    epoch.current += 1; setBusy(false); setComparison(undefined);
    setInputErrors(previous => ({ ...previous, [field]: errorText(reason) }));
    setInputDrafts(previous => ({ ...previous, [field]: raw }));
  };
  const withMillimetres = (field: string, raw: string, apply: (value: number) => FurnitureWorkspace) => {
    try { change(apply(exactMillimetreTextToMicrometres(raw, { minimumUm: 1, maximumUm: 6_000_000 })), field); }
    catch (reason) { inputError(field, reason, raw); }
  };
  const check = async (event: FormEvent) => {
    event.preventDefault();
    if (invalidInput || !changed || disabled || busy) return;
    const currentEpoch = ++epoch.current;
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
              measured_thickness_um: material.nominal_thickness_um, batch_id: null } } }, key);
          }}>{catalog.materials.filter(m => m.nominal_thickness_um === (key === "material" ? 18_000 : 6_000))
            .map(m => <option key={m.material_id} value={m.material_id}>{m.name}</option>)}</select></label>
        <label>{key === "material" ? "Uppmätt skivtjocklek (mm)" : "Uppmätt rygg-/bottentjocklek (mm)"}
          <input type="number" min="1" step="0.001" value={inputDrafts[`${key}.thickness`] ?? proposed.design[key].measured_thickness_um/1_000}
            onChange={e => withMillimetres(`${key}.thickness`, e.target.value, value => ({ ...proposed, design: { ...proposed.design, [key]: { ...proposed.design[key],
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
          change({ ...proposed, manufacturing: machine ? { ...(proposed.manufacturing ?? {
            stock_width_um: 1_220_000, stock_height_um: 2_440_000, stock_grain_axis: null }),
            machine_profile_id: machine.profile_id, machine_profile_version: machine.version } : null },
          machine && proposed.manufacturing ? undefined : "manufacturing");
        }}><option value="">Välj verkstad senare</option>{catalog.machines.map(m =>
          <option key={m.profile_id} value={m.profile_id}>{m.name} · referensprofil</option>)}</select></label>
      <FurnitureStockEditor workspace={proposed} inputDrafts={inputDrafts}
        onChange={(manufacturing, field) => change({ ...proposed, manufacturing }, field)}
        onInputError={inputError} />
      <button type="submit" disabled={busy || invalidInput || !changed}>
        {busy ? "Kontrollerar…" : "Kontrollera profilbyte"}</button>
    </fieldset>
    {dirty ? <button type="button" disabled={disabled} onClick={() => {
      change(workspace); setInputErrors({}); setInputDrafts({});
    }}>Återställ profilförslag</button> : null}
    {Object.entries(inputErrors).map(([field, message]) => <p key={field} role="alert" className={styles.error}>{message}</p>)}
    {error ? <p role="alert" className={styles.error}>{error}</p> : null}
    {comparison ? <div className={styles.proposal} aria-live="polite">
      <strong>{comparison.can_apply ? "Konsekvenser av profilbytet" : "Profilen fungerar inte med designen"}</strong>
      <p>{comparison.message}</p>
      {comparison.can_apply ? <><p>{comparison.changed_part_ids?.length ?? 0} delar räknas om. Yttermåtten behålls.</p>
        <p>{comparison.invalidated_reviews.includes("construction") ? "Konstruktion och tillverkning behöver granskas på nytt."
          : comparison.invalidated_reviews.length ? "Tillverkningen behöver beredas och granskas på nytt." : "Inga tillverkningsberoenden ändras."}</p>
        {comparison.proposed?.manufacturing.issues.map((issue, i) => <p key={i}>{issue.message}</p>)}
        {comparison.proposed ? <FurnitureStockPlan manufacturing={comparison.proposed.manufacturing} /> : null}
        <button type="button" className={styles.primary} disabled={disabled}
          onClick={() => {
            if (comparison.proposed) {
              change(comparison.proposed.workspace); setInputErrors({}); setInputDrafts({});
              onApply(comparison.proposed.workspace);
            }
          }}>Använd profilbytet i designen</button>
      </> : null}
    </div> : null}
  </form>;
}
