import type { DesignVersionRead, JobRead, ReleaseRead } from "./api-client";
import type { WorkspaceIdentity } from "./workspace-draft-storage";

export const PRODUCTION_SESSION_PREFIX = "custombuild:production:v2";
const LEGACY_PRODUCTION_PREFIXES = [
  "custombuild:production:project:",
  "custombuild:production:anonymous:",
];

export interface ProductionSessionSnapshot {
  schemaVersion: 1;
  version?: DesignVersionRead;
  job?: JobRead;
  release?: ReleaseRead;
  designApproved: boolean;
  releaseNumber: string;
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

function identityKey(identity: WorkspaceIdentity): string {
  if (!identity) return "anonymous";
  return `organization:${segment(identity.organization_id)}:user:${segment(identity.user_id)}`;
}

export function productionSessionKey(
  identity: WorkspaceIdentity,
  projectId: string | undefined,
  designId: string,
): string {
  const projectScope = projectId ? `project:${segment(projectId)}` : "project:anonymous";
  return `${PRODUCTION_SESSION_PREFIX}:${identityKey(identity)}:${projectScope}:design:${segment(designId)}`;
}

function optionalObject(value: unknown): value is Record<string, unknown> | undefined {
  return value === undefined || (typeof value === "object" && value !== null && !Array.isArray(value));
}

export function readProductionSession(
  storage: Pick<Storage, "getItem" | "removeItem">,
  key: string,
): ProductionSessionSnapshot | undefined {
  try {
    const raw = storage.getItem(key);
    if (!raw) return undefined;
    const parsed = JSON.parse(raw) as Partial<ProductionSessionSnapshot>;
    if (
      parsed.schemaVersion !== 1
      || typeof parsed.designApproved !== "boolean"
      || typeof parsed.releaseNumber !== "string"
      || !optionalObject(parsed.version)
      || !optionalObject(parsed.job)
      || !optionalObject(parsed.release)
    ) {
      storage.removeItem(key);
      return undefined;
    }
    return parsed as ProductionSessionSnapshot;
  } catch {
    storage.removeItem(key);
    return undefined;
  }
}

export function writeProductionSession(
  storage: Pick<Storage, "setItem">,
  key: string,
  snapshot: Omit<ProductionSessionSnapshot, "schemaVersion">,
): void {
  // Persist only resumable status. Signed artifact URLs are intentionally kept
  // in React memory and must be refreshed from the authorized API each session.
  const persisted: ProductionSessionSnapshot = {
    schemaVersion: 1,
    version: snapshot.version,
    job: snapshot.job,
    release: snapshot.release,
    designApproved: snapshot.designApproved,
    releaseNumber: snapshot.releaseNumber,
  };
  storage.setItem(key, JSON.stringify(persisted));
}

export function clearProductionSession(
  storage: Pick<Storage, "length" | "key" | "removeItem">,
  identity: WorkspaceIdentity,
): void {
  const prefix = `${PRODUCTION_SESSION_PREFIX}:${identityKey(identity)}:`;
  const matchingKeys: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key?.startsWith(prefix)) matchingKeys.push(key);
  }
  matchingKeys.forEach((key) => storage.removeItem(key));
}

export function clearLegacyProductionStorage(
  storage: Pick<Storage, "length" | "key" | "removeItem">,
): void {
  const matchingKeys: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key && LEGACY_PRODUCTION_PREFIXES.some((prefix) => key.startsWith(prefix))) {
      matchingKeys.push(key);
    }
  }
  matchingKeys.forEach((key) => storage.removeItem(key));
}
