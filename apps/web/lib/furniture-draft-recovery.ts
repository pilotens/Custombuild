import type { CurrentPrincipal } from "./api-client";
import type { FurnitureWorkspace } from "./furniture-workspace";

export interface FurnitureProfileDraft {
  proposed: FurnitureWorkspace;
  inputDrafts: Record<string, string>;
  inputErrors: Record<string, string>;
}

/** An editing snapshot only: never a saved revision, preview or approval. */
export interface FurnitureDraftRecovery {
  version: 1;
  workspace: FurnitureWorkspace;
  projectId?: string;
  revision: number;
  name: string;
  updatedAt: string;
  inputDrafts?: Record<string, string>;
  inputErrors?: Record<string, string>;
  profile?: FurnitureProfileDraft;
}

export function furnitureDraftRecoveryKey(apiUrl: string | undefined,
  principal: Pick<CurrentPrincipal, "organization_id" | "user_id">): string {
  return `custombuild:furniture-recovery:v1:${JSON.stringify([apiUrl ?? "", principal.organization_id, principal.user_id])}`;
}

function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function textMap(value: unknown): value is Record<string, string> | undefined {
  return value === undefined || (record(value) && Object.keys(value).length <= 100
    && Object.entries(value).every(([key, text]) => key.length <= 180
      && typeof text === "string" && text.length <= 10_000));
}

function workspaceEnvelope(value: unknown): value is FurnitureWorkspace {
  return record(value) && value.schema_version === "custombuild.furniture-workspace.v1"
    && record(value.design) && record(value.design.intent);
}

/** Validate the bounded envelope locally; the API validates both designs on restore. */
export function parseFurnitureDraftRecovery(raw: string): FurnitureDraftRecovery {
  if (new TextEncoder().encode(raw).length > 256 * 1024) throw new Error("Återställningsfilen får vara högst 256 kB.");
  const value: unknown = JSON.parse(raw);
  if (!record(value) || value.version !== 1 || !workspaceEnvelope(value.workspace)
    || !Number.isSafeInteger(value.revision) || (value.revision as number) < 0
    || typeof value.name !== "string" || !value.name.trim() || value.name.length > 180
    || typeof value.updatedAt !== "string"
    || (value.projectId !== undefined && (typeof value.projectId !== "string" || !value.projectId || value.projectId.length > 80))
    || !textMap(value.inputDrafts) || !textMap(value.inputErrors)
    || (value.profile !== undefined && (!record(value.profile) || !workspaceEnvelope(value.profile.proposed)
      || !textMap(value.profile.inputDrafts) || !textMap(value.profile.inputErrors)))) {
    throw new Error("Återställningsfilen har ett ogiltigt format. Hämta filen innan du tar bort kopian.");
  }
  return value as unknown as FurnitureDraftRecovery;
}

/**
 * Every recovery mutation uses the same origin-wide Web Lock. The expected
 * value is read and advanced while holding the lock, never before/after await.
 */
export async function replaceFurnitureRecovery(
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">,
  key: string,
  expected: { current: string | null },
  next: string | null,
  isCurrent: () => boolean,
  signal?: AbortSignal,
): Promise<boolean> {
  if (typeof navigator === "undefined" || typeof navigator.locks?.request !== "function") {
    throw new Error("Webbläsaren stöder inte säker samordning av återställningskopior. Hämta en återställningsfil för att behålla ditt arbete.");
  }
  return navigator.locks.request(`custombuild:recovery-write:${key}`, { mode: "exclusive", signal }, () => {
    if (!isCurrent() || signal?.aborted) return false;
    if (storage.getItem(key) !== expected.current) {
      throw new Error("En annan flik har ändrat återställningskopian. Hämta din återställningsfil innan du fortsätter.");
    }
    if (next === null) storage.removeItem(key); else storage.setItem(key, next);
    expected.current = next;
    return true;
  });
}
