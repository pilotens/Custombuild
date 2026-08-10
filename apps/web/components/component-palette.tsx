"use client";

import type { DragEvent } from "react";
import { BetweenHorizontalStart, Layers3, PanelBottom, PanelTop } from "lucide-react";
import {
  SEMANTIC_COMPONENTS,
  writeSemanticDragPayload,
  type SemanticComponentKind,
} from "@/lib/semantic-design";
import styles from "./semantic-editor.module.css";

const icons = {
  shelf_row: BetweenHorizontalStart,
  divider: PanelTop,
  back_panel: Layers3,
  plinth: PanelBottom,
} satisfies Record<SemanticComponentKind, typeof BetweenHorizontalStart>;

interface ComponentPaletteProps {
  onInsert: (kind: SemanticComponentKind) => void;
  onDragStartKind: (kind: SemanticComponentKind) => void;
  onDragEnd: () => void;
}

export function ComponentPalette({ onInsert, onDragStartKind, onDragEnd }: ComponentPaletteProps) {
  return (
    <section className={styles.palette} aria-labelledby="component-palette-heading">
      <div className={styles.paletteHeading}>
        <p>Byggdelar</p>
        <h2 id="component-palette-heading">Dra in i modellen</h2>
      </div>
      <div className={styles.componentList}>
        {SEMANTIC_COMPONENTS.map((component) => {
          const Icon = icons[component.kind];
          return (
            <button
              key={component.kind}
              type="button"
              className={styles.componentButton}
              draggable
              onDragStart={(event: DragEvent<HTMLButtonElement>) => {
                writeSemanticDragPayload(event.dataTransfer, component.kind);
                onDragStartKind(component.kind);
              }}
              onDragEnd={onDragEnd}
              onClick={() => onInsert(component.kind)}
              title={`${component.description} ${component.limit}`}
            >
              <span className={styles.componentIcon}><Icon aria-hidden="true" size={17} /></span>
              <span>
                <strong>{component.label}</strong>
                <small>{component.limit}</small>
              </span>
            </button>
          );
        })}
      </div>
      <p className={styles.paletteFootnote}>
        Delarna snappar semantiskt. Exakta mått, fogar och CNC-operationer skapas först av den deterministiska motorn.
      </p>
    </section>
  );
}
