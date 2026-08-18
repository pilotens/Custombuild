export type WorkspaceMode = "explore" | "studio" | "check" | "build";

/** @deprecated Persisted by the former six-step wizard. Read only for migration. */
export type LegacyWorkspaceStep =
  | "discovery"
  | "constraints"
  | "direction"
  | "composition"
  | "verification"
  | "deliverables";

/** @deprecated The Studio no longer has separate guided and free interfaces. */
export type LegacyWorkspaceEditorMode = "guided" | "free";

/** Product-facing alias retained for callers that used the old step vocabulary. */
export type WorkspaceStep = WorkspaceMode;
/** @deprecated Kept temporarily for source compatibility at integration boundaries. */
export type WorkspaceEditorMode = LegacyWorkspaceEditorMode;
export type WorkspaceViewMode = "perspective" | "front" | "side" | "top";

export interface WorkspacePanelState {
  componentLibraryOpen: boolean;
  contextPanelOpen: boolean;
  advancedPanelOpen: boolean;
}

/**
 * View-only state for the furniture workspace.
 *
 * This type must never be merged into DesignSpec or sent to preview, revision,
 * validation or generation endpoints. It is deliberately persisted beside the
 * local draft so reopening a project can restore the user's working context.
 *
 * Version 2 replaces the former six-step wizard plus guided/free split with one
 * product mode. `sanitizeWorkspaceUiState` remains the migration boundary for
 * every persisted version-1 snapshot.
 */
export interface WorkspaceUiState {
  schemaVersion: 2;
  mode: WorkspaceMode;
  viewMode: WorkspaceViewMode;
  exploded: boolean;
  transparent: boolean;
  isolateSelection: boolean;
  selectedPartId?: string;
  panels: WorkspacePanelState;
}

export const DEFAULT_WORKSPACE_UI_STATE: Readonly<WorkspaceUiState> = Object.freeze({
  schemaVersion: 2,
  mode: "explore",
  viewMode: "perspective",
  exploded: false,
  transparent: false,
  isolateSelection: false,
  panels: Object.freeze({
    componentLibraryOpen: true,
    contextPanelOpen: true,
    advancedPanelOpen: false,
  }),
});

const WORKSPACE_MODES = new Set<WorkspaceMode>(["explore", "studio", "check", "build"]);
const VIEW_MODES = new Set<WorkspaceViewMode>(["perspective", "front", "side", "top"]);
const PART_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;

const LEGACY_STEP_MODE: Readonly<Record<LegacyWorkspaceStep, WorkspaceMode>> = Object.freeze({
  discovery: "explore",
  constraints: "studio",
  direction: "studio",
  composition: "studio",
  verification: "check",
  deliverables: "build",
});

export function isWorkspaceMode(value: unknown): value is WorkspaceMode {
  return typeof value === "string" && WORKSPACE_MODES.has(value as WorkspaceMode);
}

/**
 * Deterministically collapses the old wizard into the four product modes.
 * Unknown input starts safely in Explore.
 */
export function workspaceModeFromLegacyStep(value: unknown): WorkspaceMode {
  if (isWorkspaceMode(value)) return value;
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(LEGACY_STEP_MODE, value)
    ? LEGACY_STEP_MODE[value as LegacyWorkspaceStep]
    : DEFAULT_WORKSPACE_UI_STATE.mode;
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function booleanOr(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

/**
 * Converts untrusted persisted JSON into a complete, bounded UI-state value.
 * Invalid individual fields fall back independently, preserving the rest of a
 * usable snapshot instead of discarding the user's design draft.
 */
export function sanitizeWorkspaceUiState(value: unknown): WorkspaceUiState {
  const candidate = record(value);
  const panels = record(candidate?.panels);
  const selectedPartId = typeof candidate?.selectedPartId === "string"
    && PART_ID_PATTERN.test(candidate.selectedPartId)
    ? candidate.selectedPartId
    : undefined;

  return {
    schemaVersion: 2,
    mode: isWorkspaceMode(candidate?.mode)
      ? candidate.mode
      : workspaceModeFromLegacyStep(candidate?.step),
    viewMode: typeof candidate?.viewMode === "string" && VIEW_MODES.has(candidate.viewMode as WorkspaceViewMode)
      ? candidate.viewMode as WorkspaceViewMode
      : DEFAULT_WORKSPACE_UI_STATE.viewMode,
    exploded: booleanOr(candidate?.exploded, DEFAULT_WORKSPACE_UI_STATE.exploded),
    transparent: booleanOr(candidate?.transparent, DEFAULT_WORKSPACE_UI_STATE.transparent),
    isolateSelection: booleanOr(candidate?.isolateSelection, DEFAULT_WORKSPACE_UI_STATE.isolateSelection),
    ...(selectedPartId ? { selectedPartId } : {}),
    panels: {
      componentLibraryOpen: booleanOr(
        panels?.componentLibraryOpen,
        DEFAULT_WORKSPACE_UI_STATE.panels.componentLibraryOpen,
      ),
      contextPanelOpen: booleanOr(
        panels?.contextPanelOpen,
        DEFAULT_WORKSPACE_UI_STATE.panels.contextPanelOpen,
      ),
      advancedPanelOpen: booleanOr(
        panels?.advancedPanelOpen,
        DEFAULT_WORKSPACE_UI_STATE.panels.advancedPanelOpen,
      ),
    },
  };
}
