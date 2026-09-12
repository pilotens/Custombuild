import type { CurrentPrincipal } from "./api-client";
import type { DesignSpec } from "./design-types";
import type { FurnitureProductionSource } from "./furniture-workspace";
import type { WorkshopContextDraft, WorkshopContextDraftState } from "../components/workshop-context-editor";
import {
  parseRevisionProductionContext, productionContextFromDesignSpec,
  type RevisionProductionContextSnapshot,
} from "./workshop-production-context";

/** Recovery is caller-declared input only. Never persist approvals or production state. */
export interface FurnitureProductionRecovery {
  version: 1;
  project_id: string;
  furniture_revision: number;
  furniture_design_hash: string;
  source_workspace_sha256: string;
  updated_at: string;
  context: RevisionProductionContextSnapshot;
  editor_draft?: { enabled: true; draft: WorkshopContextDraft };
}

const MAX_RECOVERY_BYTES = 256 * 1024;

function object(value: unknown, keys: string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).length !== keys.length
    || keys.some(key => !Object.hasOwn(value, key))) {
    throw new Error("Formulärkopian har ogiltig struktur.");
  }
  return value as Record<string, unknown>;
}

function texts(record: Record<string, unknown>, keys: string[]): void {
  for (const key of keys) {
    if (typeof record[key] !== "string" || record[key].length > 4096) {
      throw new Error("Formulärkopian innehåller ett ogiltigt eller för långt textfält.");
    }
  }
}

function rows(value: unknown, minimum: number, maximum: number): unknown[] {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) {
    throw new Error("Formulärkopian innehåller fel antal rader.");
  }
  return value;
}

/** Validate only inert UI data. Incomplete and invalid numbers remain exact text. */
function parseEditorRecovery(value: unknown, spec: DesignSpec): NonNullable<FurnitureProductionRecovery["editor_draft"]> {
  const editor = object(value, ["enabled", "draft"]);
  if (editor.enabled !== true) throw new Error("Formulärkopian saknar ett öppet formulär.");
  const draft = object(editor.draft, ["profiles", "registrations"]);
  const roles = spec.back_panel ? ["carcass", "back"] : ["carcass"];
  const profiles = rows(draft.profiles, roles.length, roles.length);
  profiles.forEach((value, index) => {
    const textKeys = ["supplierProfileId", "supplierProfileVersion", "sheetWidthMm", "sheetHeightMm",
      "sheetCount", "trimMarginMm", "kerfMm"];
    const profile = object(value, ["role", ...textKeys, "grainDirection", "allowRotation", "defectZones", "keepOutZones"]);
    if (profile.role !== roles[index] || !["", "X", "Y", "NONE"].includes(profile.grainDirection as string)
      || !["", "true", "false"].includes(profile.allowRotation as string)) {
      throw new Error("Formulärkopians materialroller eller val är ogiltiga.");
    }
    texts(profile, textKeys);
    for (const kind of ["defectZones", "keepOutZones"]) {
      for (const zone of rows(profile[kind], 0, 100)) {
        const keys = ["xMm", "yMm", "widthMm", "heightMm"];
        texts(object(zone, keys), keys);
      }
    }
  });
  for (const value of rows(draft.registrations, 0, 200)) {
    const keys = ["sheetIndex", "fixtureMethodId", "fixtureMethodVersion", "pinDiameterMm", "positionToleranceMm"];
    const registration = object(value, ["stockRole", ...keys, "pins"]);
    if (!["", ...roles].includes(registration.stockRole as string)) {
      throw new Error("Formulärkopians registrering hör till en annan materialroll.");
    }
    texts(registration, keys);
    for (const pin of rows(registration.pins, 2, 16)) texts(object(pin, ["xMm", "yMm"]), ["xMm", "yMm"]);
  }
  return { enabled: true, draft: editor.draft as WorkshopContextDraft };
}

export function serializeFurnitureProductionRecovery(recovery: FurnitureProductionRecovery): string {
  const raw = JSON.stringify(recovery);
  if (new TextEncoder().encode(raw).length > MAX_RECOVERY_BYTES) throw new Error("Beredningskopian är för stor.");
  return raw;
}

export function furnitureProductionRecoveryKey(
  apiUrl: string | undefined, principal: CurrentPrincipal, source: FurnitureProductionSource,
): string {
  return `custombuild:furniture-production-recovery:v1:${JSON.stringify([
    apiUrl ?? "", principal.organization_id, principal.user_id,
    source.workspace.design.design_id, source.workspace.design.revision,
    source.furniture_design_hash, source.workspace_sha256,
  ])}`;
}

export function furnitureProductionRecovery(
  spec: DesignSpec, source: FurnitureProductionSource, editor?: WorkshopContextDraftState,
): FurnitureProductionRecovery {
  return {
    version: 1, project_id: source.workspace.design.design_id,
    furniture_revision: source.workspace.design.revision,
    furniture_design_hash: source.furniture_design_hash,
    source_workspace_sha256: source.workspace_sha256,
    updated_at: new Date().toISOString(), context: productionContextFromDesignSpec(spec),
    ...(editor?.enabled && (editor.dirty || !editor.valid)
      ? { editor_draft: parseEditorRecovery({ enabled: true, draft: editor.draft }, spec) } : {}),
  };
}

export function restoreFurnitureProductionRecovery(
  raw: string, source: FurnitureProductionSource, current: DesignSpec,
): DesignSpec {
  return restoreFurnitureProductionState(raw, source, current).spec;
}

export function restoreFurnitureProductionState(
  raw: string, source: FurnitureProductionSource, current: DesignSpec,
): { spec: DesignSpec; editorDraft?: WorkshopContextDraft } {
  if (new TextEncoder().encode(raw).length > MAX_RECOVERY_BYTES) throw new Error("Beredningskopian är för stor.");
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Beredningskopian har ogiltigt format.");
  }
  const record = value as Record<string, unknown>;
  const keys = ["version", "project_id", "furniture_revision", "furniture_design_hash",
    "source_workspace_sha256", "updated_at", "context", ...(Object.hasOwn(record, "editor_draft") ? ["editor_draft"] : [])];
  if (Object.keys(record).length !== keys.length || keys.some(key => !Object.hasOwn(record, key))
    || record.version !== 1 || typeof record.updated_at !== "string"
    || record.updated_at.length > 40 || !Number.isFinite(Date.parse(record.updated_at))
    || record.project_id !== source.workspace.design.design_id
    || record.furniture_revision !== source.workspace.design.revision
    || record.furniture_design_hash !== source.furniture_design_hash
    || record.source_workspace_sha256 !== source.workspace_sha256) {
    throw new Error("Beredningskopian matchar inte exakt den kontrollerade möbelrevisionen.");
  }
  const { stock_profiles, two_sided_registrations, ...context } = parseRevisionProductionContext(record.context);
  const spec = {
    ...current, ...context,
    workshop_context: stock_profiles ? { stock_profiles, two_sided_registrations } : undefined,
  };
  // Validate the declarations against fresh, server-derived material and geometry.
  productionContextFromDesignSpec(spec);
  const editor = record.editor_draft === undefined ? undefined : parseEditorRecovery(record.editor_draft, spec);
  return { spec, ...(editor ? { editorDraft: editor.draft } : {}) };
}
