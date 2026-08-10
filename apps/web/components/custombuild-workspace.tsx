"use client";

import dynamic from "next/dynamic";
import {
  Box,
  Check,
  Cloud,
  CloudOff,
  Eye,
  EyeOff,
  Factory,
  FolderKanban,
  Focus,
  GitBranch,
  Layers3,
  LayoutTemplate,
  LoaderCircle,
  PackageCheck,
  Redo2,
  Save,
  Settings2,
  Undo2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { CustombuildApiClient } from "@/lib/api-client";
import { applySuggestion, localDesignHash, resolveDesign } from "@/lib/design-engine";
import {
  DEFAULT_DESIGN_SPEC,
  type ChangeDiff,
  type DesignSpec,
  type RuleEvaluation,
} from "@/lib/design-types";
import {
  resolveSemanticDrop,
  type SemanticComponentKind,
  type SemanticDropRequest,
} from "@/lib/semantic-design";
import type { ViewMode } from "./furniture-viewer";
import { BottomInspector, type InspectorTabId } from "./bottom-inspector";
import { ComponentPalette } from "./component-palette";
import { ParameterPanel } from "./parameter-panel";
import type { ProductionSummary } from "./production-workflow";
import styles from "./semantic-editor.module.css";
import { ValidationPanel } from "./validation-panel";

const FurnitureViewer = dynamic(() => import("./furniture-viewer"), {
  ssr: false,
  loading: () => (
    <div className="viewer-loading" role="status">
      <LoaderCircle aria-hidden="true" className="spin" size={22} />
      Förbereder parametrisk modell…
    </div>
  ),
});

type ApiState = "syncing" | "synced" | "offline" | "error";
type SaveState = "saving" | "saved" | "error";

interface ServerPreviewState {
  requestHash: string;
  result: ReturnType<typeof resolveDesign>;
}

interface SemanticNotice {
  title: string;
  detail?: string;
  error?: boolean;
}

const navItems = [
  { label: "Projekt", href: "#workspace", icon: FolderKanban, active: true },
  { label: "Mallar", href: "#parameters", icon: LayoutTemplate },
  { label: "Material", href: "#material-field", icon: Layers3 },
  { label: "Maskiner", href: "#machine-field", icon: Settings2 },
  { label: "Produktion", href: "#production-tabs", icon: Factory },
];

function isStoredSpec(value: unknown): value is Partial<DesignSpec> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const stored = value as Record<string, unknown>;
  return stored.joint_system === undefined || stored.joint_system === "dado";
}

function ApiIndicator({ state, message }: { state: ApiState; message: string }) {
  const Icon = state === "syncing" ? LoaderCircle : state === "synced" ? Cloud : CloudOff;
  const label = state === "syncing"
    ? "Synkar"
    : state === "synced"
      ? "Servermodell"
      : state === "offline"
        ? "Lokalt läge"
        : "API-fel";
  return (
    <span className={`api-indicator api-${state}`} title={message}>
      <Icon
        aria-hidden="true"
        className={state === "syncing" ? "spin" : ""}
        size={14}
      />
      {label}
    </span>
  );
}

function productionStatusLabel(summary: ProductionSummary): string {
  const labels: Record<string, string> = {
    unsaved: "Ej fryst",
    stale: "Ändrad",
    draft: "Utkast",
    design_validated: "Design validerad",
    cam_validated: "CAM validerad",
    approved: "Godkänd",
    released: "Frisläppt",
    superseded: "Ersatt",
    archived: "Arkiverad",
  };
  return labels[summary.status] ?? summary.status;
}

function defaultDropRequest(kind: SemanticComponentKind): SemanticDropRequest {
  if (kind === "shelf_row") return { kind, normalizedX: 0.5, normalizedY: 0.5 };
  if (kind === "divider") return { kind, normalizedX: 0.5, normalizedY: 0.5 };
  if (kind === "back_panel") return { kind, normalizedX: 0.5, normalizedY: 0.5 };
  return { kind, normalizedX: 0.5, normalizedY: 1 };
}

export function CustombuildWorkspace() {
  const api = useMemo(() => new CustombuildApiClient(), []);
  const [spec, setSpec] = useState<DesignSpec>(DEFAULT_DESIGN_SPEC);
  const [past, setPast] = useState<DesignSpec[]>([]);
  const [future, setFuture] = useState<DesignSpec[]>([]);
  const [changeDiff, setChangeDiff] = useState<ChangeDiff[]>([]);
  const [selectedPartId, setSelectedPartId] = useState<string>();
  const [viewMode, setViewMode] = useState<ViewMode>("perspective");
  const [exploded, setExploded] = useState(false);
  const [transparent, setTransparent] = useState(false);
  const [isolateSelection, setIsolateSelection] = useState(false);
  const [mode, setMode] = useState<"guided" | "expert">("guided");
  const [semanticDragKind, setSemanticDragKind] = useState<SemanticComponentKind>();
  const [semanticNotice, setSemanticNotice] = useState<SemanticNotice>();
  const [apiState, setApiState] = useState<ApiState>(api.configured ? "syncing" : "offline");
  const [apiMessage, setApiMessage] = useState(
    api.configured
      ? "Synkroniserar mot serverns auktoritativa modell."
      : "NEXT_PUBLIC_API_URL saknas. Lokal deterministisk förhandsvisning används.",
  );
  const [saveState, setSaveState] = useState<SaveState>("saved");
  const [serverPreview, setServerPreview] = useState<ServerPreviewState>();
  const [applyingRuleId, setApplyingRuleId] = useState<string>();
  const [hydrated, setHydrated] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTabId>("parts");
  const [productionSummary, setProductionSummary] = useState<ProductionSummary>({
    status: "unsaved",
    stale: false,
  });

  useEffect(() => {
    let cancelled = false;
    let storedSpec: DesignSpec | undefined;
    let storageFailed = false;
    try {
      const stored = window.localStorage.getItem("custombuild:bookcase:demo");
      if (stored) {
        const parsed: unknown = JSON.parse(stored);
        if (isStoredSpec(parsed)) storedSpec = { ...DEFAULT_DESIGN_SPEC, ...parsed };
      }
    } catch {
      storageFailed = true;
    }
    queueMicrotask(() => {
      if (cancelled) return;
      if (storedSpec) setSpec(storedSpec);
      if (storageFailed) setSaveState("error");
      setHydrated(true);
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    const timer = window.setTimeout(() => {
      try {
        window.localStorage.setItem("custombuild:bookcase:demo", JSON.stringify(spec));
        setSaveState("saved");
      } catch {
        setSaveState("error");
      }
    }, 450);
    return () => window.clearTimeout(timer);
  }, [hydrated, spec]);

  const localDesign = useMemo(() => resolveDesign(spec, changeDiff), [changeDiff, spec]);
  const design = serverPreview?.requestHash === localDesign.design_hash
    ? { ...serverPreview.result, change_diff: changeDiff }
    : localDesign;

  useEffect(() => {
    if (!api.configured) return;
    const controller = new AbortController();
    const requestHash = localDesignHash(spec);
    const timer = window.setTimeout(() => {
      api.previewDesign(spec, controller.signal)
        .then((result) => {
          setServerPreview({ requestHash, result });
          setApiState("synced");
          setApiMessage("Serverns auktoritativa preview är synkroniserad.");
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setApiState("error");
          setApiMessage(
            error instanceof Error ? error.message : "Ett okänt API-fel inträffade.",
          );
        });
    }, 500);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [api, spec]);

  const effectiveSelectedPartId = selectedPartId
    && design.parts.some((part) => part.part_id === selectedPartId)
    ? selectedPartId
    : undefined;

  const replaceSpec = useCallback((next: DesignSpec, diff: ChangeDiff[] = []) => {
    if (localDesignHash(next) === localDesignHash(spec)) return;
    setPast((items) => [...items, spec].slice(-50));
    setFuture([]);
    setSpec(next);
    setChangeDiff(diff);
    setSaveState("saving");
    if (api.configured) {
      setApiState("syncing");
      setApiMessage("Synkroniserar samma DesignSpec mot serverns auktoritativa motor.");
    }
  }, [api.configured, spec]);

  const updateSpec = useCallback((patch: Partial<DesignSpec>) => {
    replaceSpec({ ...spec, ...patch }, []);
  }, [replaceSpec, spec]);

  const applySemanticDrop = useCallback((request: SemanticDropRequest) => {
    try {
      const outcome = resolveSemanticDrop(spec, request);
      replaceSpec(outcome.spec, outcome.diff);
      setSemanticNotice({ title: outcome.message, detail: outcome.warning });
    } catch (error: unknown) {
      setSemanticNotice({
        title: error instanceof Error ? error.message : "Byggdelen kunde inte placeras.",
        error: true,
      });
    } finally {
      setSemanticDragKind(undefined);
    }
  }, [replaceSpec, spec]);

  const undo = () => {
    const previous = past.at(-1);
    if (!previous) return;
    setPast((items) => items.slice(0, -1));
    setFuture((items) => [spec, ...items].slice(0, 50));
    setSpec(previous);
    setChangeDiff([]);
    setSaveState("saving");
    if (api.configured) setApiState("syncing");
  };

  const redo = () => {
    const next = future[0];
    if (!next) return;
    setFuture((items) => items.slice(1));
    setPast((items) => [...items, spec].slice(-50));
    setSpec(next);
    setChangeDiff([]);
    setSaveState("saving");
    if (api.configured) setApiState("syncing");
  };

  const applyRuleSuggestion = (evaluation: RuleEvaluation) => {
    const applied = applySuggestion(spec, evaluation);
    if (applied.diff.length === 0) return;
    replaceSpec(applied.spec, applied.diff);
    if (!api.configured) return;
    setApplyingRuleId(evaluation.rule_id);
    api.autofixDesign({ ...spec, reinforcement_mode: "auto" })
      .then((result) => {
        const targetHash = localDesignHash(applied.spec);
        if (localDesignHash(result.spec) === targetHash) {
          setServerPreview({ requestHash: targetHash, result });
          setApiState("synced");
          setApiMessage(
            "Serverns autokorrigering överensstämmer med den lokala deterministiska ändringen.",
          );
        } else {
          setApiState("error");
          setApiMessage(
            "Serverns autokorrigering avviker från klientpreviewn; ändringen kan inte betraktas som verifierad.",
          );
        }
      })
      .catch((error: unknown) => {
        setApiState("error");
        setApiMessage(
          error instanceof Error
            ? error.message
            : "Autokorrigeringen kunde inte verifieras mot servern.",
        );
      })
      .finally(() => setApplyingRuleId(undefined));
  };

  const saveLabel = saveState === "saving"
    ? "Sparar…"
    : saveState === "error"
      ? "Kunde inte spara lokalt"
      : "Sparad";

  return (
    <div className="app-shell">
      <aside className="side-nav" aria-label="Huvudnavigering">
        <a className="brand" href="#workspace" aria-label="Custombuild arbetsyta">
          <span className="brand-mark"><Box aria-hidden="true" size={20} /></span>
          <span className="brand-name">Custom<span>build</span></span>
        </a>
        <nav>
          <p className="nav-label">Arbetsyta</p>
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <a
                key={item.label}
                href={item.href}
                className={item.active ? "active" : ""}
                title={item.label}
              >
                <Icon aria-hidden="true" size={18} />
                <span>{item.label}</span>
                {item.active ? <span className="active-indicator" /> : null}
              </a>
            );
          })}
        </nav>
        <div className="nav-context">
          <p className="nav-label">Verkstad</p>
          <div className="workshop-card">
            <span className="workshop-icon"><Factory aria-hidden="true" size={16} /></span>
            <span><strong>Stockholm Lab</strong><small>Referensmiljö</small></span>
          </div>
        </div>
        <div className="profile-card">
          <span className="avatar">PS</span>
          <span><strong>Philip Sande</strong><small>Owner · Designer</small></span>
        </div>
      </aside>

      <main className="main-shell" id="workspace">
        <header className="top-header">
          <div className="project-identity">
            <div className="breadcrumb"><span>Projekt</span><span>/</span><span>BK-2408</span></div>
            <div className="project-title-row">
              <h1>Arkitektväggen</h1>
              <span className="revision-chip">
                <GitBranch aria-hidden="true" size={13} />
                {productionSummary.revision
                  ? `Rev ${String(productionSummary.revision).padStart(2, "0")}`
                  : "Ej fryst"}
              </span>
              <span className={`draft-chip production-${productionSummary.status}`}>
                {productionStatusLabel(productionSummary)}
              </span>
            </div>
          </div>
          <div className="header-actions">
            <div className="save-state" aria-live="polite">
              {saveState === "saved"
                ? <Check aria-hidden="true" size={13} />
                : <Save aria-hidden="true" size={13} />}
              {saveLabel}
            </div>
            <ApiIndicator state={apiState} message={apiMessage} />
            <span className="header-divider" />
            <button
              type="button"
              className="icon-button"
              aria-label="Ångra"
              title="Ångra"
              disabled={past.length === 0}
              onClick={undo}
            ><Undo2 aria-hidden="true" size={17} /></button>
            <button
              type="button"
              className="icon-button"
              aria-label="Gör om"
              title="Gör om"
              disabled={future.length === 0}
              onClick={redo}
            ><Redo2 aria-hidden="true" size={17} /></button>
            <div className="segmented-control mode-toggle" aria-label="Redigeringsläge">
              <button
                type="button"
                className={mode === "guided" ? "active" : ""}
                aria-pressed={mode === "guided"}
                onClick={() => setMode("guided")}
              >Guidad</button>
              <button
                type="button"
                className={mode === "expert" ? "active" : ""}
                aria-pressed={mode === "expert"}
                onClick={() => {
                  setMode("expert");
                  setSemanticDragKind(undefined);
                }}
              >Expert</button>
            </div>
            <button
              type="button"
              className="primary-button"
              onClick={() => setInspectorTab("production")}
            >
              <Factory aria-hidden="true" size={15} /> Produktionsflöde
            </button>
          </div>
        </header>

        <div className="workspace-grid">
          <section
            className={`viewer-panel ${styles.semanticViewerPanel}`}
            aria-label="Konstruktionsvy"
          >
            <div className="viewer-toolbar">
              <div className="view-tabs" aria-label="Vy">
                {([
                  ["perspective", "3D"],
                  ["front", "Front"],
                  ["side", "Sida"],
                  ["top", "Topp"],
                ] as const).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    className={viewMode === value ? "active" : ""}
                    aria-pressed={viewMode === value}
                    onClick={() => setViewMode(value)}
                  >{label}</button>
                ))}
              </div>
              <span className="toolbar-divider" />
              <button
                type="button"
                className={`tool-button ${exploded ? "active" : ""}`}
                aria-pressed={exploded}
                onClick={() => setExploded((value) => !value)}
              ><PackageCheck aria-hidden="true" size={15} /> Exploderad</button>
              <button
                type="button"
                className={`tool-button ${transparent ? "active" : ""}`}
                aria-pressed={transparent}
                onClick={() => setTransparent((value) => !value)}
              >
                {transparent
                  ? <EyeOff aria-hidden="true" size={15} />
                  : <Eye aria-hidden="true" size={15} />} Transparent
              </button>
              <button
                type="button"
                className={`tool-button ${isolateSelection ? "active" : ""}`}
                aria-pressed={isolateSelection}
                disabled={!effectiveSelectedPartId}
                onClick={() => setIsolateSelection((value) => !value)}
              ><Focus aria-hidden="true" size={15} /> Isolera</button>
              <div className="viewer-toolbar-spacer" />
              {effectiveSelectedPartId ? (
                <button
                  type="button"
                  className="selection-chip"
                  onClick={() => {
                    setSelectedPartId(undefined);
                    setIsolateSelection(false);
                  }}
                >
                  <span className="selection-dot" />
                  {design.parts.find((part) => part.part_id === effectiveSelectedPartId)?.name
                    ?? effectiveSelectedPartId}
                  <X aria-hidden="true" size={13} />
                </button>
              ) : <span className="selection-hint">Klicka på en del för att inspektera</span>}
            </div>
            {apiState === "offline" || apiState === "error" ? (
              <div
                className={`offline-banner ${apiState === "error" ? "error" : ""}`}
                role="status"
              >
                <CloudOff aria-hidden="true" size={14} />
                <span>
                  <strong>
                    {apiState === "error" ? "Servern kan inte nås." : "Lokalt konstruktionsläge."}
                  </strong> Produktionsfiler och serverauktoritativ geometri är inte tillgängliga.
                </span>
              </div>
            ) : null}
            {mode === "guided" ? (
              <ComponentPalette
                onInsert={(kind) => applySemanticDrop(defaultDropRequest(kind))}
                onDragStartKind={setSemanticDragKind}
                onDragEnd={() => setSemanticDragKind(undefined)}
              />
            ) : null}
            <FurnitureViewer
              parts={design.parts}
              designSize={{
                widthMm: spec.width_mm,
                heightMm: spec.height_mm,
                depthMm: spec.depth_mm,
              }}
              selectedPartId={effectiveSelectedPartId}
              viewMode={viewMode}
              exploded={exploded}
              transparent={transparent}
              isolateSelection={isolateSelection}
              semanticDropEnabled={mode === "guided"}
              semanticSpec={spec}
              semanticDragKind={semanticDragKind}
              onSelectPart={setSelectedPartId}
              onSemanticDrop={applySemanticDrop}
            />
            <div className="viewer-status-strip">
              <span>
                <span className="engine-dot" />
                {design.source === "server-preview"
                  ? "Servervaliderad konstruktionspreview"
                  : "Deterministisk klientpreview"}
              </span>
              <span>Högerhänt · X bredd · Y djup · Z höjd</span>
              <span>{design.parts.length} delar · BOM/nesting/CAM är klientpreview</span>
            </div>
            {semanticNotice ? (
              <div
                className={styles.semanticNotice}
                role={semanticNotice.error ? "alert" : "status"}
              >
                <strong>{semanticNotice.title}</strong>
                {semanticNotice.detail ? <span>{semanticNotice.detail}</span> : null}
                <button
                  type="button"
                  aria-label="Stäng placeringsmeddelande"
                  onClick={() => setSemanticNotice(undefined)}
                ><X aria-hidden="true" size={14} /></button>
              </div>
            ) : null}
            {changeDiff.length > 0 ? (
              <div className="change-diff" role="status">
                <div className="diff-icon"><GitBranch aria-hidden="true" size={16} /></div>
                <div>
                  <strong>
                    {changeDiff.length} {changeDiff.length === 1 ? "ändring" : "ändringar"} i utkastet
                  </strong>
                  <p>
                    {changeDiff
                      .map((diff) => (
                        `${String(diff.field)}: ${String(diff.before)} → ${String(diff.after)}`
                      ))
                      .join(" · ")}
                  </p>
                </div>
                <button
                  type="button"
                  aria-label="Stäng ändringsöversikt"
                  onClick={() => setChangeDiff([])}
                ><X aria-hidden="true" size={15} /></button>
              </div>
            ) : null}
          </section>

          <aside className="right-rail">
            <ParameterPanel spec={spec} mode={mode} onChange={updateSpec} />
            <ValidationPanel
              evaluations={design.rule_evaluations}
              status={design.status}
              applyingRuleId={applyingRuleId}
              onApplySuggestion={applyRuleSuggestion}
              onSelectPart={setSelectedPartId}
            />
          </aside>

          <BottomInspector
            design={design}
            spec={spec}
            selectedPartId={effectiveSelectedPartId}
            onSelectPart={setSelectedPartId}
            onProductionSummaryChange={setProductionSummary}
            activeTab={inspectorTab}
            onActiveTabChange={setInspectorTab}
          />
        </div>
      </main>
    </div>
  );
}
