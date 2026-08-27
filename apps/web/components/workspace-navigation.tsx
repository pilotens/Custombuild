"use client";

import { Compass, FileArchive, LayoutGrid, ShieldCheck } from "lucide-react";
import type {
  LegacyWorkspaceEditorMode,
  LegacyWorkspaceStep,
  WorkspaceMode as WorkspaceProductMode,
} from "@/lib/workspace-ui-state";
import { workspaceModeFromLegacyStep } from "@/lib/workspace-ui-state";
import styles from "./workspace-navigation.module.css";

export type WorkspaceStage = WorkspaceProductMode;
/** @deprecated Use WorkspaceStage. */
export type WorkspaceMode = LegacyWorkspaceEditorMode;

export interface WorkspaceStageDefinition {
  id: WorkspaceStage;
  label: string;
  description: string;
  icon: typeof Compass;
}

export const WORKSPACE_MODES: readonly WorkspaceStageDefinition[] = [
  { id: "explore", label: "Utforska", description: "Välj startpunkt", icon: Compass },
  { id: "studio", label: "Studio", description: "Forma din möbel", icon: LayoutGrid },
  { id: "check", label: "Kontroll", description: "Säkerställ konstruktionen", icon: ShieldCheck },
  { id: "build", label: "Underlag", description: "Designgranska och exportera", icon: FileArchive },
] as const;

/** @deprecated Use WORKSPACE_MODES. */
export const WORKSPACE_STAGES = WORKSPACE_MODES;

export interface WorkspaceNavigationProps {
  current: WorkspaceStage | LegacyWorkspaceStep;
  onStageChange: (stage: WorkspaceStage) => void;
  /** Authenticated empty projects stay in Explore until a start point is selected. */
  startPointSelected?: boolean;
  /** @deprecated The Studio is now one direct-manipulation interface. */
  mode?: LegacyWorkspaceEditorMode;
  /** @deprecated Retained only while callers migrate; no control is rendered. */
  onModeChange?: (mode: LegacyWorkspaceEditorMode) => void;
  /** @deprecated Panel visibility belongs to the contextual Studio controls. */
  componentLibraryOpen?: boolean;
  /** @deprecated Panel visibility belongs to the contextual Studio controls. */
  contextPanelOpen?: boolean;
  /** @deprecated Panel visibility belongs to the contextual Studio controls. */
  onComponentLibraryOpenChange?: (open: boolean) => void;
  /** @deprecated Panel visibility belongs to the contextual Studio controls. */
  onContextPanelOpenChange?: (open: boolean) => void;
}

export function WorkspaceNavigation({
  current,
  onStageChange,
  startPointSelected = true,
}: WorkspaceNavigationProps) {
  const activeMode = workspaceModeFromLegacyStep(current);

  return (
    <div className={`cb-workspace-navigation ${styles.navigation}`}>
      <nav className={styles.modes} aria-label="Produktlägen">
        <ul>
          {WORKSPACE_MODES.map((mode) => {
            const Icon = mode.icon;
            const active = activeMode === mode.id;
            const disabled = !startPointSelected && mode.id !== "explore";
            return (
              <li key={mode.id}>
                <button
                  type="button"
                  aria-current={active ? "page" : undefined}
                  className={active ? styles.active : undefined}
                  disabled={disabled}
                  title={disabled ? "Välj en startpunkt i Utforska först." : undefined}
                  onClick={() => onStageChange(mode.id)}
                >
                  <Icon aria-hidden="true" size={17} strokeWidth={1.7} />
                  <span>
                    <strong>{mode.label}</strong>
                    <small>{mode.description}</small>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </nav>
    </div>
  );
}
