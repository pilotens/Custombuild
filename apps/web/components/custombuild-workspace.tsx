"use client";

import dynamic from "next/dynamic";
import {
  AlertTriangle,
  Box,
  Check,
  Cloud,
  CloudOff,
  Eye,
  EyeOff,
  Focus,
  GitBranch,
  LoaderCircle,
  LogIn,
  LogOut,
  Maximize2,
  PackageCheck,
  Plus,
  Redo2,
  Save,
  Undo2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  CustombuildApiClient,
  type CurrentPrincipal,
  type ProjectRead,
} from "@/lib/api-client";
import {
  beginOidcLogin,
  clearOidcSession,
  completeOidcCallback,
  oidcConfigured,
} from "@/lib/auth-client";
import {
  adaptStructuralSupports,
  balanceDesignSymmetry,
  editPartParametrically,
  localDesignHash,
  mergeServerDesignWithLocalDfm,
  migrateLegacyStructuralRemovals,
  movePartVertically,
  removePartFromDesign,
  resolveDesign,
  restorePartCustomizations,
  setBayWidthRatio,
  setShelfHeightRatio,
  setShelfOpeningHeight,
  shelfOpeningHeights,
} from "@/lib/design-engine";
import {
  DEFAULT_DESIGN_SPEC,
  type ChangeDiff,
  type DesignSpec,
  type PartOverride,
  type ResolvedDesign,
  type ResolvedPart,
  type RuleEvaluation,
} from "@/lib/design-types";
import {
  FURNITURE_TEMPLATES,
  furnitureTemplate,
  hasCustomInteriorLayout,
  hasPartCustomization,
  isReferenceImageDesign,
  type FurnitureTemplate,
  type FurnitureTemplateId,
} from "@/lib/furniture-templates";
import {
  DEFAULT_PLANNING_BRIEF,
  type FurniturePlanningBrief,
} from "@/lib/furniture-planning";
import type { FurnitureComparisonPreview, ViewMode } from "./furniture-viewer";
import { CanvasStateBanners } from "./canvas-state-banners";
import { ComponentPalette } from "./component-palette";
import { DraftConflictBanner } from "./draft-conflict-banner";
import { TemplatePicker } from "./template-picker";
import { ProductionDrawer } from "./production-drawer";
import { ReferenceImageImporter } from "./reference-image-importer";
import { SelectedPartInspector } from "./selected-part-inspector";
import { StudioInspector } from "./studio-inspector";
import { ValidationPanel, type ActiveValidationFixPreview } from "./validation-panel";
import {
  WorkspaceNavigation,
  type WorkspaceStage,
} from "./workspace-navigation";
import type { ReferenceImageResult } from "@/lib/reference-image";
import {
  defaultSemanticDropRequest,
  resolveSemanticDrop,
  type SemanticComponentKind,
  type SemanticDropRequest,
} from "@/lib/semantic-design";
import {
  ANONYMOUS_PROJECT_ID,
  readSelectedProject,
  readWorkspaceDraft,
  writeSelectedProject,
  writeWorkspaceDraft,
} from "@/lib/workspace-draft-storage";
import {
  DesignHydrationError,
  parseLocalDesignPatch,
  parseLocalDesignSpec,
  parseServerProjectDraft,
  workspaceIntentEnvelopeFromSpec,
} from "@/lib/workspace-design-envelope";
import {
  normalizeWorkspaceDesignTransaction,
  previewWorkspaceDesignTransaction,
  type WorkspaceDesignFieldChange,
} from "@/lib/workspace-design-transaction";
import { DEFAULT_WORKSPACE_UI_STATE, type WorkspaceUiState } from "@/lib/workspace-ui-state";
import {
  parseWorkspaceUrl,
  selectWorkspaceMode,
  selectWorkspaceProject,
  serializeWorkspaceUrl,
  type ParsedWorkspaceUrl,
} from "@/lib/workspace-url-state";
import { automaticValidationFix } from "@/lib/validation-guidance";
import { clearLegacyProductionStorage, clearProductionSession } from "@/lib/production-session-storage";
import styles from "./semantic-editor.module.css";
import studioStyles from "./studio-shell.module.css";

const FurnitureViewer = dynamic(() => import("./furniture-viewer"), {
  ssr: false,
  loading: () => (
    <div className="viewer-loading" role="status">
      <LoaderCircle aria-hidden="true" className="spin" size={22} />
      Förbereder parametrisk modell…
    </div>
  ),
});

type ApiState = "syncing" | "synced" | "offline" | "concept" | "error";
const SERVER_PREVIEW_RETRY_BASE_MS = 750;
const SERVER_PREVIEW_RETRY_MAX_MS = 15_000;
type SaveState = "saving" | "saved" | "error";
type StudioInspectorContext = "furniture" | "part";

interface ServerPreviewState {
  requestHash: string;
  result: ReturnType<typeof resolveDesign>;
}

interface SemanticNotice {
  title: string;
  detail: string;
  error?: boolean;
}

interface DraftConflictState {
  projectId: string;
  message: string;
  localDraftJson: string;
}

interface HydrationBlocker {
  projectId: string;
  code: "INVALID_SERVER_DRAFT";
}

interface ValidationFixPreviewState extends ActiveValidationFixPreview {
  token: string;
  sourceSpec: DesignSpec;
  sourceSpecSignature: string;
  sourceProjectId: string | null;
  sourceParts: readonly ResolvedPart[];
  sourcePartsSignature: string;
  evaluationSignature: string;
  proposedSpec: DesignSpec;
  proposedDesign: ResolvedDesign;
  requestedDiff: ChangeDiff[];
  structuralDiff: ChangeDiff[];
  changeDiff: ChangeDiff[];
  changes: WorkspaceDesignFieldChange[];
}

type WorkspaceHistoryAction = "push" | "replace" | "none";

interface WorkspaceStageChangeOptions {
  history?: WorkspaceHistoryAction;
  focus?: boolean;
  allowWithoutStartPoint?: boolean;
}

const WORKSPACE_MODE_HEADINGS: Record<WorkspaceStage, string> = {
  explore: "Utforska och välj startpunkt",
  studio: "Forma din möbel i Studio",
  check: "Kontrollera konstruktionen",
  build: "Designgranska och exportera underlag",
};

const DEFAULT_PROJECT_NAME = "Arkitektväggen";
const LEGACY_STORED_SPEC_KEY = "custombuild:bookcase:demo";
const LEGACY_STORED_TEMPLATE_KEY = "custombuild:furniture-template:demo";
const LEGACY_SUPPORT_AUTOMATION_KEY = "custombuild:bookcase:support-automation:v1";

function canonicalSignatureJson(value: unknown): string {
  if (value === undefined) return "undefined";
  if (Array.isArray(value)) return `[${value.map(canonicalSignatureJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([key, child]) => `${JSON.stringify(key)}:${canonicalSignatureJson(child)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

/** Canonical identity for the complete rule evaluation that authorized a preview. */
export function validationEvaluationSignature(evaluation: RuleEvaluation): string {
  return canonicalSignatureJson(evaluation);
}

function workspaceSpecSignature(spec: DesignSpec): string {
  return JSON.stringify(spec);
}

function displayedPartsSignature(parts: readonly ResolvedPart[]): string {
  return JSON.stringify(parts.map((part) => [
    part.part_id,
    part.name,
    part.kind,
    part.orientation,
    part.width_mm,
    part.depth_mm,
    part.thickness_mm,
    part.position_mm.x,
    part.position_mm.y,
    part.position_mm.z,
    part.color,
    part.material_id,
    part.weight_kg,
    part.features,
  ]));
}

type ComparisonDesignSize = { widthMm: number; heightMm: number; depthMm: number };

function comparisonPartWorldGeometry(part: ResolvedPart, designSize: ComparisonDesignSize) {
  const scale = part.orientation === "YZ"
    ? [part.thickness_mm, part.width_mm, part.depth_mm]
    : part.orientation === "XZ"
      ? [part.width_mm, part.depth_mm, part.thickness_mm]
      : [part.width_mm, part.thickness_mm, part.depth_mm];
  return {
    orientation: part.orientation,
    position: [
      part.position_mm.x - designSize.widthMm / 2,
      part.position_mm.z - designSize.heightMm / 2,
      -(part.position_mm.y - designSize.depthMm / 2),
    ],
    scale,
  };
}

function sameComparisonTuple(left: readonly unknown[], right: readonly unknown[]): boolean {
  return left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
}

export function comparisonGeometryChanged(
  sourceParts: readonly ResolvedPart[],
  sourceDesignSize: ComparisonDesignSize,
  proposedParts: readonly ResolvedPart[],
  proposedDesignSize: ComparisonDesignSize,
): boolean {
  if (sourceParts.length !== proposedParts.length) return true;
  const proposedById = new Map(proposedParts.map((part) => [part.part_id, part] as const));
  return sourceParts.some((source) => {
    const proposed = proposedById.get(source.part_id);
    if (!proposed) return true;
    const sourceGeometry = comparisonPartWorldGeometry(source, sourceDesignSize);
    const proposedGeometry = comparisonPartWorldGeometry(proposed, proposedDesignSize);
    return sourceGeometry.orientation !== proposedGeometry.orientation
      || !sameComparisonTuple(sourceGeometry.position, proposedGeometry.position)
      || !sameComparisonTuple(sourceGeometry.scale, proposedGeometry.scale);
  });
}

function requestedValidationDiff(
  source: DesignSpec,
  patch: Partial<DesignSpec>,
  reason: string,
): ChangeDiff[] {
  return (Object.keys(patch) as Array<keyof DesignSpec>).flatMap((field) => {
    const before = source[field];
    const after = patch[field];
    const supported = (value: unknown): value is string | number | boolean => (
      typeof value === "string" || typeof value === "number" || typeof value === "boolean"
    );
    if (!supported(before) || !supported(after) || Object.is(before, after)) return [];
    return [{ field, before, after, reason }];
  });
}

function isFurnitureTemplateId(value: unknown): value is FurnitureTemplateId {
  return typeof value === "string" && FURNITURE_TEMPLATES.some((template) => template.id === value);
}

function normalizedDefaultSpec(): DesignSpec {
  return balanceDesignSymmetry(adaptStructuralSupports(DEFAULT_DESIGN_SPEC).spec);
}

function normalizedProjectDefaultSpec(projectId: string): DesignSpec {
  return { ...normalizedDefaultSpec(), design_id: projectId };
}

function initialRequestedWorkspaceMode(intent: ParsedWorkspaceUrl): WorkspaceStage | undefined {
  return !intent.projectParamPresent && !intent.modeParamPresent ? "explore" : intent.mode;
}

function normalizeStoredWorkspace(value: unknown): { spec: DesignSpec; diff: ChangeDiff[] } | undefined {
  if (value === undefined) return undefined;
  const bounded = parseLocalDesignSpec(value);
  const migrated = migrateLegacyStructuralRemovals(bounded);
  const adapted = adaptStructuralSupports(migrated.spec);
  const normalized = parseLocalDesignSpec(balanceDesignSymmetry(adapted.spec));
  return {
    spec: normalized,
    diff: [...migrated.diff, ...adapted.diff],
  };
}

function ApiIndicator({ state, message }: { state: ApiState; message: string }) {
  const Icon = state === "syncing" ? LoaderCircle : state === "synced" ? Cloud : CloudOff;
  const label = state === "syncing" ? "Synkar" : state === "synced" ? "Servermodell" : state === "concept" ? "Konceptläge" : state === "offline" ? "Lokalt läge" : "API-fel";
  return (
    <span className={`api-indicator api-${state}`} title={message}>
      <Icon aria-hidden="true" className={state === "syncing" ? "spin" : ""} size={14} />
      {label}
    </span>
  );
}

export function CustombuildWorkspace() {
  const api = useMemo(() => new CustombuildApiClient(), []);
  const [spec, setSpec] = useState<DesignSpec>(normalizedDefaultSpec);
  const [past, setPast] = useState<DesignSpec[]>([]);
  const [future, setFuture] = useState<DesignSpec[]>([]);
  const [changeDiff, setChangeDiff] = useState<ChangeDiff[]>([]);
  const [selectedPartId, setSelectedPartId] = useState<string>();
  const [studioInspectorContext, setStudioInspectorContext] = useState<StudioInspectorContext>("furniture");
  const [partEditNotice, setPartEditNotice] = useState<string>();
  const [principal, setPrincipal] = useState<CurrentPrincipal>();
  const [authReady, setAuthReady] = useState(!api.configured);
  const [authError, setAuthError] = useState<string>();
  const [viewMode, setViewMode] = useState<ViewMode>("perspective");
  const [exploded, setExploded] = useState(false);
  const [transparent, setTransparent] = useState(false);
  const [isolateSelection, setIsolateSelection] = useState(false);
  const [apiState, setApiState] = useState<ApiState>(api.configured ? "syncing" : "offline");
  const [apiMessage, setApiMessage] = useState(
    api.configured
      ? "Synkroniserar mot serverns auktoritativa modell."
      : "NEXT_PUBLIC_API_URL saknas. Lokal deterministisk förhandsvisning används.",
  );
  const [saveState, setSaveState] = useState<SaveState>("saved");
  const [serverPreview, setServerPreview] = useState<ServerPreviewState>();
  const [serverPreviewRetryNonce, setServerPreviewRetryNonce] = useState(0);
  const [hydrated, setHydrated] = useState(false);
  const [projectId, setProjectId] = useState<string>();
  const [projects, setProjects] = useState<Array<Pick<ProjectRead, "id" | "name">>>([]);
  const [activeProject, setActiveProject] = useState<Pick<ProjectRead, "id" | "name">>();
  const [projectCreateOpen, setProjectCreateOpen] = useState(false);
  const [projectCreateName, setProjectCreateName] = useState("");
  const [projectCreateBusy, setProjectCreateBusy] = useState(false);
  const [projectError, setProjectError] = useState<string>();
  const [draftConflict, setDraftConflict] = useState<DraftConflictState>();
  const [draftConflictBusy, setDraftConflictBusy] = useState(false);
  const [draftConflictCopied, setDraftConflictCopied] = useState(false);
  const [hydrationBlocker, setHydrationBlocker] = useState<HydrationBlocker>();
  const [serverDraftReady, setServerDraftReady] = useState(!api.configured);
  const [workspaceSelected, setWorkspaceSelected] = useState(false);
  const [planningBrief, setPlanningBrief] = useState<FurniturePlanningBrief>(DEFAULT_PLANNING_BRIEF);
  const [workspaceStage, setWorkspaceStage] = useState<WorkspaceStage>("explore");
  const [modeHeadingFocusRequest, setModeHeadingFocusRequest] = useState(0);
  const [workspacePanels, setWorkspacePanels] = useState<WorkspaceUiState["panels"]>(
    DEFAULT_WORKSPACE_UI_STATE.panels,
  );
  const [referenceImporterOpen, setReferenceImporterOpen] = useState(false);
  const [selectedTemplateId, setSelectedTemplateId] = useState<FurnitureTemplateId>("shelving");
  const [cameraResetNonce, setCameraResetNonce] = useState(0);
  const [semanticDragKind, setSemanticDragKind] = useState<SemanticComponentKind>();
  const [semanticNotice, setSemanticNotice] = useState<SemanticNotice>();
  const [validationFixPreview, setValidationFixPreview] = useState<ValidationFixPreviewState>();
  const specRef = useRef(spec);
  const resizeSnapshotRef = useRef<DesignSpec | undefined>(undefined);
  const partMoveSnapshotRef = useRef<DesignSpec | undefined>(undefined);
  const partMoveNoticeRef = useRef<string | undefined>(undefined);
  const workspaceLoadRef = useRef(0);
  const draftSaveSequenceRef = useRef(0);
  const serverSaveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const serverDraftRevisionRef = useRef<Map<string, number>>(new Map());
  const draftConflictProjectRef = useRef<string | undefined>(undefined);
  const suppressNextServerDraftSaveRef = useRef(false);
  const modeHeadingRef = useRef<HTMLHeadingElement>(null);
  const workspaceStageRef = useRef<WorkspaceStage>(workspaceStage);
  const validationPreviewSequenceRef = useRef(0);
  const confirmedValidationPreviewTokenRef = useRef<string | undefined>(undefined);
  const serverPreviewRetryAttemptRef = useRef(0);
  const urlIntentRef = useRef<ParsedWorkspaceUrl>(
    typeof window === "undefined"
      ? { projectParamPresent: false, modeParamPresent: false }
      : parseWorkspaceUrl(window.location.search),
  );
  const serverAvailable = api.configured && api.authenticated;

  const retryServerPreview = useCallback(() => {
    serverPreviewRetryAttemptRef.current = 0;
    setServerPreview(undefined);
    setApiState("syncing");
    setApiMessage("Serverpreviewn hämtas om efter en versionskonflikt.");
    setServerPreviewRetryNonce((nonce) => nonce + 1);
  }, []);

  const changeSelectedPart = useCallback((partId?: string) => {
    setSelectedPartId(partId);
    setStudioInspectorContext(partId ? "part" : "furniture");
    if (!partId) setIsolateSelection(false);
  }, []);

  const currentUiState = useCallback((): WorkspaceUiState => ({
    ...DEFAULT_WORKSPACE_UI_STATE,
    mode: workspaceStage,
    viewMode,
    exploded,
    transparent,
    isolateSelection,
    ...(selectedPartId ? { selectedPartId } : {}),
    panels: workspacePanels,
  }), [exploded, isolateSelection, selectedPartId, transparent, viewMode, workspacePanels, workspaceStage]);

  const writeWorkspaceUrl = useCallback((
    targetProjectId: string | undefined,
    mode: WorkspaceStage,
    history: Exclude<WorkspaceHistoryAction, "none">,
  ) => {
    const next = serializeWorkspaceUrl(window.location.href, {
      ...(targetProjectId ? { projectId: targetProjectId } : {}),
      mode,
    });
    const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (next !== current) {
      if (history === "push") window.history.pushState(window.history.state, "", next);
      else window.history.replaceState(window.history.state, "", next);
    }
    urlIntentRef.current = parseWorkspaceUrl(window.location.search);
  }, []);

  const changeWorkspaceStage = useCallback((
    stage: WorkspaceStage,
    options: WorkspaceStageChangeOptions = {},
  ) => {
    const nextStage = principal && !workspaceSelected && stage !== "explore" && !options.allowWithoutStartPoint
      ? "explore"
      : stage;
    workspaceStageRef.current = nextStage;
    setWorkspaceStage(nextStage);
    if (options.focus !== false) setModeHeadingFocusRequest((request) => request + 1);
    setReferenceImporterOpen(false);
    if (nextStage === "explore") {
      changeSelectedPart(undefined);
    }
    const history = options.history ?? "push";
    if (history !== "none") writeWorkspaceUrl(projectId, nextStage, history);
    return nextStage;
  }, [changeSelectedPart, principal, projectId, workspaceSelected, writeWorkspaceUrl]);

  useEffect(() => {
    if (modeHeadingFocusRequest === 0) return;
    const frame = window.requestAnimationFrame(() => {
      modeHeadingRef.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [modeHeadingFocusRequest]);

  useEffect(() => {
    if (!api.configured) return;
    let cancelled = false;
    void (async () => {
      try {
        await completeOidcCallback();
        if (api.authenticated) {
          const current = await api.getCurrentPrincipal();
          if (!cancelled) {
            setPrincipal(current);
            setApiState("syncing");
            setAuthError(undefined);
          }
        }
      } catch (error) {
        if (!cancelled) setAuthError(error instanceof Error ? error.message : "Inloggningen misslyckades.");
      } finally {
        if (!cancelled) setAuthReady(true);
      }
    })();
    return () => { cancelled = true; };
  }, [api]);

  useEffect(() => {
    workspaceStageRef.current = workspaceStage;
  }, [workspaceStage]);

  useEffect(() => {
    specRef.current = spec;
  }, [spec]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select, [contenteditable='true']")) return;
      const modifier = event.ctrlKey || event.metaKey;
      if (!modifier || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === "z" && !event.shiftKey && past.length > 0) {
        event.preventDefault();
        const previous = past.at(-1);
        if (!previous) return;
        setPast((items) => items.slice(0, -1));
        setFuture((items) => [specRef.current, ...items].slice(0, 50));
        specRef.current = previous;
        setSpec(previous);
        setChangeDiff([]);
        setPartEditNotice(undefined);
        setSaveState("saving");
        if (serverAvailable) setApiState("syncing");
      }
      if ((key === "y" || (key === "z" && event.shiftKey)) && future.length > 0) {
        event.preventDefault();
        const next = future[0];
        if (!next) return;
        setFuture((items) => items.slice(1));
        setPast((items) => [...items, specRef.current].slice(-50));
        specRef.current = next;
        setSpec(next);
        setChangeDiff([]);
        setPartEditNotice(undefined);
        setSaveState("saving");
        if (serverAvailable) setApiState("syncing");
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [future, past, serverAvailable]);

  const applyHydratedWorkspace = useCallback((
    storedSpec: unknown,
    storedTemplateId: unknown,
    selected: boolean,
    storedPlanningBrief?: FurniturePlanningBrief,
    storedUiState: WorkspaceUiState = DEFAULT_WORKSPACE_UI_STATE,
    requestedMode?: WorkspaceStage,
  ) => {
    const restored = normalizeStoredWorkspace(storedSpec);
    const nextSpec = restored?.spec ?? normalizedDefaultSpec();
    specRef.current = nextSpec;
    setSpec(nextSpec);
    setChangeDiff(restored?.diff ?? []);
    setPast([]);
    setFuture([]);
    changeSelectedPart(storedUiState.selectedPartId);
    setViewMode(storedUiState.viewMode);
    setExploded(storedUiState.exploded);
    setTransparent(storedUiState.transparent);
    setIsolateSelection(storedUiState.isolateSelection);
    setWorkspacePanels(storedUiState.panels);
    setServerPreview(undefined);
    setWorkspaceSelected(Boolean(restored && selected));
    setPlanningBrief(storedPlanningBrief ?? DEFAULT_PLANNING_BRIEF);
    setSelectedTemplateId(
      isFurnitureTemplateId(storedTemplateId)
        ? storedTemplateId
        : nextSpec.furniture_type === "wall_library" ? "wall-library" : "shelving",
    );
    const nextMode = restored && selected
      ? requestedMode ?? storedUiState.mode
      : principal ? "explore" : requestedMode ?? "explore";
    workspaceStageRef.current = nextMode;
    setWorkspaceStage(nextMode);
    return nextMode;
  }, [changeSelectedPart, principal]);

  const loadProjectWorkspace = useCallback(async (
    project: Pick<ProjectRead, "id" | "name">,
    requestedMode?: WorkspaceStage,
  ) => {
    if (!principal) return;
    const identity = principal;
    const requestId = ++workspaceLoadRef.current;
    setHydrated(false);
    setServerDraftReady(false);
    setHydrationBlocker(undefined);
    setReferenceImporterOpen(false);
    changeSelectedPart(undefined);
    setServerPreview(undefined);
    setWorkspaceSelected(false);
    setPast([]);
    setFuture([]);
    setProjectError(undefined);
    draftConflictProjectRef.current = undefined;
    setDraftConflict(undefined);
    setDraftConflictBusy(false);
    setDraftConflictCopied(false);
    setProjectId(project.id);
    setActiveProject(project);
    setApiState("syncing");
    setApiMessage(`Öppnar ${project.name} och hämtar dess serverutkast.`);
    try {
      writeSelectedProject(window.localStorage, identity, project);
    } catch {
      setSaveState("error");
    }

    const localDraft = readWorkspaceDraft(window.localStorage, identity, project.id);
    try {
      const draft = await api.getProjectDraft(project.id);
      if (workspaceLoadRef.current !== requestId) return;
      const parsedDraft = parseServerProjectDraft({
        projectId: project.id,
        draftRevision: draft.draft_revision,
        specJson: draft.spec_json,
        workspaceSpecJson: draft.workspace_spec_json,
      });
      serverDraftRevisionRef.current.set(project.id, draft.draft_revision);
      if (parsedDraft.kind === "ready") {
        suppressNextServerDraftSaveRef.current = true;
        const nextMode = applyHydratedWorkspace(
          parsedDraft.spec,
          draft.template_id,
          true,
          localDraft?.planningBrief,
          localDraft?.uiState,
          requestedMode,
        );
        writeWorkspaceUrl(project.id, nextMode, "replace");
      } else if (localDraft) {
        const nextMode = applyHydratedWorkspace(
          localDraft.spec,
          localDraft.templateId,
          localDraft.workspaceSelected,
          localDraft.planningBrief,
          localDraft.uiState,
          requestedMode,
        );
        writeWorkspaceUrl(project.id, nextMode, "replace");
      } else {
        const nextMode = applyHydratedWorkspace(
          normalizedProjectDefaultSpec(project.id),
          undefined,
          false,
          undefined,
          DEFAULT_WORKSPACE_UI_STATE,
          requestedMode,
        );
        writeWorkspaceUrl(project.id, nextMode, "replace");
      }
      setServerDraftReady(true);
      setApiState("synced");
      setApiMessage(`Projektet ${project.name} är öppet och isolerat till din organisation.`);
      setSaveState("saved");
    } catch (error) {
      if (workspaceLoadRef.current !== requestId) return;
      serverDraftRevisionRef.current.delete(project.id);
      const transportFailure = error instanceof ApiError && error.transportFailure;
      if (!transportFailure) {
        setHydrationBlocker({ projectId: project.id, code: "INVALID_SERVER_DRAFT" });
        setServerDraftReady(false);
        setSaveState("error");
        setApiState("error");
        setApiMessage("Serverutkastet stoppades eftersom dess datakontrakt inte kunde verifieras.");
        setProjectError(
          "Serverutkastet är blockerat (INVALID_SERVER_DRAFT). Hämta om projektet eller kontakta support innan någon modell öppnas eller sparas.",
        );
      } else if (localDraft) {
        const nextMode = applyHydratedWorkspace(
          localDraft.spec,
          localDraft.templateId,
          localDraft.workspaceSelected,
          localDraft.planningBrief,
          localDraft.uiState,
          requestedMode,
        );
        writeWorkspaceUrl(project.id, nextMode, "replace");
        setServerDraftReady(false);
        setSaveState("saved");
        setApiState("error");
        setApiMessage(error instanceof Error ? error.message : "Projektets serverutkast kunde inte återställas.");
      } else {
        const nextMode = applyHydratedWorkspace(
          normalizedProjectDefaultSpec(project.id),
          undefined,
          false,
          undefined,
          DEFAULT_WORKSPACE_UI_STATE,
          requestedMode,
        );
        writeWorkspaceUrl(project.id, nextMode, "replace");
        setServerDraftReady(false);
        setSaveState("error");
        setApiState("error");
        setApiMessage(error instanceof Error ? error.message : "Projektets serverutkast kunde inte återställas.");
      }
    } finally {
      if (workspaceLoadRef.current === requestId) setHydrated(true);
    }
  }, [api, applyHydratedWorkspace, changeSelectedPart, principal, writeWorkspaceUrl]);

  useEffect(() => {
    if (!authReady || principal) return;
    const requestId = ++workspaceLoadRef.current;
    let snapshot = readWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID);
    let storageFailed = false;
    // Legacy keys had no identity. Migrate them only in a deliberately offline
    // installation; an authenticated installation must never adopt them on logout.
    if (!snapshot && !api.configured) {
      try {
        const legacySpec = window.localStorage.getItem(LEGACY_STORED_SPEC_KEY);
        const legacyTemplate = window.localStorage.getItem(LEGACY_STORED_TEMPLATE_KEY);
        if (legacySpec) {
          const parsed: unknown = JSON.parse(legacySpec);
          const restored = normalizeStoredWorkspace(parsed);
          if (!restored) throw new Error("Det gamla lokala utkastet saknar en design.");
          const templateId = isFurnitureTemplateId(legacyTemplate)
            ? legacyTemplate
            : restored.spec.furniture_type === "wall_library" ? "wall-library" : "shelving";
          writeWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID, {
            spec: restored.spec,
            templateId,
            workspaceSelected: true,
          });
          snapshot = readWorkspaceDraft(window.localStorage, undefined, ANONYMOUS_PROJECT_ID);
          if (!snapshot) throw new Error("Det gamla lokala utkastet kunde inte verifieras efter migrering.");
          window.localStorage.removeItem(LEGACY_STORED_SPEC_KEY);
          window.localStorage.removeItem(LEGACY_STORED_TEMPLATE_KEY);
          window.localStorage.removeItem(LEGACY_SUPPORT_AUTOMATION_KEY);
        }
      } catch {
        storageFailed = true;
      }
    }
    queueMicrotask(() => {
      if (workspaceLoadRef.current !== requestId) return;
      serverDraftRevisionRef.current.clear();
      draftConflictProjectRef.current = undefined;
      setDraftConflict(undefined);
      setDraftConflictBusy(false);
      setDraftConflictCopied(false);
      setProjects([]);
      setProjectId(undefined);
      setActiveProject(undefined);
      setHydrationBlocker(undefined);
      setProjectError(undefined);
      const requestedMode = initialRequestedWorkspaceMode(urlIntentRef.current);
      let nextMode: WorkspaceStage;
      if (snapshot) {
        nextMode = applyHydratedWorkspace(
          snapshot.spec,
          snapshot.templateId,
          snapshot.workspaceSelected,
          snapshot.planningBrief,
          snapshot.uiState,
          requestedMode,
        );
      } else {
        nextMode = applyHydratedWorkspace(
          undefined,
          undefined,
          false,
          undefined,
          DEFAULT_WORKSPACE_UI_STATE,
          requestedMode,
        );
      }
      writeWorkspaceUrl(undefined, nextMode, "replace");
      if (storageFailed) setSaveState("error");
      else setSaveState("saved");
      setServerDraftReady(true);
      setHydrated(true);
    });
    return () => { workspaceLoadRef.current += 1; };
  }, [api, applyHydratedWorkspace, authReady, principal, writeWorkspaceUrl]);

  useEffect(() => {
    if (!authReady || !principal || !serverAvailable) return;
    let cancelled = false;
    void (async () => {
      const requestedMode = initialRequestedWorkspaceMode(urlIntentRef.current);
      try {
        let availableProjects = (await api.listProjects()).filter((project) => !project.archived);
        if (cancelled) return;
        if (availableProjects.length === 0) {
          const created = await api.createProject(DEFAULT_PROJECT_NAME);
          availableProjects = [created];
        }
        if (cancelled) return;
        setProjects(availableProjects);
        const remembered = readSelectedProject(window.localStorage, principal);
        const selected = selectWorkspaceProject(
          availableProjects,
          urlIntentRef.current.projectId,
          remembered?.id,
        );
        if (!selected) throw new Error("Organisationen saknar ett projekt som kan öppnas.");
        await loadProjectWorkspace(selected, requestedMode);
      } catch (error) {
        if (cancelled) return;
        const remembered = readSelectedProject(window.localStorage, principal);
        if (remembered) {
          setProjects([remembered]);
          await loadProjectWorkspace(remembered, requestedMode);
          return;
        }
        setProjectError(error instanceof Error ? error.message : "Projektlistan kunde inte hämtas.");
        setApiState("error");
        setApiMessage("Projektlistan kunde inte hämtas och inget identitetsbundet lokalt projekt finns.");
        const nextMode = applyHydratedWorkspace(
          undefined,
          undefined,
          false,
          undefined,
          DEFAULT_WORKSPACE_UI_STATE,
          requestedMode,
        );
        writeWorkspaceUrl(undefined, nextMode, "replace");
        setServerDraftReady(false);
        setHydrated(true);
      }
    })();
    return () => {
      cancelled = true;
      workspaceLoadRef.current += 1;
    };
  }, [api, applyHydratedWorkspace, authReady, loadProjectWorkspace, principal, serverAvailable, writeWorkspaceUrl]);

  useEffect(() => {
    if (!hydrated || hydrationBlocker) return;
    const localProjectId = principal ? projectId : ANONYMOUS_PROJECT_ID;
    if (!localProjectId) return;
    const timer = window.setTimeout(() => {
      try {
        writeWorkspaceDraft(window.localStorage, principal, localProjectId, {
          spec,
          templateId: selectedTemplateId,
          workspaceSelected,
          planningBrief,
          uiState: currentUiState(),
        });
        if (!principal || !serverAvailable) setSaveState("saved");
      } catch {
        setSaveState("error");
      }
    }, 450);
    return () => window.clearTimeout(timer);
  }, [currentUiState, hydrated, hydrationBlocker, planningBrief, principal, projectId, selectedTemplateId, serverAvailable, spec, workspaceSelected]);

  const queueServerDraftSave = useCallback((
    targetProjectId: string,
    templateId: FurnitureTemplateId,
    targetSpec: DesignSpec,
  ): Promise<void> => {
    if (draftConflictProjectRef.current === targetProjectId) {
      return Promise.reject(new ApiError(
        "Autosparandet är pausat eftersom serverutkastet har ändrats i en annan flik.",
        409,
        "DRAFT_REVISION_CONFLICT",
        "Kopiera vid behov dina lokala ändringar och hämta sedan senaste utkast.",
      ));
    }
    const queued = serverSaveQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        const expectedDraftRevision = serverDraftRevisionRef.current.get(targetProjectId);
        if (expectedDraftRevision === undefined) {
          const error = new ApiError(
            "Serverutkastets revisionsnummer saknas, så ändringen har inte skrivits över.",
            409,
            "DRAFT_REVISION_UNKNOWN",
            "Ladda om projektet för att hämta den senaste utkastsversionen och försök igen.",
          );
          draftConflictProjectRef.current = targetProjectId;
          setDraftConflict({
            projectId: targetProjectId,
            message: error.message,
            localDraftJson: JSON.stringify({
              template_id: templateId,
              workspace_spec: workspaceIntentEnvelopeFromSpec(targetSpec),
            }, null, 2),
          });
          setDraftConflictCopied(false);
          throw error;
        }
        try {
          const saved = await api.updateProjectDraft(
            targetProjectId,
            templateId,
            targetSpec,
            expectedDraftRevision,
          );
          serverDraftRevisionRef.current.set(targetProjectId, saved.draft_revision);
        } catch (error) {
          if (error instanceof ApiError && error.code === "DRAFT_REVISION_CONFLICT") {
            draftConflictProjectRef.current = targetProjectId;
            setDraftConflict({
              projectId: targetProjectId,
              message: error.message,
              localDraftJson: JSON.stringify({
                template_id: templateId,
                workspace_spec: workspaceIntentEnvelopeFromSpec(targetSpec),
              }, null, 2),
            });
            setDraftConflictCopied(false);
          }
          throw error;
        }
      });
    serverSaveQueueRef.current = queued;
    return queued;
  }, [api]);

  useEffect(() => {
    if (!hydrated || hydrationBlocker || !serverDraftReady || !projectId || !workspaceSelected) return;
    if (suppressNextServerDraftSaveRef.current) {
      suppressNextServerDraftSaveRef.current = false;
      return;
    }
    if (draftConflictProjectRef.current === projectId) return;
    const saveSequence = ++draftSaveSequenceRef.current;
    const timer = window.setTimeout(() => {
      setSaveState("saving");
      void queueServerDraftSave(projectId, selectedTemplateId, spec)
        .then(() => {
          if (draftSaveSequenceRef.current === saveSequence) setSaveState("saved");
        })
        .catch(() => {
          if (draftSaveSequenceRef.current !== saveSequence) return;
          setSaveState("error");
        });
    }, 800);
    return () => window.clearTimeout(timer);
  }, [hydrated, hydrationBlocker, projectId, queueServerDraftSave, selectedTemplateId, serverDraftReady, spec, workspaceSelected]);

  const localDesign = useMemo(() => resolveDesign(spec, changeDiff), [changeDiff, spec]);
  const customInterior = hasCustomInteriorLayout(spec);
  const referenceImageDesign = isReferenceImageDesign(spec);
  const partCustomization = hasPartCustomization(spec);
  const conceptGeometry = referenceImageDesign || partCustomization;
  const design = !conceptGeometry && serverPreview?.requestHash === localDesign.design_hash
    ? mergeServerDesignWithLocalDfm(
        { ...serverPreview.result, change_diff: changeDiff },
        localDesign,
      )
    : localDesign;
  const currentSpecSignature = useMemo(() => workspaceSpecSignature(spec), [spec]);
  const currentPartsSignature = useMemo(() => displayedPartsSignature(design.parts), [design.parts]);
  const previewEvaluation = validationFixPreview
    ? design.rule_evaluations.find((evaluation) => evaluation.rule_id === validationFixPreview.ruleId)
    : undefined;
  const validationFixPreviewIsCurrent = Boolean(
    validationFixPreview
      && workspaceStage === "check"
      && validationFixPreview.sourceSpecSignature === currentSpecSignature
      && validationFixPreview.sourceProjectId === (projectId ?? null)
      && validationFixPreview.sourcePartsSignature === currentPartsSignature
      && previewEvaluation
      && validationEvaluationSignature(previewEvaluation) === validationFixPreview.evaluationSignature,
  );
  const activeValidationFixPreview = validationFixPreviewIsCurrent ? validationFixPreview : undefined;
  const viewerComparisonPreview = useMemo<FurnitureComparisonPreview | undefined>(() => (
    activeValidationFixPreview
      ? {
          proposedParts: activeValidationFixPreview.proposedDesign.parts,
          designSize: {
            widthMm: activeValidationFixPreview.proposedSpec.width_mm,
            heightMm: activeValidationFixPreview.proposedSpec.height_mm,
            depthMm: activeValidationFixPreview.proposedSpec.depth_mm,
          },
          rule: {
            ruleId: activeValidationFixPreview.ruleId,
            ruleVersion: activeValidationFixPreview.ruleVersion,
            title: previewEvaluation?.title ?? activeValidationFixPreview.label,
          },
        }
      : undefined
  ), [activeValidationFixPreview, previewEvaluation?.title]);

  useEffect(() => {
    if (!validationFixPreview || validationFixPreviewIsCurrent) return;
    const invalidToken = validationFixPreview.token;
    queueMicrotask(() => {
      setValidationFixPreview((current) => current?.token === invalidToken ? undefined : current);
    });
  }, [validationFixPreview, validationFixPreviewIsCurrent]);

  const selectedTemplate = furnitureTemplate(selectedTemplateId);
  const blockingEvaluations = design.rule_evaluations.filter((rule) => rule.status === "BLOCK");
  const warningEvaluations = design.rule_evaluations.filter((rule) => rule.status === "WARNING");
  const primaryValidationIssue = blockingEvaluations[0] ?? warningEvaluations[0];
  const designHealthLabel = design.status === "PASS"
    ? "Konstruktion OK"
    : `${design.status === "BLOCK" ? blockingEvaluations.length : warningEvaluations.length} ${design.status === "BLOCK" ? "krav" : "kontroller"} · ${primaryValidationIssue?.title ?? "Öppna byggbarhet"}`;
  const integrityEvaluation = [
    design.rule_evaluations.find((rule) => rule.rule_id === "STR-TOPO-001" && rule.status !== "PASS"),
    design.rule_evaluations.find((rule) => rule.rule_id === "STR-DEF-001" && rule.status !== "PASS"),
    design.rule_evaluations.find((rule) => rule.rule_id === "PART-CUSTOM-001" && rule.status !== "PASS"),
    design.rule_evaluations.find((rule) => rule.rule_id === "STAB-RACK-001" && rule.status !== "PASS"),
    design.rule_evaluations.find((rule) => rule.rule_id === "STAB-TIP-002" && rule.status !== "PASS"),
  ].find(Boolean);
  const displayedApiState: ApiState = conceptGeometry ? "concept" : apiState;
  const displayedApiMessage = referenceImageDesign
    ? "Modellen är tolkad lokalt från en referensbild och måste konstruktionsgranskas innan CAD/CAM."
    : partCustomization
      ? "Enskilda delar har ändrats lokalt. Förband, bärighet och produktionsunderlag måste konstruktionsgranskas."
    : customInterior
      ? "Den anpassade indelningen synkroniseras med serverns parametriska CAD/CAM-motor."
    : apiMessage;

  useEffect(() => {
    if (!serverAvailable || !hydrated || hydrationBlocker || !projectId) return;
    if (isReferenceImageDesign(spec) || hasPartCustomization(spec)) return;
    const controller = new AbortController();
    let reconnectTimer: number | undefined;
    const requestHash = localDesignHash(spec);
    const timer = window.setTimeout(() => {
      const request = spec.reinforcement_mode === "auto"
        ? api.autofixDesign(spec, controller.signal, projectId)
        : api.previewDesign(spec, controller.signal, projectId);
      request
        .then((result) => {
          const authoritativeSpec = spec.reinforcement_mode === "auto"
            ? parseLocalDesignSpec(adaptStructuralSupports({
                ...spec,
                divider_count: result.spec.divider_count,
                back_panel: result.spec.back_panel,
                reinforcement_mode: result.spec.reinforcement_mode,
              }).spec)
            : parseLocalDesignSpec(spec);
          const authoritativeHash = localDesignHash(authoritativeSpec);
          setServerPreview({ requestHash: authoritativeHash, result: { ...result, spec: authoritativeSpec } });
          serverPreviewRetryAttemptRef.current = 0;
          if (authoritativeHash !== requestHash) {
            setSpec(authoritativeSpec);
            setChangeDiff(result.change_diff);
            setSaveState("saving");
          }
          setApiState("synced");
          setApiMessage(
            spec.reinforcement_mode === "auto"
              ? "Serverns konstruktionsregler har räknat om stöd, delar och förband."
              : "Serverns auktoritativa preview är synkroniserad.",
          );
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setApiState("error");
          if (error instanceof ApiError && error.transportFailure) {
            const attempt = serverPreviewRetryAttemptRef.current;
            serverPreviewRetryAttemptRef.current = attempt + 1;
            const retryDelay = Math.min(
              SERVER_PREVIEW_RETRY_BASE_MS * (2 ** Math.min(attempt, 5)),
              SERVER_PREVIEW_RETRY_MAX_MS,
            );
            setApiMessage(`${error.message} Försöker ansluta igen automatiskt.`);
            reconnectTimer = window.setTimeout(() => {
              setServerPreviewRetryNonce((nonce) => nonce + 1);
            }, retryDelay);
          } else {
            serverPreviewRetryAttemptRef.current = 0;
            setApiMessage(error instanceof Error ? error.message : "Ett okänt API-fel inträffade.");
          }
        });
    }, 500);
    return () => {
      window.clearTimeout(timer);
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      controller.abort();
    };
  }, [api, hydrated, hydrationBlocker, projectId, serverAvailable, serverPreviewRetryNonce, spec]);

  const effectiveSelectedPartId = selectedPartId && design.parts.some((part) => part.part_id === selectedPartId)
    ? selectedPartId
    : undefined;
  const selectedPart = effectiveSelectedPartId
    ? design.parts.find((part) => part.part_id === effectiveSelectedPartId)
    : undefined;

  const replaceSpec = useCallback((next: DesignSpec, diff: ChangeDiff[] = []): boolean => {
    const transaction = normalizeWorkspaceDesignTransaction(spec, next, diff);
    const normalized = transaction.normalizedSpec;
    if (transaction.changedFields.length === 0) return false;
    specRef.current = normalized;
    setPast((items) => [...items, spec].slice(-50));
    setFuture([]);
    setSpec(normalized);
    setChangeDiff(transaction.changeDiff);
    setSaveState("saving");
    if (serverAvailable) {
      setApiState("syncing");
      setApiMessage("Synkroniserar samma DesignSpec mot serverns auktoritativa motor.");
    }
    return true;
  }, [serverAvailable, spec]);

  const updateSpec = useCallback((patch: Partial<DesignSpec>, reason?: string) => {
    try {
      replaceSpec(parseLocalDesignPatch(spec, patch), []);
      if (reason) setPartEditNotice(reason);
    } catch (error) {
      if (!(error instanceof DesignHydrationError)) throw error;
      setPartEditNotice(undefined);
      setSemanticNotice({
        title: "Ändringen kunde inte genomföras",
        detail: error.message,
        error: true,
      });
    }
  }, [replaceSpec, spec]);

  const requestValidationFixPreview = useCallback((evaluation: RuleEvaluation) => {
    try {
      const fix = automaticValidationFix(evaluation, spec);
      if (!fix) return;
      const candidate = parseLocalDesignPatch(spec, fix.patch);
      const requestedDiff = requestedValidationDiff(spec, fix.patch, fix.reason);
      const transaction = previewWorkspaceDesignTransaction(spec, candidate, requestedDiff);
      const token = `${evaluation.rule_id}:${evaluation.rule_version}:${++validationPreviewSequenceRef.current}`;
      setValidationFixPreview({
        token,
        sourceSpec: spec,
        sourceSpecSignature: workspaceSpecSignature(spec),
        sourceProjectId: projectId ?? null,
        sourceParts: design.parts,
        sourcePartsSignature: displayedPartsSignature(design.parts),
        evaluationSignature: validationEvaluationSignature(evaluation),
        ruleId: evaluation.rule_id,
        ruleVersion: evaluation.rule_version,
        label: fix.label,
        reason: fix.reason,
        proposedSpec: transaction.normalizedSpec,
        proposedDesign: transaction.resolvedDesign,
        requestedDiff: transaction.requestedDiff,
        structuralDiff: transaction.structuralDiff,
        changeDiff: transaction.changeDiff,
        changes: transaction.changedFields,
        noGeometryChange: !comparisonGeometryChanged(
          design.parts,
          { widthMm: spec.width_mm, heightMm: spec.height_mm, depthMm: spec.depth_mm },
          transaction.resolvedDesign.parts,
          {
            widthMm: transaction.normalizedSpec.width_mm,
            heightMm: transaction.normalizedSpec.height_mm,
            depthMm: transaction.normalizedSpec.depth_mm,
          },
        ),
      });
      setSemanticNotice(undefined);
    } catch (error) {
      if (!(error instanceof DesignHydrationError)) throw error;
      setValidationFixPreview(undefined);
      setSemanticNotice({
        title: "Förhandsvisningen kunde inte beräknas",
        detail: error.message,
        error: true,
      });
    }
  }, [design.parts, projectId, spec]);

  const cancelValidationFixPreview = useCallback(() => {
    setValidationFixPreview(undefined);
  }, []);

  const confirmValidationFixPreview = useCallback(() => {
    const preview = activeValidationFixPreview;
    if (!preview || confirmedValidationPreviewTokenRef.current === preview.token) return;
    confirmedValidationPreviewTokenRef.current = preview.token;
    setValidationFixPreview(undefined);
    const applied = replaceSpec(preview.proposedSpec, preview.changeDiff);
    if (applied) {
      setSemanticNotice(undefined);
      setPartEditNotice(`${preview.reason}. Den lokala förhandsvisningen har nu tillämpats i utkastet.`);
    } else {
      setPartEditNotice(undefined);
      setSemanticNotice({
        title: "Ingen ändring tillämpades",
        detail: "Förslaget matchar redan det aktuella normaliserade utkastet.",
      });
    }
  }, [activeValidationFixPreview, replaceSpec]);

  const restoreLastSaved = useCallback(() => {
    const localProjectId = principal ? projectId : ANONYMOUS_PROJECT_ID;
    if (!localProjectId) return;
    const snapshot = readWorkspaceDraft(window.localStorage, principal, localProjectId);
    if (!snapshot) {
      setProjectError("Det finns inget sparat utkast att återställa ännu.");
      return;
    }
    const nextMode = applyHydratedWorkspace(
      snapshot.spec,
      snapshot.templateId,
      snapshot.workspaceSelected,
      snapshot.planningBrief,
      snapshot.uiState,
    );
    writeWorkspaceUrl(principal ? projectId : undefined, nextMode, "replace");
    setProjectError(undefined);
    setSaveState("saved");
    setPartEditNotice("Det senast sparade utkastet återställdes.");
  }, [applyHydratedWorkspace, principal, projectId, writeWorkspaceUrl]);

  const applySemanticDrop = useCallback((request: SemanticDropRequest) => {
    try {
      const outcome = resolveSemanticDrop(spec, request);
      replaceSpec(outcome.spec, outcome.diff);
      setSemanticNotice({ title: outcome.message, detail: outcome.detail });
      setSemanticDragKind(undefined);
      changeSelectedPart(undefined);
      if (request.kind === "shelf_row" || request.kind === "divider" || request.kind === "base_cabinet") {
      }
    } catch (error) {
      setSemanticNotice({
        title: "Delen kunde inte placeras",
        detail: error instanceof Error ? error.message : "Välj en annan position i modellen.",
        error: true,
      });
      setSemanticDragKind(undefined);
    }
  }, [changeSelectedPart, replaceSpec, spec]);

  const updateSelectedPart = useCallback((partId: string, patch: PartOverride) => {
    const edited = editPartParametrically(spec, partId, patch);
    setPartEditNotice(edited.notice);
    if (edited.supported) replaceSpec(edited.spec);
  }, [replaceSpec, spec]);

  const resetSelectedPart = useCallback((partId: string) => {
    const partOverrides = { ...spec.part_overrides };
    delete partOverrides[partId];
    const shelf = /^shelf-(\d+)-bay-\d+$/.exec(partId);
    const divider = /^divider-(\d+)$/.exec(partId);
    let next = { ...spec, part_overrides: partOverrides };
    if (shelf && spec.shelf_count > 0) {
      const index = Math.min(spec.shelf_count - 1, Math.max(0, Number(shelf[1]) - 1));
      next = setShelfHeightRatio(next, index, (index + 1) / (spec.shelf_count + 1));
      setPartEditNotice("Den valda hyllraden återställdes utan att övriga hyllnivåer förlorades.");
    } else if (divider && spec.divider_count > 0) {
      const index = Math.min(spec.divider_count - 1, Math.max(0, Number(divider[1]) - 1));
      next = setBayWidthRatio(next, index, 1 / (spec.divider_count + 1));
      setPartEditNotice("Den valda fackgränsen återställdes och hyllsegmenten räknades om.");
    } else {
      setPartEditNotice("Delens parametriska mått återställs genom möbelns yttermått och materialval.");
    }
    replaceSpec(next);
  }, [replaceSpec, spec]);

  const updateShelfOpening = useCallback((openingIndex: number, valueMm: number) => {
    const next = setShelfOpeningHeight(spec, openingIndex, valueMm);
    const appliedValue = Math.round(shelfOpeningHeights(next)[openingIndex] ?? valueMm);
    setPartEditNotice(`Det fria hyllavståndet sattes till ${appliedValue} mm och modellen räknades om.`);
    replaceSpec(next);
  }, [replaceSpec, spec]);

  const removeSelectedPart = useCallback((partId: string) => {
    const removal = removePartFromDesign(spec, partId);
    setPartEditNotice(removal.notice);
    replaceSpec(removal.spec, removal.diff);
    changeSelectedPart(undefined);
  }, [changeSelectedPart, replaceSpec, spec]);

  const resetAllPartCustomizations = useCallback(() => {
    replaceSpec(restorePartCustomizations(spec));
    setPartEditNotice("Alla individuella del- och indelningsändringar återställdes.");
    changeSelectedPart(undefined);
  }, [changeSelectedPart, replaceSpec, spec]);

  const undo = () => {
    const previous = past.at(-1);
    if (!previous) return;
    setPast((items) => items.slice(0, -1));
    setFuture((items) => [spec, ...items].slice(0, 50));
    specRef.current = previous;
    setSpec(previous);
    setChangeDiff([]);
    setPartEditNotice(undefined);
    setSaveState("saving");
    if (serverAvailable) setApiState("syncing");
  };

  const redo = () => {
    const next = future[0];
    if (!next) return;
    setFuture((items) => items.slice(1));
    setPast((items) => [...items, spec].slice(-50));
    specRef.current = next;
    setSpec(next);
    setChangeDiff([]);
    setPartEditNotice(undefined);
    setSaveState("saving");
    if (serverAvailable) setApiState("syncing");
  };

  const persistLocalWorkspace = useCallback((
    targetSpec: DesignSpec,
    templateId: FurnitureTemplateId,
    selected: boolean,
    brief: FurniturePlanningBrief,
  ) => {
    const localProjectId = principal ? projectId : ANONYMOUS_PROJECT_ID;
    if (!localProjectId) return;
    try {
      writeWorkspaceDraft(window.localStorage, principal, localProjectId, {
        spec: targetSpec,
        templateId,
        workspaceSelected: selected,
        planningBrief: brief,
        uiState: currentUiState(),
      });
      if (!serverAvailable) setSaveState("saved");
    } catch {
      setSaveState("error");
    }
  }, [currentUiState, principal, projectId, serverAvailable]);

  const savePlanningBrief = useCallback((brief: FurniturePlanningBrief) => {
    setPlanningBrief(brief);
    persistLocalWorkspace(specRef.current, selectedTemplateId, workspaceSelected, brief);
  }, [persistLocalWorkspace, selectedTemplateId, workspaceSelected]);

  const selectFurnitureTemplate = useCallback((template: FurnitureTemplate, brief: FurniturePlanningBrief) => {
    setPlanningBrief(brief);
    setWorkspaceSelected(true);
    setSelectedTemplateId(template.id);
    setReferenceImporterOpen(false);
    changeWorkspaceStage("studio", { allowWithoutStartPoint: true });
    setCameraResetNonce((value) => value + 1);
    setPartEditNotice(undefined);
    replaceSpec({
      ...DEFAULT_DESIGN_SPEC,
      design_id: specRef.current.design_id,
      revision: specRef.current.revision,
      ...template.patch,
    }, []);
    persistLocalWorkspace(specRef.current, template.id, true, brief);
  }, [changeWorkspaceStage, persistLocalWorkspace, replaceSpec]);

  const applyReferenceImage = useCallback((result: ReferenceImageResult) => {
    setWorkspaceSelected(true);
    const templateId: FurnitureTemplateId = result.patch.furniture_type === "wall_library" ? "wall-library" : "shelving";
    setSelectedTemplateId(templateId);
    setReferenceImporterOpen(false);
    changeWorkspaceStage("studio", { allowWithoutStartPoint: true });
    setCameraResetNonce((value) => value + 1);
    setPartEditNotice(undefined);
    replaceSpec({
      ...DEFAULT_DESIGN_SPEC,
      design_id: specRef.current.design_id,
      revision: specRef.current.revision,
      ...result.patch,
      reference_image_import: result.metadata,
    }, []);
    persistLocalWorkspace(specRef.current, templateId, true, {
      ...planningBrief,
      startMode: "reference",
      selectedTemplateId: templateId,
    });
  }, [changeWorkspaceStage, persistLocalWorkspace, planningBrief, replaceSpec]);

  const inspectReferenceImage = useCallback(async (file: File) => {
    if (!serverAvailable || !projectId) {
      throw new ApiError(
        "Referensbilden måste först sparas som ett oföränderligt projektunderlag på servern.",
        503,
        "REFERENCE_IMPORT_SERVER_REQUIRED",
        "Logga in, öppna ett serverprojekt och försök ladda upp bilden igen.",
      );
    }
    setApiState("syncing");
    setApiMessage("Sparar referensbildens original och verifierar filens SHA-256 på servern.");
    try {
      const inspection = await api.inspectReferenceImage(projectId, file);
      setApiState("synced");
      setApiMessage(`Referensbilden är sparad oföränderligt som import ${inspection.import_id}.`);
      return inspection;
    } catch (error) {
      setApiState("error");
      setApiMessage(error instanceof Error ? error.message : "Referensbilden kunde inte sparas på servern.");
      throw error;
    }
  }, [api, projectId, serverAvailable]);

  const openReferenceImporter = useCallback((brief?: FurniturePlanningBrief) => {
    if (brief) savePlanningBrief(brief);
    setReferenceImporterOpen(true);
  }, [savePlanningBrief]);

  const beginDimensionResize = useCallback(() => {
    resizeSnapshotRef.current ??= specRef.current;
  }, []);

  const resizeDimension = useCallback((patch: Partial<Pick<DesignSpec, "width_mm" | "height_mm" | "depth_mm">>) => {
    try {
      const boundedNext = parseLocalDesignPatch(specRef.current, patch);
      const adapted = adaptStructuralSupports(balanceDesignSymmetry(boundedNext));
      const normalized = parseLocalDesignSpec(balanceDesignSymmetry(adapted.spec));
      specRef.current = normalized;
      setSpec(normalized);
      setChangeDiff(adapted.diff);
      setSaveState("saving");
      if (serverAvailable) setApiState("syncing");
    } catch (error) {
      if (!(error instanceof DesignHydrationError)) throw error;
      setPartEditNotice(undefined);
      setSemanticNotice({
        title: "Storleksändringen stoppades",
        detail: error.message,
        error: true,
      });
    }
  }, [serverAvailable]);

  const finishDimensionResize = useCallback(() => {
    const before = resizeSnapshotRef.current;
    resizeSnapshotRef.current = undefined;
    if (!before || localDesignHash(before) === localDesignHash(specRef.current)) return;
    setPast((items) => [...items, before].slice(-50));
    setFuture([]);
  }, []);

  const beginPartMove = useCallback(() => {
    partMoveSnapshotRef.current ??= specRef.current;
    partMoveNoticeRef.current = undefined;
    setPartEditNotice(undefined);
  }, []);

  const movePart = useCallback((partId: string, positionZMm: number) => {
    const moved = movePartVertically(specRef.current, partId, positionZMm);
    const adapted = adaptStructuralSupports(balanceDesignSymmetry(moved.spec));
    const normalized = balanceDesignSymmetry(adapted.spec);
    specRef.current = normalized;
    partMoveNoticeRef.current = moved.notice;
    setSpec(normalized);
    setChangeDiff(adapted.diff);
    setSaveState("saving");
    if (serverAvailable) setApiState("syncing");
  }, [serverAvailable]);

  const movePartHorizontally = useCallback((partId: string, positionXMm: number) => {
    const moved = editPartParametrically(specRef.current, partId, { position_x_mm: positionXMm });
    if (!moved.supported) return;
    const adapted = adaptStructuralSupports(balanceDesignSymmetry(moved.spec));
    const normalized = balanceDesignSymmetry(adapted.spec);
    specRef.current = normalized;
    partMoveNoticeRef.current = moved.notice;
    setSpec(normalized);
    setChangeDiff(adapted.diff);
    setSaveState("saving");
    if (serverAvailable) setApiState("syncing");
  }, [serverAvailable]);

  const finishPartMove = useCallback(() => {
    const before = partMoveSnapshotRef.current;
    partMoveSnapshotRef.current = undefined;
    if (!before || localDesignHash(before) === localDesignHash(specRef.current)) return;
    setPast((items) => [...items, before].slice(-50));
    setFuture([]);
    setPartEditNotice(partMoveNoticeRef.current);
    partMoveNoticeRef.current = undefined;
  }, []);

  const startLogin = useCallback(() => {
    setAuthError(undefined);
    void beginOidcLogin().catch((error: unknown) => {
      setAuthError(error instanceof Error ? error.message : "Inloggningen kunde inte startas.");
    });
  }, []);

  const copyConflictedDraft = useCallback(() => {
    if (!draftConflict) return;
    if (!navigator.clipboard) {
      setProjectError("Webbläsaren tillåter inte automatisk kopiering. Dina ändringar finns fortfarande kvar på skärmen.");
      return;
    }
    void navigator.clipboard.writeText(draftConflict.localDraftJson)
      .then(() => {
        setDraftConflictCopied(true);
        setProjectError(undefined);
      })
      .catch(() => {
        setProjectError("Det lokala utkastet kunde inte kopieras automatiskt. Dina ändringar finns fortfarande kvar på skärmen.");
      });
  }, [draftConflict]);

  const reloadLatestDraft = useCallback(() => {
    if (!draftConflict || draftConflict.projectId !== projectId || draftConflictBusy) return;
    const targetProjectId = draftConflict.projectId;
    const requestId = ++workspaceLoadRef.current;
    setDraftConflictBusy(true);
    setHydrated(false);
    setServerDraftReady(false);
    setSaveState("saving");
    setApiState("syncing");
    setApiMessage("Hämtar den senaste serverrevisionen utan att skriva över den.");
    void api.getProjectDraft(targetProjectId)
      .then((draft) => {
        if (workspaceLoadRef.current !== requestId) return;
        const parsedDraft = parseServerProjectDraft({
          projectId: targetProjectId,
          draftRevision: draft.draft_revision,
          specJson: draft.spec_json,
          workspaceSpecJson: draft.workspace_spec_json,
        });
        if (parsedDraft.kind !== "ready") {
          throw new DesignHydrationError("INVALID_SERVER_DRAFT", [
            "Konfliktrevisionen saknar ett fullständigt serverutkast.",
          ]);
        }
        serverDraftRevisionRef.current.set(targetProjectId, draft.draft_revision);
        suppressNextServerDraftSaveRef.current = true;
        const nextMode = applyHydratedWorkspace(
          parsedDraft.spec,
          draft.template_id,
          true,
          planningBrief,
          DEFAULT_WORKSPACE_UI_STATE,
          workspaceStageRef.current,
        );
        writeWorkspaceUrl(targetProjectId, nextMode, "replace");
        draftConflictProjectRef.current = undefined;
        setDraftConflict(undefined);
        setDraftConflictCopied(false);
        setProjectError(undefined);
        setServerDraftReady(true);
        setSaveState("saved");
        setApiState("synced");
        setApiMessage(`Senaste serverutkastet (revision ${draft.draft_revision}) har hämtats. Nästa ändring sparas ovanpå den revisionen.`);
      })
      .catch((error: unknown) => {
        if (workspaceLoadRef.current !== requestId) return;
        if (
          (error instanceof DesignHydrationError && error.code === "INVALID_SERVER_DRAFT")
          || (error instanceof ApiError && !error.transportFailure)
        ) {
          serverDraftRevisionRef.current.delete(targetProjectId);
          setHydrationBlocker({ projectId: targetProjectId, code: "INVALID_SERVER_DRAFT" });
          setProjectError(
            "Serverutkastet är blockerat (INVALID_SERVER_DRAFT). Hämta om projektet eller kontakta support innan någon modell öppnas eller sparas.",
          );
        } else {
          setProjectError(error instanceof Error ? error.message : "Det senaste serverutkastet kunde inte hämtas.");
        }
        setSaveState("error");
        setApiState("error");
      })
      .finally(() => {
        if (workspaceLoadRef.current !== requestId) return;
        setDraftConflictBusy(false);
        setHydrated(true);
      });
  }, [api, applyHydratedWorkspace, draftConflict, draftConflictBusy, planningBrief, projectId, writeWorkspaceUrl]);

  const persistCurrentWorkspace = useCallback(async () => {
    if (hydrationBlocker) {
      throw new DesignHydrationError("INVALID_SERVER_DRAFT", [
        "Det blockerade serverutkastet får inte skrivas över från arbetsytan.",
      ]);
    }
    const currentSpec = specRef.current;
    persistLocalWorkspace(currentSpec, selectedTemplateId, workspaceSelected, planningBrief);
    if (!serverAvailable || !projectId || !workspaceSelected) return;
    setSaveState("saving");
    await queueServerDraftSave(projectId, selectedTemplateId, currentSpec);
    setSaveState("saved");
  }, [hydrationBlocker, persistLocalWorkspace, planningBrief, projectId, queueServerDraftSave, selectedTemplateId, serverAvailable, workspaceSelected]);

  useEffect(() => {
    if (!hydrated) return;

    const handlePopState = () => {
      const parsed = parseWorkspaceUrl(window.location.search);
      urlIntentRef.current = parsed;

      if (!principal) {
        const localDraft = readWorkspaceDraft(
          window.localStorage,
          undefined,
          ANONYMOUS_PROJECT_ID,
        );
        const nextMode = selectWorkspaceMode(
          parsed,
          localDraft?.uiState.mode ?? workspaceStageRef.current,
        );
        const appliedMode = changeWorkspaceStage(nextMode, { history: "none", focus: false });
        writeWorkspaceUrl(undefined, appliedMode, "replace");
        return;
      }

      const remembered = readSelectedProject(window.localStorage, principal);
      const targetProject = selectWorkspaceProject(
        projects,
        parsed.projectId,
        remembered?.id ?? projectId,
      );
      if (!targetProject) {
        const nextMode = selectWorkspaceMode(parsed, workspaceStageRef.current);
        const appliedMode = changeWorkspaceStage(nextMode, { history: "none", focus: false });
        writeWorkspaceUrl(projectId, appliedMode, "replace");
        return;
      }

      if (targetProject.id === projectId) {
        const localDraft = readWorkspaceDraft(window.localStorage, principal, targetProject.id);
        const nextMode = selectWorkspaceMode(
          parsed,
          localDraft?.uiState.mode ?? workspaceStageRef.current,
        );
        const appliedMode = changeWorkspaceStage(nextMode, { history: "none", focus: false });
        writeWorkspaceUrl(targetProject.id, appliedMode, "replace");
        return;
      }

      void persistCurrentWorkspace()
        .then(() => {
          setReferenceImporterOpen(false);
          changeSelectedPart(undefined);
          return loadProjectWorkspace(targetProject, parsed.mode);
        })
        .catch((error: unknown) => {
          setSaveState("error");
          setProjectError(
            `Projektet i webbläsarhistoriken kunde inte öppnas eftersom utkastet inte kunde sparas. ${error instanceof Error ? error.message : "Försök igen."}`,
          );
          writeWorkspaceUrl(projectId, workspaceStageRef.current, "replace");
        });
    };

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [
    changeSelectedPart,
    changeWorkspaceStage,
    hydrated,
    loadProjectWorkspace,
    persistCurrentWorkspace,
    principal,
    projectId,
    projects,
    writeWorkspaceUrl,
  ]);

  const switchProject = useCallback((nextProjectId: string) => {
    const project = projects.find((candidate) => candidate.id === nextProjectId);
    if (!project || project.id === projectId) return;
    const requestedMode = workspaceStageRef.current;
    const prepareSwitch = hydrationBlocker ? Promise.resolve() : persistCurrentWorkspace();
    void prepareSwitch
      .then(() => {
        setReferenceImporterOpen(false);
        changeSelectedPart(undefined);
        writeWorkspaceUrl(project.id, requestedMode, "push");
        return loadProjectWorkspace(project, requestedMode);
      })
      .catch((error: unknown) => {
        setSaveState("error");
        setProjectError(`Projektet byttes inte eftersom utkastet inte kunde sparas. ${error instanceof Error ? error.message : "Försök igen."}`);
      });
  }, [changeSelectedPart, hydrationBlocker, loadProjectWorkspace, persistCurrentWorkspace, projectId, projects, writeWorkspaceUrl]);

  const createProject = useCallback(() => {
    const name = projectCreateName.trim();
    if (!hydrated || !name || projectCreateBusy) return;
    const lifecycleId = workspaceLoadRef.current;
    setProjectCreateBusy(true);
    setProjectError(undefined);
    void (async () => {
      try {
        if (!hydrationBlocker) await persistCurrentWorkspace();
        if (workspaceLoadRef.current !== lifecycleId) return;
        const project = await api.createProject(name);
        if (workspaceLoadRef.current !== lifecycleId) return;
        setProjects((current) => [project, ...current.filter((candidate) => candidate.id !== project.id)]);
        setProjectCreateName("");
        setProjectCreateOpen(false);
        writeWorkspaceUrl(project.id, "explore", "push");
        await loadProjectWorkspace(project, "explore");
      } catch (error) {
        if (workspaceLoadRef.current !== lifecycleId) return;
        setProjectError(
          error instanceof ApiError && error.status === 409
            ? "Det finns redan ett projekt med det namnet. Ange ett annat namn."
            : error instanceof Error ? error.message : "Projektet kunde inte skapas.",
        );
      } finally {
        setProjectCreateBusy(false);
      }
    })();
  }, [api, hydrated, hydrationBlocker, loadProjectWorkspace, persistCurrentWorkspace, projectCreateBusy, projectCreateName, writeWorkspaceUrl]);

  const logout = useCallback(() => {
    clearProductionSession(window.sessionStorage, principal);
    clearLegacyProductionStorage(window.localStorage);
    clearOidcSession();
    workspaceLoadRef.current += 1;
    serverDraftRevisionRef.current.clear();
    draftConflictProjectRef.current = undefined;
    suppressNextServerDraftSaveRef.current = false;
    setDraftConflict(undefined);
    setDraftConflictBusy(false);
    setDraftConflictCopied(false);
    setHydrated(false);
    setPrincipal(undefined);
    setProjectId(undefined);
    setProjects([]);
    setActiveProject(undefined);
    setProjectCreateOpen(false);
    setProjectCreateBusy(false);
    setHydrationBlocker(undefined);
    setProjectError(undefined);
    setWorkspaceSelected(false);
    setPlanningBrief(DEFAULT_PLANNING_BRIEF);
    setServerDraftReady(false);
    setSpec(normalizedDefaultSpec());
    setPast([]);
    setFuture([]);
    setChangeDiff([]);
    changeSelectedPart(undefined);
    setServerPreview(undefined);
    setApiState("offline");
    setApiMessage("Du är utloggad. Den separata anonyma arbetsytan återställs lokalt.");
  }, [changeSelectedPart, principal]);

  const saveTarget = projectId ? "på servern" : "lokalt";
  const saveLabel = saveState === "saving"
    ? `Sparar ${saveTarget}…`
    : saveState === "error"
      ? `Kunde inte spara ${saveTarget}`
      : `Sparad ${saveTarget}`;

  const handleWorkspaceStageChange = changeWorkspaceStage;
  const hasCanvasStateBanner = Boolean(
    authError
    || (projectError && !projectCreateOpen)
    || displayedApiState === "offline"
    || displayedApiState === "error",
  );

  return (
    <div className={`app-shell cb-redesign-shell ${studioStyles.productShell}`}>
      <a className="cb-skip-link" href="#workspace-mode-heading">Hoppa till huvudinnehåll</a>
      <aside className="side-nav" aria-label="Custombuild">
        <a className="brand" href="#workspace" aria-label="Custombuild arbetsyta">
          <span className="brand-mark"><Box aria-hidden="true" size={20} /></span>
          <span className="brand-name">Custom<span>build</span></span>
        </a>
        <div className="profile-card">
          <span className="avatar">{principal?.name?.slice(0, 2).toUpperCase() ?? "CB"}</span>
          <span><strong>{principal?.name ?? "Lokal användare"}</strong><small>{principal ? principal.role : "Arbetsyta"}</small></span>
        </div>
      </aside>

      <main className="main-shell cb-redesign-main" id="workspace">
        <header className="top-header">
          <div className="project-identity">
            <div className="breadcrumb">
              <span>Projekt</span><span>/</span>
              {principal ? (
                <span className="project-switcher">
                  <select
                    aria-label="Aktivt projekt"
                    value={projectId ?? ""}
                    disabled={(!hydrated && !hydrationBlocker) || projects.length === 0}
                    onChange={(event) => switchProject(event.target.value)}
                  >
                    {!projectId ? <option value="">Hämtar projekt…</option> : null}
                    {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
                  </select>
                  <button
                    type="button"
                    className="new-product-button"
                    aria-label="Skapa ny produkt"
                    title="Skapa ny produkt i ett separat projekt"
                    disabled={!hydrated || projectCreateBusy}
                    onClick={() => {
                      if (!hydrated || projectCreateBusy) return;
                      setProjectCreateOpen((open) => !open);
                      setProjectError(undefined);
                    }}
                  >
                    <Plus aria-hidden="true" size={13} />
                    <span>Ny produkt</span>
                  </button>
                </span>
              ) : <span>Lokalt utkast</span>}
            </div>
            <div className="project-title-row">
              <h1>{activeProject?.name ?? "Skapa din möbel"}</h1>
              <span className="revision-chip">
                <GitBranch aria-hidden="true" size={13} /> Designutkast
              </span>
            </div>
            {projectCreateOpen && principal ? (
              <form
                className="project-create-popover"
                onSubmit={(event) => {
                  event.preventDefault();
                  createProject();
                }}
              >
                <label htmlFor="new-project-name">Nytt projekt</label>
                <p>Den nya produkten får ett separat utkast. Den öppna modellen behålls.</p>
                <div>
                  <input
                    id="new-project-name"
                    autoFocus
                    maxLength={180}
                    placeholder="Projektnamn"
                    disabled={!hydrated || projectCreateBusy}
                    value={projectCreateName}
                    onChange={(event) => setProjectCreateName(event.target.value)}
                  />
                  <button type="submit" disabled={!hydrated || !projectCreateName.trim() || projectCreateBusy}>
                    {projectCreateBusy ? <LoaderCircle aria-hidden="true" className="spin" size={14} /> : <Plus aria-hidden="true" size={14} />}
                    Skapa
                  </button>
                  <button type="button" onClick={() => setProjectCreateOpen(false)}>Avbryt</button>
                </div>
                {projectError ? <small role="alert">{projectError}</small> : null}
              </form>
            ) : null}
          </div>
          <div className="header-actions">
            <div className="save-state" aria-live="polite">
              {saveState === "saved" ? <Check aria-hidden="true" size={13} /> : <Save aria-hidden="true" size={13} />}
              {saveLabel}
            </div>
            <button
              type="button"
              className="save-draft-button"
              disabled={!hydrated || Boolean(hydrationBlocker) || saveState === "saving"}
              onClick={() => {
                void persistCurrentWorkspace().catch((error: unknown) => {
                  setSaveState("error");
                  setProjectError(error instanceof Error ? error.message : "Utkastet kunde inte sparas.");
                });
              }}
            >
              <Save aria-hidden="true" size={15} /> Spara utkast
            </button>
            <ApiIndicator state={displayedApiState} message={displayedApiMessage} />
            {oidcConfigured() ? (
              principal ? (
                <button type="button" className="auth-session-button" onClick={logout}><LogOut aria-hidden="true" size={15} /> Logga ut</button>
              ) : (
                <button type="button" className="auth-session-button" onClick={startLogin} disabled={!authReady}><LogIn aria-hidden="true" size={15} /> Logga in</button>
              )
            ) : null}
            <span className="header-divider" />
            <button type="button" className="icon-button" aria-label="Ångra" title="Ångra" disabled={Boolean(hydrationBlocker) || past.length === 0} onClick={undo}><Undo2 aria-hidden="true" size={17} /></button>
              <button type="button" className="icon-button" aria-label="Gör om" title="Gör om" disabled={Boolean(hydrationBlocker) || future.length === 0} onClick={redo}><Redo2 aria-hidden="true" size={17} /></button>
            <button
              type="button"
              className="icon-button"
              aria-label="Återställ senast sparade utkast"
              title="Återställ senast sparade utkast"
              disabled={!hydrated || Boolean(hydrationBlocker)}
              onClick={restoreLastSaved}
            >
              <Cloud aria-hidden="true" size={17} />
            </button>
          </div>
        </header>

        {hydrationBlocker ? (
          <section className="offline-banner error" role="alert" data-testid="server-draft-hydration-blocker">
            <CloudOff aria-hidden="true" size={18} />
            <span>
              <strong>Projektets modell har inte öppnats.</strong>{" "}
              Serverutkastet klarade inte det strikta datakontraktet ({hydrationBlocker.code}).
              Ingen lokal modell har ersatt serverunderlaget och autosparandet är stoppat.
            </span>
            <button
              type="button"
              onClick={() => {
                if (activeProject) void loadProjectWorkspace(activeProject, workspaceStageRef.current);
              }}
            >
              Försök hämta igen
            </button>
          </section>
        ) : !hydrated && principal ? (
          <section className="viewer-loading" role="status">
            <LoaderCircle aria-hidden="true" className="spin" size={22} />
            Verifierar projektets serverutkast…
          </section>
        ) : (
        <>
        <WorkspaceNavigation
          current={workspaceStage}
          startPointSelected={!principal || workspaceSelected}
          onStageChange={handleWorkspaceStageChange}
        />

        <h2
          className="cb-visually-hidden"
          id="workspace-mode-heading"
          ref={modeHeadingRef}
          tabIndex={-1}
        >
          {WORKSPACE_MODE_HEADINGS[workspaceStage]}
        </h2>

        {workspaceStage === "explore" ? (
          <div className={studioStyles.explorePage}>
            {workspaceSelected && activeProject ? (
              <aside className="existing-product-explore-notice" aria-label="Befintligt produktutkast">
                <strong>Du utforskar startpunkter i {activeProject.name}.</strong>
                <span>
                  Ett nytt modellval ersätter utkastet i detta projekt. Välj <b>Ny produkt</b> ovan
                  för att behålla modellen och börja i ett separat projekt.
                </span>
              </aside>
            ) : null}
            <TemplatePicker
              key={principal ? projectId ?? "server-project-loading" : ANONYMOUS_PROJECT_ID}
              open
              presentation="embedded"
              selectedId={selectedTemplateId}
              required={!workspaceSelected}
              initialBrief={planningBrief}
              onBriefChange={savePlanningBrief}
              onSelect={selectFurnitureTemplate}
              onUploadImage={openReferenceImporter}
              onClose={() => { if (workspaceSelected) changeWorkspaceStage("studio"); }}
            />
          </div>
        ) : (
        <div
          className={`workspace-grid configurator-workspace cb-stage-${workspaceStage} ${studioStyles.workspace}`}
          data-mode={workspaceStage}
          data-library={workspacePanels.componentLibraryOpen && workspaceStage === "studio"}
        >
          <section
            className={`viewer-panel ${styles.semanticViewerPanel} ${studioStyles.modelStage} ${hasCanvasStateBanner ? "has-canvas-state-banner" : ""}`}
            aria-label="Konstruktionsvy"
          >
            <div
              className="viewer-toolbar"
              role="toolbar"
              aria-label="Visningsverktyg"
              aria-describedby="viewer-toolbar-scroll-help"
            >
              <span id="viewer-toolbar-scroll-help" className="viewer-toolbar-help">
                Verktygsfältet kan rullas horisontellt när alla val inte ryms.
              </span>
              <div
                className="viewer-toolbar-controls"
                tabIndex={0}
                aria-label="Visningskontroller, horisontellt rullningsbara"
                aria-describedby="viewer-toolbar-scroll-help"
              >
                <div className="view-tabs" role="group" aria-label="Vy">
                  {([
                    ["perspective", "3D"],
                    ["front", "Front"],
                    ["side", "Sida"],
                    ["top", "Topp"],
                  ] as const).map(([value, label]) => (
                    <button key={value} type="button" className={viewMode === value ? "active" : ""} aria-pressed={viewMode === value} onClick={() => setViewMode(value)}>{label}</button>
                  ))}
                </div>
                <span className="toolbar-divider" />
                <button type="button" className={`tool-button ${exploded ? "active" : ""}`} aria-pressed={exploded} onClick={() => setExploded((value) => !value)}><PackageCheck aria-hidden="true" size={15} /> Exploderad</button>
                <button type="button" className={`tool-button ${transparent ? "active" : ""}`} aria-pressed={transparent} onClick={() => setTransparent((value) => !value)}>{transparent ? <EyeOff aria-hidden="true" size={15} /> : <Eye aria-hidden="true" size={15} />} Transparent</button>
                <button type="button" className={`tool-button ${isolateSelection ? "active" : ""}`} aria-pressed={isolateSelection} disabled={!effectiveSelectedPartId} onClick={() => setIsolateSelection((value) => !value)}><Focus aria-hidden="true" size={15} /> Isolera</button>
                <button type="button" className="tool-button" onClick={() => setCameraResetNonce((value) => value + 1)}><Maximize2 aria-hidden="true" size={15} /> Anpassa vy</button>
                {partCustomization ? <button type="button" className="tool-button part-reset-tool" onClick={resetAllPartCustomizations}><Undo2 aria-hidden="true" size={15} /> Återställ deländringar</button> : null}
              </div>
              <div className="viewer-toolbar-spacer" />
              <button
                type="button"
                className={`design-health-pill status-${design.status.toLowerCase()}`}
                title={designHealthLabel}
                aria-label={design.status === "PASS" ? designHealthLabel : `Visa byggbarhetskontroll: ${designHealthLabel}`}
                onClick={() => {
                  changeSelectedPart(undefined);
                  changeWorkspaceStage("check");
                }}
              >
                <span className="design-health-dot" />{designHealthLabel}
              </button>
              <label className="part-selector">
                <span>Välj del</span>
                <select
                  aria-label="Välj möbeldel att inspektera"
                  title="Klicka i modellen eller välj en del i listan"
                  value={effectiveSelectedPartId ?? ""}
                  onChange={(event) => changeSelectedPart(event.target.value || undefined)}
                >
                  <option value="">Välj del</option>
                  {design.parts.map((part) => <option key={part.part_id} value={part.part_id}>{part.name}</option>)}
                </select>
              </label>
            </div>
            {draftConflict && draftConflict.projectId === projectId ? (
              <DraftConflictBanner
                message={draftConflict.message}
                busy={draftConflictBusy}
                copied={draftConflictCopied}
                onCopy={copyConflictedDraft}
                onReload={reloadLatestDraft}
              />
            ) : null}
            <CanvasStateBanners
              apiMessage={displayedApiMessage}
              apiState={displayedApiState === "offline" || displayedApiState === "error" ? displayedApiState : undefined}
              authError={authError}
              canRetryApi={Boolean(serverAvailable && hydrated && projectId && !hydrationBlocker)}
              onRetryApi={retryServerPreview}
              projectError={projectError && !projectCreateOpen ? projectError : undefined}
            />
            {integrityEvaluation && (partCustomization || partEditNotice) ? (
              <div className={`structural-integrity-alert status-${integrityEvaluation.status.toLowerCase()}`} role="alert">
                <AlertTriangle aria-hidden="true" size={19} />
                <span>
                  <strong>{integrityEvaluation.status === "BLOCK" ? "Konstruktionen behöver åtgärdas" : integrityEvaluation.rule_id === "PART-CUSTOM-001" ? "Konstruktionsgranskning krävs" : "Kontrollera konstruktionen"}</strong>
                  <small>{partEditNotice ? `${partEditNotice} ` : ""}{integrityEvaluation.summary}</small>
                </span>
                <button type="button" disabled={past.length === 0} onClick={undo}>Ångra senaste</button>
              </div>
            ) : partEditNotice ? (
              <div className="structural-integrity-alert status-pass" role="status">
                <span><strong>Modellen byggdes om</strong><small>{partEditNotice}</small></span>
              </div>
            ) : null}
            <div className={`${styles.builderStage} ${studioStyles.builder} ${workspaceStage !== "studio" || !workspacePanels.componentLibraryOpen ? styles.builderStageLibraryClosed : ""}`}>
              {workspaceStage === "studio" && workspacePanels.componentLibraryOpen ? (
                <ComponentPalette
                  spec={spec}
                  onInsert={(kind) => applySemanticDrop(defaultSemanticDropRequest(spec, kind))}
                  onDragStartKind={setSemanticDragKind}
                  onDragEnd={() => setSemanticDragKind(undefined)}
                />
              ) : null}
              <div className={`${styles.canvasStage} ${studioStyles.canvas}`}>
                <div className={studioStyles.modelLabel} data-testid="current-design-label">
                  <strong>Aktuell konstruktion</strong>
                  <small>{spec.width_mm} × {spec.height_mm} × {Math.max(spec.depth_mm, spec.base_cabinet_depth_mm)} mm</small>
                </div>
                <FurnitureViewer
                  parts={design.parts}
                  designSize={{ widthMm: spec.width_mm, heightMm: spec.height_mm, depthMm: spec.depth_mm }}
                  selectedPartId={effectiveSelectedPartId}
                  viewMode={viewMode}
                  exploded={exploded}
                  transparent={transparent}
                  isolateSelection={isolateSelection}
                  onSelectPart={workspaceStage === "studio" ? changeSelectedPart : () => undefined}
                  resizeEnabled={workspaceStage === "studio" && !exploded}
                  onResizeStart={workspaceStage === "studio" ? beginDimensionResize : undefined}
                  onResize={workspaceStage === "studio" ? resizeDimension : undefined}
                  onResizeEnd={workspaceStage === "studio" ? finishDimensionResize : undefined}
                  onPartMoveStart={workspaceStage === "studio" ? beginPartMove : undefined}
                  onPartMove={workspaceStage === "studio" ? movePart : undefined}
                  onPartMoveEnd={workspaceStage === "studio" ? finishPartMove : undefined}
                  onPartHorizontalMoveStart={workspaceStage === "studio" ? beginPartMove : undefined}
                  onPartHorizontalMove={workspaceStage === "studio" ? movePartHorizontally : undefined}
                  onPartHorizontalMoveEnd={workspaceStage === "studio" ? finishPartMove : undefined}
                  presentation={workspaceStage === "check" ? "validation" : workspaceStage === "build" ? "production" : "studio"}
                  cameraResetNonce={cameraResetNonce}
                  semanticDropEnabled={workspaceStage === "studio"}
                  semanticSpec={spec}
                  semanticDragKind={semanticDragKind}
                  onSemanticDrop={applySemanticDrop}
                  comparisonPreview={viewerComparisonPreview}
                />
                {semanticNotice ? (
                  <div className={`${styles.semanticNotice} ${semanticNotice.error ? styles.semanticNoticeError : ""}`} role={semanticNotice.error ? "alert" : "status"}>
                    <span className={styles.noticeIcon}>{semanticNotice.error ? <AlertTriangle aria-hidden="true" size={18} /> : <Check aria-hidden="true" size={18} />}</span>
                    <p><strong>{semanticNotice.title}</strong><span>{semanticNotice.detail}</span></p>
                    <button type="button" aria-label="Stäng placeringsmeddelande" onClick={() => setSemanticNotice(undefined)}><X aria-hidden="true" size={14} /></button>
                  </div>
                ) : null}
              </div>
            </div>
            <div className="viewer-status-strip">
              <span><span className="engine-dot" /> {referenceImageDesign ? `Bildkoncept · ${spec.reference_image_import?.file_name ?? "referens"}` : partCustomization ? `Delkoncept · ${spec.removed_part_ids.length + Object.keys(spec.part_overrides).length} ändringar` : customInterior ? "Anpassad serverlayout · konstruktionsscreenad" : "Dynamisk modell · ändringar visas direkt"}</span>
              <span>{workspaceStage === "studio" ? "Dra i måtthandtag, hyllor och avdelare" : "X bredd · Y djup · Z höjd"}</span>
              <span>{spec.divider_count + 1} bärande fack · {spec.shelf_count} hyllrader</span>
            </div>
            {changeDiff.length > 0 ? (
              <div className="change-diff" role="status">
                <div className="diff-icon"><GitBranch aria-hidden="true" size={16} /></div>
                <div>
                  <strong>{changeDiff.length} {changeDiff.length === 1 ? "ändring" : "ändringar"} i utkastet</strong>
                  <p>{changeDiff.map((diff) => `${String(diff.field)}: ${String(diff.before)} → ${String(diff.after)}`).join(" · ")}</p>
                </div>
                <button type="button" aria-label="Stäng ändringsöversikt" onClick={() => setChangeDiff([])}><X aria-hidden="true" size={15} /></button>
              </div>
            ) : null}
          </section>

          <aside className={`right-rail configurator-rail ${studioStyles.contextRail} ${!workspacePanels.contextPanelOpen ? "cb-panel-collapsed" : ""}`}>
            {!workspacePanels.contextPanelOpen ? (
              <button
                type="button"
                className="cb-panel-reopen"
                onClick={() => setWorkspacePanels((panels) => ({ ...panels, contextPanelOpen: true }))}
              >
                Visa redigering
              </button>
            ) : null}
            {workspacePanels.contextPanelOpen && workspaceStage === "studio" ? (
              <div className={studioStyles.studioContext}>
                <div className={studioStyles.contextSwitcher} role="group" aria-label="Redigeringskontext">
                  <button
                    type="button"
                    aria-pressed={!selectedPart || studioInspectorContext === "furniture"}
                    onClick={() => setStudioInspectorContext("furniture")}
                  >
                    <strong>Möbel</strong>
                    <small>Hela konstruktionen</small>
                  </button>
                  <button
                    type="button"
                    aria-pressed={Boolean(selectedPart && studioInspectorContext === "part")}
                    disabled={!selectedPart}
                    onClick={() => setStudioInspectorContext("part")}
                  >
                    <strong>Vald del</strong>
                    <small>{selectedPart?.name ?? "Ingen del vald"}</small>
                  </button>
                  {selectedPart ? (
                    <button
                      type="button"
                      className={studioStyles.deselectPart}
                      aria-label={`Avmarkera ${selectedPart.name}`}
                      onClick={() => changeSelectedPart(undefined)}
                    >
                      <X aria-hidden="true" size={15} />
                    </button>
                  ) : null}
                </div>
                <div className={studioStyles.contextPanelViewport}>
                  {selectedPart && studioInspectorContext === "part" ? (
                    <SelectedPartInspector
                      part={selectedPart}
                      spec={spec}
                      override={spec.part_overrides[selectedPart.part_id]}
                      onChange={(patch) => updateSelectedPart(selectedPart.part_id, patch)}
                      onShelfOpeningChange={updateShelfOpening}
                      onRemove={() => removeSelectedPart(selectedPart.part_id)}
                      onReset={() => resetSelectedPart(selectedPart.part_id)}
                      onClose={() => changeSelectedPart(undefined)}
                    />
                  ) : (
                    <StudioInspector
                      spec={spec}
                      status={design.status}
                      partCount={design.parts.length}
                      onChange={updateSpec}
                      onOpenExplore={() => changeWorkspaceStage("explore")}
                      onOpenCheck={() => changeWorkspaceStage("check")}
                    />
                  )}
                </div>
              </div>
            ) : workspaceStage === "check" ? (
              <div className={studioStyles.checkRail}>
                <ValidationPanel
                  evaluations={design.rule_evaluations}
                  status={design.status}
                  spec={spec}
                  activePreview={activeValidationFixPreview}
                  onRequestPreview={requestValidationFixPreview}
                  onCancelPreview={cancelValidationFixPreview}
                  onConfirmPreview={confirmValidationFixPreview}
                  onSelectPart={(partId) => {
                    changeSelectedPart(partId);
                    setIsolateSelection(true);
                    setViewMode("front");
                  }}
                  onNavigateToStep={() => changeWorkspaceStage("studio")}
                  onOpenProduction={() => changeWorkspaceStage("build")}
                />
              </div>
            ) : workspaceStage === "build" ? (
              <div className={studioStyles.buildRail}>
                <ProductionDrawer
                  open
                  presentation="embedded"
                  spec={spec}
                  design={design}
                  template={selectedTemplate}
                  onClose={() => changeWorkspaceStage("studio")}
                  onOpenTemplates={() => changeWorkspaceStage("explore")}
                  onApplyDesignChange={updateSpec}
                  onRequestServerPreviewRetry={retryServerPreview}
                  projectName={activeProject?.name ?? DEFAULT_PROJECT_NAME}
                  projectId={projectId}
                  principal={principal}
                />
              </div>
            ) : null}
          </aside>
        </div>
        )}
        </>
        )}
      </main>
      <ReferenceImageImporter
        open={referenceImporterOpen}
        onInspect={inspectReferenceImage}
        onClose={() => {
          setReferenceImporterOpen(false);
          if (!workspaceSelected) changeWorkspaceStage("explore");
        }}
        onApply={applyReferenceImage}
      />
    </div>
  );
}
