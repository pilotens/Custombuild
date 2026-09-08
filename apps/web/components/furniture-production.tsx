"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CustombuildApiClient, designSpecFromServer, normalizePreviewResponse,
  type CurrentPrincipal,
} from "@/lib/api-client";
import { DEFAULT_DESIGN_SPEC, type DesignSpec, type ResolvedDesign } from "@/lib/design-types";
import type { FurnitureProductionSource, FurnitureWorkspace } from "@/lib/furniture-workspace";
import { parseRevisionProductionContext, productionContextFromDesignSpec } from "@/lib/workshop-production-context";
import { ProductionWorkflow, type ProductionSummary } from "./production-workflow";
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

export function FurnitureProduction({ api, principal, workspace, designHash, projectName, onClose }: Props) {
  const projectId = workspace.design.design_id;
  const [data, setData] = useState<Preparation>();
  const [error, setError] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  const [baseline, setBaseline] = useState<string>();
  const [formDirty, setFormDirty] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const activeSpec = useRef<DesignSpec | undefined>(undefined);
  const retry = useCallback(() => setRefresh(value => value + 1), []);
  const dirty = formDirty || Boolean(data && contextKey(data.spec) !== baseline);
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
        const result = await api.previewFurnitureProduction(
          projectId, workspace.design.revision, designHash, controller.signal,
        );
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
        if (!activeSpec.current) setBaseline(contextKey(spec));
        activeSpec.current = spec;
        setData({ spec, design, source: result.source_furniture, preview: result.preview });
        setError(undefined);
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Beredningen kunde inte öppnas.");
      }
    })();
    return () => controller.abort();
  }, [api, projectId, workspace.design.revision, designHash, initialSpec, refresh]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const summaryChanged = useCallback((summary: ProductionSummary) => {
    if (summary.revision && !summary.stale && activeSpec.current) {
      setBaseline(contextKey(activeSpec.current));
    }
  }, []);
  const formChanged = useCallback((state: { dirty: boolean }) => setFormDirty(state.dirty), []);
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

  return <section className={styles.preparation} aria-label="Beredning av sparat hyllsystem">
    <div className={styles.titleRow}>
      <div><p className={styles.eyebrow}>{projectName} · möbelrevision {workspace.design.revision}</p>
        <h1>Förbered tillverkningen</h1>
        <p>Bind råmaterial och fogar, skapa beredningsunderlag och ta fram CAM för den valda maskinen.</p>
        <p>Planeringsprofilen beskriver ännu ingen verifierad verkstad eller tillgänglig materialbatch.</p>
      </div>
      <button onClick={() => dirty ? setConfirmClose(true) : onClose()}>Tillbaka till möbeln</button>
    </div>
    {confirmClose ? <section role="alertdialog" aria-label="Osparad beredning" className={styles.notice}>
      <p>Beredningen har osparade ändringar. Spara en tillverkningsrevision för att behålla dem.</p>
      <button onClick={() => setConfirmClose(false)}>Fortsätt bereda</button>
      <button onClick={onClose}>Lämna utan att spara beredningen</button>
    </section> : null}
    {error ? <p role="alert" className={styles.error}>{error} <button onClick={retry}>Försök igen</button></p> : null}
    {data ? <ProductionWorkflow spec={data.spec} design={data.design}
      apiClient={api} principal={principal} projectId={projectId} projectName={projectName}
      templateId="shelving" sourceFurniture={data.source} showRevisionHistory
      onApplyWorkshopContextChange={applyContext} onRequestServerPreviewRetry={retry}
      onSummaryChange={summaryChanged} onWorkshopContextDraftStateChange={formChanged} />
      : !error ? <p role="status">Kontrollerar den sparade möbeln mot tillverkningsmodellen…</p> : null}
  </section>;
}
