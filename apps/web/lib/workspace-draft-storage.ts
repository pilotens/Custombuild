import type { CurrentPrincipal, ProjectRead } from "./api-client";
import type { DesignSpec } from "./design-types";
import { isFurniturePlanningBrief, type FurniturePlanningBrief } from "./furniture-planning";
import {
  sanitizeWorkspaceUiState,
  type WorkspaceUiState,
} from "./workspace-ui-state";
import {
  localDraftPayloadSizeIsAllowed,
  parseLocalDesignSpec,
} from "./workspace-design-envelope";

const STORAGE_PREFIX = "custombuild:workspace:v3";
const LEGACY_STORAGE_PREFIX = "custombuild:workspace:v2";
export const ANONYMOUS_PROJECT_ID = "local-draft";

export interface WorkspaceDraftSnapshot {
  version: 3;
  spec: DesignSpec;
  templateId: string;
  workspaceSelected: boolean;
  planningBrief?: FurniturePlanningBrief;
  uiState: WorkspaceUiState;
  updatedAt: string;
}

export type WorkspaceDraftWriteInput = Omit<
  WorkspaceDraftSnapshot,
  "version" | "updatedAt" | "uiState"
> & { uiState?: WorkspaceUiState };

export interface StoredProjectSelection {
  id: string;
  name: string;
}

export type WorkspaceIdentity = Pick<CurrentPrincipal, "organization_id" | "user_id"> | undefined;

function segment(value: string): string {
  return encodeURIComponent(value);
}

export function workspaceIdentityKey(identity: WorkspaceIdentity): string {
  if (!identity) return "anonymous";
  return `organization:${segment(identity.organization_id)}:user:${segment(identity.user_id)}`;
}

export function workspaceDraftKey(identity: WorkspaceIdentity, projectId: string): string {
  return `${STORAGE_PREFIX}:${workspaceIdentityKey(identity)}:project:${segment(projectId)}:draft`;
}

export function legacyWorkspaceDraftKey(identity: WorkspaceIdentity, projectId: string): string {
  return `${LEGACY_STORAGE_PREFIX}:${workspaceIdentityKey(identity)}:project:${segment(projectId)}:draft`;
}

export function projectSelectionKey(identity: Exclude<WorkspaceIdentity, undefined>): string {
  return `${STORAGE_PREFIX}:${workspaceIdentityKey(identity)}:selected-project`;
}

function legacyProjectSelectionKey(identity: Exclude<WorkspaceIdentity, undefined>): string {
  return `${LEGACY_STORAGE_PREFIX}:${workspaceIdentityKey(identity)}:selected-project`;
}

function parseWorkspaceDraft(raw: string): WorkspaceDraftSnapshot | undefined {
  if (!localDraftPayloadSizeIsAllowed(raw)) return undefined;
  const parsed = JSON.parse(raw) as Record<string, unknown>;
  if (
    (parsed.version !== 2 && parsed.version !== 3)
    || !parsed.spec
    || typeof parsed.spec !== "object"
    || Array.isArray(parsed.spec)
    || typeof parsed.templateId !== "string"
    || !parsed.templateId
    || typeof parsed.workspaceSelected !== "boolean"
    || (parsed.planningBrief !== undefined && !isFurniturePlanningBrief(parsed.planningBrief))
    || typeof parsed.updatedAt !== "string"
  ) return undefined;

  return {
    version: 3,
    spec: parseLocalDesignSpec(parsed.spec),
    templateId: parsed.templateId,
    workspaceSelected: parsed.workspaceSelected,
    ...(parsed.planningBrief ? { planningBrief: parsed.planningBrief } : {}),
    uiState: sanitizeWorkspaceUiState(parsed.version === 3 ? parsed.uiState : undefined),
    updatedAt: parsed.updatedAt,
  };
}

export function readWorkspaceDraft(
  storage: Pick<Storage, "getItem">,
  identity: WorkspaceIdentity,
  projectId: string,
): WorkspaceDraftSnapshot | undefined {
  const keys = [workspaceDraftKey(identity, projectId), legacyWorkspaceDraftKey(identity, projectId)];
  for (const key of keys) {
    try {
      const raw = storage.getItem(key);
      if (!raw) continue;
      const parsed = parseWorkspaceDraft(raw);
      if (parsed) return parsed;
    } catch {
      // Preserve the raw entry for explicit recovery or support inspection. A
      // separate valid legacy entry may still be used without mutating either.
    }
  }
  return undefined;
}

export function writeWorkspaceDraft(
  storage: Pick<Storage, "getItem" | "setItem">,
  identity: WorkspaceIdentity,
  projectId: string,
  snapshot: WorkspaceDraftWriteInput,
): void {
  persistWorkspaceDraft(storage, identity, projectId, snapshot, false);
}

function persistWorkspaceDraft(
  storage: Pick<Storage, "getItem" | "setItem">,
  identity: WorkspaceIdentity,
  projectId: string,
  snapshot: WorkspaceDraftWriteInput,
  allowQuarantineRecovery: boolean,
): void {
  const boundedSpec = parseLocalDesignSpec(snapshot.spec);
  const key = workspaceDraftKey(identity, projectId);
  const existingRaw = storage.getItem(key);
  if (existingRaw !== null && !allowQuarantineRecovery) {
    let quarantined = false;
    try {
      quarantined = !parseWorkspaceDraft(existingRaw);
    } catch {
      quarantined = true;
    }
    if (quarantined) {
      throw new Error(
        "Det befintliga lokala v3-utkastet är satt i karantän och kräver explicit återställning.",
      );
    }
  }
  storage.setItem(key, JSON.stringify({
    ...snapshot,
    spec: boundedSpec,
    version: 3,
    uiState: sanitizeWorkspaceUiState(snapshot.uiState),
    updatedAt: new Date().toISOString(),
  } satisfies WorkspaceDraftSnapshot));
}

/** Explicit recovery boundary; ordinary autosave must never call this function. */
export function replaceQuarantinedWorkspaceDraft(
  storage: Pick<Storage, "getItem" | "setItem">,
  identity: WorkspaceIdentity,
  projectId: string,
  snapshot: WorkspaceDraftWriteInput,
): void {
  persistWorkspaceDraft(storage, identity, projectId, snapshot, true);
}

export function readSelectedProject(
  storage: Pick<Storage, "getItem" | "removeItem">,
  identity: Exclude<WorkspaceIdentity, undefined>,
): StoredProjectSelection | undefined {
  const keys = [projectSelectionKey(identity), legacyProjectSelectionKey(identity)];
  for (const key of keys) {
    try {
      const raw = storage.getItem(key);
      if (!raw) continue;
      const parsed = JSON.parse(raw) as Partial<StoredProjectSelection>;
      if (typeof parsed.id === "string" && parsed.id && typeof parsed.name === "string" && parsed.name) {
        return parsed as StoredProjectSelection;
      }
      storage.removeItem(key);
    } catch {
      storage.removeItem(key);
    }
  }
  return undefined;
}

export function writeSelectedProject(
  storage: Pick<Storage, "setItem">,
  identity: Exclude<WorkspaceIdentity, undefined>,
  project: Pick<ProjectRead, "id" | "name">,
): void {
  storage.setItem(projectSelectionKey(identity), JSON.stringify({ id: project.id, name: project.name }));
}
