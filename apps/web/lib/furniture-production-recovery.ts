import type { CurrentPrincipal } from "./api-client";
import type { DesignSpec } from "./design-types";
import type { FurnitureProductionSource } from "./furniture-workspace";
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
  spec: DesignSpec, source: FurnitureProductionSource,
): FurnitureProductionRecovery {
  return {
    version: 1, project_id: source.workspace.design.design_id,
    furniture_revision: source.workspace.design.revision,
    furniture_design_hash: source.furniture_design_hash,
    source_workspace_sha256: source.workspace_sha256,
    updated_at: new Date().toISOString(), context: productionContextFromDesignSpec(spec),
  };
}

export function restoreFurnitureProductionRecovery(
  raw: string, source: FurnitureProductionSource, current: DesignSpec,
): DesignSpec {
  if (new TextEncoder().encode(raw).length > 256 * 1024) throw new Error("Beredningskopian är för stor.");
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Beredningskopian har ogiltigt format.");
  }
  const record = value as Record<string, unknown>;
  const keys = ["version", "project_id", "furniture_revision", "furniture_design_hash",
    "source_workspace_sha256", "updated_at", "context"];
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
  return spec;
}
