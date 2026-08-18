"use client";

import type { DragEvent } from "react";
import {
  BetweenHorizontalStart,
  Archive,
  GripVertical,
  Layers3,
  PanelBottom,
  PanelTop,
  Plus,
} from "lucide-react";
import type { DesignSpec } from "@/lib/design-types";
import {
  SEMANTIC_COMPONENTS,
  writeSemanticDragPayload,
  type SemanticComponentKind,
} from "@/lib/semantic-design";
import styles from "./semantic-editor.module.css";

const icons = {
  shelf_row: BetweenHorizontalStart,
  divider: PanelTop,
  base_cabinet: Archive,
  back_panel: Layers3,
  plinth: PanelBottom,
} satisfies Record<SemanticComponentKind, typeof BetweenHorizontalStart>;

function componentState(spec: DesignSpec, kind: SemanticComponentKind): { count: string; disabled: boolean } {
  if (kind === "shelf_row") return { count: `${spec.shelf_count} st`, disabled: spec.shelf_count >= 40 };
  if (kind === "divider") return { count: `${spec.divider_count} st`, disabled: spec.divider_count >= 16 };
  if (kind === "base_cabinet") return { count: spec.base_cabinet_count > 0 ? `${spec.base_cabinet_count} st` : "", disabled: spec.base_cabinet_count > 0 };
  if (kind === "back_panel") return { count: spec.back_panel ? "Monterat" : "", disabled: spec.back_panel };
  return { count: spec.plinth ? "Monterad" : "", disabled: spec.plinth };
}

interface ComponentPaletteProps {
  spec: DesignSpec;
  onInsert: (kind: SemanticComponentKind) => void;
  onDragStartKind: (kind: SemanticComponentKind) => void;
  onDragEnd: () => void;
}

export function ComponentPalette({ spec, onInsert, onDragStartKind, onDragEnd }: ComponentPaletteProps) {
  return (
    <section className={styles.palette} aria-labelledby="component-palette-heading">
      <header className={styles.paletteHeading}>
        <span className={styles.paletteEyebrow}>Komponentbibliotek</span>
        <h2 id="component-palette-heading">Lägg till delar</h2>
        <p>Välj en del eller dra den till modellen.</p>
      </header>
      <div className={styles.componentGroupHeading}>
        <h3>Stomme och inredning</h3>
        <span>{SEMANTIC_COMPONENTS.length} delar</span>
      </div>
      <div className={styles.componentList}>
        {SEMANTIC_COMPONENTS.map((component) => {
          const Icon = icons[component.kind];
          const state = componentState(spec, component.kind);
          return (
            <button
              key={component.kind}
              type="button"
              className={styles.componentButton}
              draggable={!state.disabled}
              disabled={state.disabled}
              onDragStart={(event: DragEvent<HTMLButtonElement>) => {
                writeSemanticDragPayload(event.dataTransfer, component.kind);
                onDragStartKind(component.kind);
              }}
              onDragEnd={onDragEnd}
              onClick={() => onInsert(component.kind)}
            >
              <span className={styles.componentThumbnail} data-component-kind={component.kind}>
                <Icon aria-hidden="true" size={27} strokeWidth={1.45} />
              </span>
              <span className={styles.componentCopy}>
                <span className={styles.componentMeta}>
                  <span>Parametrisk</span>
                  {state.count ? <em>{state.count}</em> : null}
                </span>
                <strong>{component.label}</strong>
                <small>{component.description}</small>
                <span className={styles.componentAction}>
                  {state.disabled ? "Tillagd" : "Lägg till"}
                </span>
              </span>
              <span className={styles.componentControl} aria-hidden="true">
                {state.disabled ? null : <GripVertical size={14} />}
                <span className={styles.addIcon}><Plus size={15} /></span>
              </span>
            </button>
          );
        })}
      </div>
      <footer className={styles.paletteFootnote}>
        <p><strong>Placeras automatiskt</strong><small>Varje del får ett giltigt läge i möbeln.</small></p>
      </footer>
    </section>
  );
}
