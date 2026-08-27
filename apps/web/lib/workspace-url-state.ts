import { isWorkspaceMode, type WorkspaceMode } from "./workspace-ui-state";

export const WORKSPACE_PROJECT_PARAM = "project";
export const WORKSPACE_MODE_PARAM = "mode";

const PROJECT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;

export interface ParsedWorkspaceUrl {
  projectId?: string;
  mode?: WorkspaceMode;
  projectParamPresent: boolean;
  modeParamPresent: boolean;
}

export interface WorkspaceUrlTarget {
  projectId?: string;
  mode: WorkspaceMode;
}

/**
 * Reads only URL-owned workspace context. A duplicate, empty or malformed
 * owned parameter is treated as explicit but invalid so callers can fall back
 * safely and then canonicalize the address.
 */
export function parseWorkspaceUrl(search: string): ParsedWorkspaceUrl {
  const params = new URLSearchParams(search);
  const projectValues = params.getAll(WORKSPACE_PROJECT_PARAM);
  const modeValues = params.getAll(WORKSPACE_MODE_PARAM);
  const projectCandidate = projectValues.length === 1 ? projectValues[0] : undefined;
  const modeCandidate = modeValues.length === 1 ? modeValues[0] : undefined;

  return {
    ...(projectCandidate && PROJECT_ID_PATTERN.test(projectCandidate)
      ? { projectId: projectCandidate }
      : {}),
    ...(isWorkspaceMode(modeCandidate) ? { mode: modeCandidate } : {}),
    projectParamPresent: projectValues.length > 0,
    modeParamPresent: modeValues.length > 0,
  };
}

export function selectWorkspaceProject<T extends { id: string }>(
  availableProjects: readonly T[],
  urlProjectId?: string,
  rememberedProjectId?: string,
): T | undefined {
  return availableProjects.find((project) => project.id === urlProjectId)
    ?? availableProjects.find((project) => project.id === rememberedProjectId)
    ?? availableProjects[0];
}

export function selectWorkspaceMode(
  parsed: Pick<ParsedWorkspaceUrl, "mode">,
  fallback: WorkspaceMode,
): WorkspaceMode {
  return parsed.mode ?? fallback;
}

/**
 * Rewrites only `project` and `mode`, preserving the current path, unrelated
 * query parameters and hash. Project is intentionally absent for anonymous
 * workspaces. Design data, revisions, selection and camera state never enter
 * this boundary.
 */
export function serializeWorkspaceUrl(currentHref: string, target: WorkspaceUrlTarget): string {
  const url = new URL(currentHref);
  url.searchParams.delete(WORKSPACE_PROJECT_PARAM);
  url.searchParams.delete(WORKSPACE_MODE_PARAM);
  if (target.projectId) url.searchParams.set(WORKSPACE_PROJECT_PARAM, target.projectId);
  url.searchParams.set(WORKSPACE_MODE_PARAM, target.mode);
  return `${url.pathname}${url.search}${url.hash}`;
}
