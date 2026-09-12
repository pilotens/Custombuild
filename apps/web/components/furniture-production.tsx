"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  CustombuildApiClient, designSpecFromServer, normalizePreviewResponse,
  type CurrentPrincipal,
} from "@/lib/api-client";
import { DEFAULT_DESIGN_SPEC, type DesignSpec, type ResolvedDesign } from "@/lib/design-types";
import type { FurnitureProductionSource, FurnitureWorkspace } from "@/lib/furniture-workspace";
import { furnitureProductionRecovery, furnitureProductionRecoveryKey,
  restoreFurnitureProductionState, serializeFurnitureProductionRecovery } from "@/lib/furniture-production-recovery";
import { replaceFurnitureRecovery } from "@/lib/furniture-draft-recovery";
import { parseRevisionProductionContext, productionContextFromDesignSpec } from "@/lib/workshop-production-context";
import { ProductionWorkflow, type ProductionSummary } from "./production-workflow";
import { restoreWorkshopContextDraftState, type WorkshopContextDraftState } from "./workshop-context-editor";
import styles from "./furniture-workspace.module.css";

interface Props {
  api: CustombuildApiClient;
  principal: CurrentPrincipal;
  workspace: FurnitureWorkspace;
  designHash: string;
  projectName: string;
  onClose: () => void;
}
interface Preparation {
  spec: DesignSpec;
  design: ResolvedDesign;
  source: FurnitureProductionSource;
  preview: Record<string, unknown>;
}
const contextKey = (spec: DesignSpec) => JSON.stringify(productionContextFromDesignSpec(spec));

export function FurnitureProduction(props: Props) {
  // A different account, API, or furniture source must never inherit in-memory edits.
  return <FurnitureProductionSession key={JSON.stringify([props.api.baseUrl,
    props.principal.organization_id, props.principal.user_id, props.principal.role,
    props.designHash, props.workspace])} {...props} />;
}

function FurnitureProductionSession({ api, principal, workspace, designHash, projectName, onClose }: Props) {
  const projectId = workspace.design.design_id;
  const [data, setData] = useState<Preparation>();
  const [error, setError] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  const [baseline, setBaseline] = useState<string>();
  const [formState, setFormState] = useState<WorkshopContextDraftState>();
  const [confirmClose, setConfirmClose] = useState(false);
  const [pendingRecovery, setPendingRecovery] = useState<{ raw: string; persisted: boolean }>();
  const [storageError, setStorageError] = useState<string>();
  const [recoveryNotice, setRecoveryNotice] = useState<string>();
  const [sourceReady, setSourceReady] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const recoveryChecked = useRef(false);
  const lastSeenRecovery = useRef<string | null>(null);
  const recoverySession = useRef<AbortController | null>(null);
  const boundSourceHash = useRef<string | undefined>(undefined);
  const pendingNavigation = useRef<(() => void) | undefined>(undefined);
  const navigationApproved = useRef(false);
  const activeSpec = useRef<DesignSpec | undefined>(undefined);
  const retry = useCallback(() => setRefresh(value => value + 1), []);
  const formDirty = Boolean(formState && (formState.dirty || !formState.valid));
  const dirty = formDirty || Boolean(data && contextKey(data.spec) !== baseline);
  const recoveryKey = data ? furnitureProductionRecoveryKey(api.baseUrl, principal, data.source) : undefined;
  // Bind the new source session before its recovery buttons can be activated.
  // A passive effect can abort the old session after an early click starts.
  useLayoutEffect(() => {
    const controller = new AbortController();
    recoverySession.current = controller;
    return () => controller.abort();
  }, [recoveryKey]);
  const initialSpec = useMemo((): DesignSpec => {
    const planning = workspace.manufacturing;
    if (!planning) throw new Error("Välj en planeringsprofil före beredningen.");
    return {
      ...DEFAULT_DESIGN_SPEC,
      design_id: projectId,
      machine_profile_id: planning.machine_profile_id,
      stock_width_mm: planning.stock_width_um / 1_000,
      stock_height_mm: planning.stock_height_um / 1_000,
      back_stock_width_mm: planning.stock_width_um / 1_000,
      back_stock_height_mm: planning.stock_height_um / 1_000,
      // No stock profile is implied. The workshop binds actual sheets and quantities.
      stock_count: 1, back_stock_count: 1, workshop_context: undefined,
      reinforcement_mode: "manual", part_overrides: {}, removed_part_ids: [],
      reference_image_import: undefined,
    };
  }, [workspace, projectId]);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        setSourceReady(false);
        const result = await api.previewFurnitureProduction(
          projectId, workspace.design.revision, designHash, controller.signal,
        );
        if (boundSourceHash.current && boundSourceHash.current !== result.source_furniture.workspace_sha256) {
          throw new Error("Den sparade möbelkällan har ändrats. Ladda ned beredningsutkastet och öppna möbeln igen.");
        }
        let requested = activeSpec.current ?? initialSpec;
        if (!activeSpec.current) {
          const state = await api.getProductionState(projectId);
          const source = state.version?.result_json.source_furniture as FurnitureProductionSource | undefined;
          if (source?.workspace_sha256 === result.source_furniture.workspace_sha256) {
            const { stock_profiles, two_sided_registrations, ...legacy } = parseRevisionProductionContext(
              state.version!.result_json.production_context,
            );
            requested = { ...requested, ...legacy,
              workshop_context: stock_profiles ? { stock_profiles, two_sided_registrations } : undefined };
          }
        }
        const spec = designSpecFromServer(result.preview.spec, requested);
        const design = normalizePreviewResponse(result.preview, spec);
        if (controller.signal.aborted) return;
        boundSourceHash.current = result.source_furniture.workspace_sha256;
        if (!activeSpec.current) setBaseline(contextKey(spec));
        activeSpec.current = spec;
        setData({ spec, design, source: result.source_furniture, preview: result.preview });
        if (!recoveryChecked.current) {
          try {
            const raw = window.localStorage.getItem(furnitureProductionRecoveryKey(api.baseUrl, principal, result.source_furniture));
            lastSeenRecovery.current = raw;
            recoveryChecked.current = true;
            if (raw) setPendingRecovery({ raw, persisted: true });
          } catch {
            setStorageError("Webbläsaren kan inte läsa en lokal beredningskopia. Ladda ned utkastet innan du lämnar sidan.");
          }
        }
        setSourceReady(true);
        setError(undefined);
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Beredningen kunde inte öppnas.");
      }
    })();
    return () => controller.abort();
  }, [api, principal, projectId, workspace.design.revision, designHash, initialSpec, refresh]);

  useEffect(() => {
    if (!data || !recoveryKey || pendingRecovery || !recoveryChecked.current) return;
    let active = true;
    const controller = new AbortController();
    void Promise.resolve().then(async () => {
      if (!active) return;
      try {
        const next = dirty ? serializeFurnitureProductionRecovery(furnitureProductionRecovery(data.spec, data.source, formState)) : null;
        const written = await replaceFurnitureRecovery(window.localStorage, recoveryKey, lastSeenRecovery, next,
          () => active, controller.signal);
        if (active && written) setStorageError(undefined);
      } catch (reason) {
        if (!active) return;
        setStorageError(reason instanceof Error && /^(En annan flik|Formulärkopian|Beredningskopian|Webbläsaren)/.test(reason.message)
          ? reason.message : "Den lokala beredningskopian kunde inte sparas. Ladda ned utkastet innan du lämnar sidan.");
      }
    });
    return () => { active = false; controller.abort(); };
  }, [data, recoveryKey, pendingRecovery, dirty, formState]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      if (!navigationApproved.current) { event.preventDefault(); event.returnValue = ""; }
    };
    const followLink = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!(anchor instanceof HTMLAnchorElement) || anchor.hasAttribute("download")
        || (anchor.target && anchor.target !== "_self")) return;
      const destination = new URL(anchor.href, window.location.href);
      if (destination.origin !== window.location.origin
        || `${destination.pathname}${destination.search}` === `${window.location.pathname}${window.location.search}`) return;
      event.preventDefault();
      event.stopPropagation();
      pendingNavigation.current = () => {
        navigationApproved.current = true;
        window.location.assign(destination.href);
      };
      setConfirmClose(true);
    };
    window.addEventListener("beforeunload", warn);
    document.addEventListener("click", followLink, true);
    return () => {
      window.removeEventListener("beforeunload", warn);
      document.removeEventListener("click", followLink, true);
    };
  }, [dirty]);

  const summaryChanged = useCallback((summary: ProductionSummary) => {
    if (summary.revision && !summary.stale && summary.status !== "syncing" && activeSpec.current) {
      setBaseline(contextKey(activeSpec.current));
    }
  }, []);
  const formChanged = useCallback((state: WorkshopContextDraftState) => setFormState(state), []);
  const applyContext = (patch: Partial<DesignSpec>) => {
    if (!data) return;
    try {
      const spec = { ...data.spec, ...patch };
      const design = normalizePreviewResponse(data.preview, spec);
      activeSpec.current = spec;
      setData({ ...data, spec, design });
      setError(undefined);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Kontrollera verkstadsuppgifterna."); }
  };

  const downloadRecovery = (raw?: string) => {
    if (!data && !raw) return;
    let content: string;
    try {
      content = raw ?? serializeFurnitureProductionRecovery(furnitureProductionRecovery(data!.spec, data!.source, formState));
    } catch (reason) {
      setStorageError(reason instanceof Error ? reason.message : "Beredningskopian kunde inte skapas.");
      return;
    }
    const url = URL.createObjectURL(new Blob([content], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = `${projectId}-beredningsutkast.json`;
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
  };
  const restoreRecovery = async () => {
    if (!data || !pendingRecovery || restoring) return;
    setRestoring(true);
    setError(undefined);
    try {
      // Recheck access and the exact source even if the dialog has been open for a while.
      const fresh = await api.previewFurnitureProduction(projectId, workspace.design.revision, designHash);
      if (fresh.source_furniture.workspace_sha256 !== data.source.workspace_sha256) {
        throw new Error("Möbelkällan har ändrats. Kopian kan inte användas för denna beredning.");
      }
      const serverSpec = designSpecFromServer(fresh.preview.spec, data.spec);
      const { spec, editorDraft } = restoreFurnitureProductionState(pendingRecovery.raw, fresh.source_furniture, serverSpec);
      const design = normalizePreviewResponse(fresh.preview, spec);
      setFormState(editorDraft ? restoreWorkshopContextDraftState(spec, editorDraft) : undefined);
      activeSpec.current = spec;
      setData({ spec, design, source: fresh.source_furniture, preview: fresh.preview });
      setPendingRecovery(undefined);
      setSourceReady(true);
      setError(undefined);
      setRecoveryNotice("Beredningsutkastet är återställt. Spara och kontrollera en tillverkningsrevision. Kopian innehåller inga godkännanden.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Beredningskopian kunde inte återställas.");
    } finally { setRestoring(false); }
  };
  const discardRecovery = async () => {
    const session = recoverySession.current;
    if (restoring || !session || session.signal.aborted) return;
    setRestoring(true);
    try {
      if (pendingRecovery?.persisted && recoveryKey) {
        const removed = await replaceFurnitureRecovery(window.localStorage, recoveryKey, lastSeenRecovery, null,
          () => !session.signal.aborted, session.signal);
        if (!removed) return;
      }
      if (!session.signal.aborted) { setPendingRecovery(undefined); setError(undefined); }
    } catch (reason) {
      if (!session.signal.aborted) setStorageError(reason instanceof Error && /^(En annan flik|Webbläsaren)/.test(reason.message)
        ? reason.message : "Kopian kunde inte tas bort. Ladda ned den och försök igen.");
    } finally {
      if (!session.signal.aborted) setRestoring(false);
    }
  };

  return <section className={styles.preparation} aria-label="Beredning av sparat hyllsystem">
    <div className={styles.titleRow}>
      <div><p className={styles.eyebrow}>{projectName} · möbelrevision {workspace.design.revision}</p>
        <h1>Förbered tillverkningen</h1>
        <p>Bind råmaterial och fogar, skapa beredningsunderlag och ta fram CAM för den valda maskinen.</p>
        <p>Planeringsprofilen beskriver ännu ingen verifierad verkstad eller tillgänglig materialbatch.</p>
      </div>
      <button onClick={() => {
        pendingNavigation.current = undefined;
        if (dirty) setConfirmClose(true); else onClose();
      }}>Tillbaka till möbeln</button>
    </div>
    {confirmClose ? <section role="alertdialog" aria-label="Osparad beredning" className={styles.notice}>
      <p>Beredningen har osparade ändringar. Spara en tillverkningsrevision för att behålla dem på servern.</p>
      <p>Verkstadsuppgifter och ofullständiga formulärfält kan återställas från den lokala kopian när webbläsaren tillåter det.</p>
      <button onClick={() => setConfirmClose(false)}>Fortsätt bereda</button>
      <button onClick={() => (pendingNavigation.current ?? onClose)()}>Lämna utan att spara beredningen</button>
    </section> : null}
    {error ? <p role="alert" className={styles.error}>{error} <button disabled={restoring} onClick={retry}>Försök igen</button></p> : null}
    {storageError ? <p role="alert" className={styles.error}>{storageError}</p> : null}
    {recoveryNotice ? <p role="status" className={styles.notice}>{recoveryNotice}</p> : null}
    {pendingRecovery ? <section aria-label="Lokal beredningskopia" className={styles.notice}>
      <h2>Återställ osparad beredning</h2>
      <p>Kopian måste matcha exakt denna möbelrevision. Uppgifterna kontrolleras mot servern innan de används.</p>
      <button disabled={restoring} aria-busy={restoring} onClick={() => { void restoreRecovery(); }}>Återställ beredningskopian</button>
      <button onClick={() => downloadRecovery(pendingRecovery.raw)}>Ladda ned beredningskopian</button>
      <button disabled={restoring} onClick={() => { void discardRecovery(); }}>Kasta beredningskopian</button>
    </section> : null}
    {data && !pendingRecovery ? <section className={styles.notice} aria-label="Säkerhetskopia av beredning">
      <p>Verkstadsuppgifter och ofullständiga formulärfält säkerhetskopieras lokalt tills revisionen sparas. Återställda fält måste kontrolleras innan de tillämpas. En kopia är inget tillverkningsgodkännande.</p>
      <button onClick={() => downloadRecovery()}>Ladda ned beredningsutkast</button>
      <label>Läs in beredningsutkast <input type="file" accept=".json,application/json" disabled={dirty || !sourceReady}
        onChange={event => {
          const file = event.target.files?.[0]; event.target.value = "";
          if (!file) return;
          if (file.size > 256 * 1024) { setError("Beredningskopian är för stor."); return; }
          void file.text().then(raw => setPendingRecovery({ raw, persisted: false }))
            .catch(() => setError("Filen kunde inte läsas."));
        }} /></label>
    </section> : null}
    {data && !pendingRecovery ? <fieldset disabled={!sourceReady} aria-label="Verkstadsberedning">
      {!sourceReady && !error ? <p role="status">Kontrollerar den sparade möbeln igen…</p> : null}
      <ProductionWorkflow spec={data.spec} design={data.design}
      apiClient={api} principal={principal} projectId={projectId} projectName={projectName}
      templateId="shelving" sourceFurniture={data.source} showRevisionHistory
      onApplyWorkshopContextChange={applyContext} onRequestServerPreviewRetry={retry}
      onSummaryChange={summaryChanged} workshopContextDraftState={formState} onWorkshopContextDraftStateChange={formChanged} />
      </fieldset>
      : !error && !pendingRecovery ? <p role="status">Kontrollerar den sparade möbeln mot tillverkningsmodellen…</p> : null}
  </section>;
}
