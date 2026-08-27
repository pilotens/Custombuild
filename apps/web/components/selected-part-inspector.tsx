"use client";

import { RotateCcw, SlidersHorizontal, Trash2, X } from "lucide-react";
import { DESIGN_CONSTRAINTS, maximumBaseCabinetHeightMm } from "@/lib/design-constraints";
import type { DesignSpec, PartOverride, ResolvedPart } from "@/lib/design-types";
import { shelfOpeningHeights } from "@/lib/design-engine";
import styles from "./semantic-editor.module.css";

interface SelectedPartInspectorProps {
  part: ResolvedPart;
  spec: DesignSpec;
  override?: PartOverride;
  onChange: (patch: PartOverride) => void;
  onShelfOpeningChange?: (openingIndex: number, valueMm: number) => void;
  onRemove: () => void;
  onReset: () => void;
  onClose: () => void;
}

const PART_KIND_LABELS: Record<ResolvedPart["kind"], string> = {
  side: "Sidostycke",
  top: "Toppskiva",
  bottom: "Bottenskiva",
  shelf: "Hyllplan",
  back: "Bakstycke",
  plinth: "Sockel",
  divider: "Vertikal avdelare",
  base_side: "Underskåpssida",
  base_bottom: "Underskåpsbotten",
  base_top: "Underskåpstopp",
  cabinet_front: "Skåpsfront",
};

function PartNumberField({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
}) {
  const update = (next: number) => {
    if (!Number.isFinite(next)) return;
    onChange(Math.min(max, Math.max(min, next)));
  };

  return (
    <label className={`${styles.inspectorField} part-inspector-field`}>
      <span>{label}</span>
      <span className={`${styles.inspectorNumber} part-inspector-number`}>
        <input
          aria-label={label}
          type="number"
          min={min}
          max={max}
          step={step}
          value={Math.round(value * 10) / 10}
          onChange={(event) => { if (event.target.value !== "") update(Number(event.target.value)); }}
        />
        <small>mm</small>
      </span>
    </label>
  );
}

interface PartFieldBounds {
  min: number;
  max: number;
}

function orderedBounds(min: number, max: number): PartFieldBounds {
  const safeMin = Math.max(1, min);
  return { min: safeMin, max: Math.max(safeMin, max) };
}

function partWidthFieldBounds(part: ResolvedPart, spec: DesignSpec): PartFieldBounds {
  if (part.kind === "side") {
    return { min: DESIGN_CONSTRAINTS.heightMm.minimum, max: DESIGN_CONSTRAINTS.heightMm.maximum };
  }
  if (part.kind === "base_side") {
    const cabinetToPanelOffsetMm = spec.base_cabinet_height_mm - part.width_mm;
    return orderedBounds(
      DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm - cabinetToPanelOffsetMm,
      maximumBaseCabinetHeightMm(spec.height_mm, spec.measured_thickness_mm) - cabinetToPanelOffsetMm,
    );
  }
  if (part.kind === "shelf") {
    const innerWidthMm = spec.width_mm
      - (2 + Math.max(0, Math.trunc(spec.divider_count))) * spec.measured_thickness_mm;
    return orderedBounds(
      DESIGN_CONSTRAINTS.minimumShelfWidthMm,
      Math.min(DESIGN_CONSTRAINTS.widthMm.maximum, innerWidthMm),
    );
  }
  if (part.kind === "top" || part.kind === "bottom" || part.kind === "plinth") {
    const sideThicknessMm = 2 * spec.measured_thickness_mm;
    return orderedBounds(
      DESIGN_CONSTRAINTS.widthMm.minimum - sideThicknessMm,
      DESIGN_CONSTRAINTS.widthMm.maximum - sideThicknessMm,
    );
  }
  return { min: DESIGN_CONSTRAINTS.widthMm.minimum, max: DESIGN_CONSTRAINTS.widthMm.maximum };
}

function verticalPositionFieldBounds(part: ResolvedPart, spec: DesignSpec): PartFieldBounds {
  if (part.kind === "top") {
    const halfThicknessMm = part.thickness_mm / 2;
    return {
      min: DESIGN_CONSTRAINTS.heightMm.minimum - halfThicknessMm,
      max: DESIGN_CONSTRAINTS.heightMm.maximum - halfThicknessMm,
    };
  }
  return { min: 0, max: spec.height_mm };
}

export function SelectedPartInspector({
  part,
  spec,
  override,
  onChange,
  onShelfOpeningChange,
  onRemove,
  onReset,
  onClose,
}: SelectedPartInspectorProps) {
  const widthFieldBounds = partWidthFieldBounds(part, spec);
  const verticalPositionBounds = verticalPositionFieldBounds(part, spec);
  const shelfMatch = /^shelf-(\d+)-bay-\d+$/.exec(part.part_id);
  const shelfIndex = shelfMatch ? Number(shelfMatch[1]) - 1 : undefined;
  const shelfOpenings = shelfIndex === undefined ? [] : shelfOpeningHeights(spec);
  const dividerMatch = /^divider-(\d+)$/.exec(part.part_id);
  const hasOverride = (override !== undefined && Object.keys(override).length > 0)
    || (shelfIndex !== undefined && spec.shelf_height_ratios.length > 0)
    || (dividerMatch !== null && spec.bay_width_ratios.length > 0);
  const topologyRemoval = part.kind === "divider" || part.kind === "shelf"
    || (part.kind === "base_side" && Number(part.part_id.split("-").at(-1)) > 1 && Number(part.part_id.split("-").at(-1)) <= spec.base_cabinet_count);
  const removalHint = part.kind === "divider"
    ? "De angränsande facken slås ihop och alla hyllplan över den nya spännvidden genereras om."
    : part.kind === "shelf"
      ? "Hela hyllraden tas bort så att inga avbrutna hyllplan eller hål lämnas."
      : topologyRemoval
        ? "Angränsande skåpsmoduler slås ihop och stomdelarna genereras om."
        : "Delen tas bort direkt. Om den är bärande visas en integritetsvarning i modellen.";

  return (
    <section className={`${styles.inspector} selected-part-inspector`} aria-label={`Redigera fysisk del ${part.name}`}>
      <header className={styles.inspectorHeader}>
        <span className={styles.inspectorThumbnail}><SlidersHorizontal aria-hidden="true" size={21} strokeWidth={1.5} /></span>
        <div className={styles.inspectorIdentity}>
          <p className="part-inspector-eyebrow">Vald fysisk del</p>
          <h2>{part.name}</h2>
          <div className={styles.inspectorTags}>
            <span>Deltyp: {PART_KIND_LABELS[part.kind]}</span>
            <span>ID: {part.part_id}</span>
          </div>
        </div>
        <button type="button" className={`${styles.inspectorClose} part-inspector-close`} aria-label={`Avmarkera ${part.name}`} onClick={onClose}>
          <X aria-hidden="true" size={18} />
        </button>
      </header>

      <div className={`${styles.inspectorIntro} part-inspector-concept-note`}>
        Fysisk del · {Math.round(part.width_mm)} × {Math.round(part.depth_mm)} × {Math.round(part.thickness_mm * 10) / 10} mm · {part.material_id}. Ändringar uppdaterar anslutande delar automatiskt.
      </div>

      <fieldset className={styles.inspectorSection}>
        <legend>Parametriska mått</legend>
        <div className={`${styles.inspectorFieldGrid} part-inspector-field-grid`}>
          {(["side", "top", "bottom", "shelf", "plinth", "base_side"] as ResolvedPart["kind"][]).includes(part.kind) ? (
            <PartNumberField
              label={part.kind === "side" || part.kind === "base_side" ? "Konstruktionshöjd" : part.kind === "shelf" ? "Fackbredd" : "Möbelbredd via delen"}
              value={part.width_mm}
              min={widthFieldBounds.min}
              max={widthFieldBounds.max}
              step={1}
              onChange={(width_mm) => onChange({ width_mm })}
            />
          ) : null}
          {(part.kind === "side" || part.kind === "top" || part.kind === "bottom" || part.kind === "base_side") ? (
            <PartNumberField
              label="Konstruktionsdjup"
              value={part.depth_mm}
              min={DESIGN_CONSTRAINTS.depthMm.minimum}
              max={DESIGN_CONSTRAINTS.depthMm.maximum}
              step={1}
              onChange={(depth_mm) => onChange({ depth_mm })}
            />
          ) : null}
          {part.kind !== "back" ? <PartNumberField label="Materialtjocklek, hela möbeln" value={spec.measured_thickness_mm} min={17} max={19} step={0.1} onChange={(thickness_mm) => onChange({ thickness_mm })} /> : null}
        </div>
      </fieldset>

      {shelfIndex !== undefined && onShelfOpeningChange ? (
        <fieldset className={`${styles.inspectorSection} ${styles.inspectorSpacing} part-shelf-spacing`}>
          <legend>Fria avstånd runt hyllplanet</legend>
          <p>Mätt från materialyta till materialyta.</p>
          <div className={`${styles.inspectorFieldGrid} part-inspector-field-grid`}>
            <PartNumberField
              label="Fritt under"
              value={shelfOpenings[shelfIndex] ?? DESIGN_CONSTRAINTS.minimumShelfOpeningMm}
              min={DESIGN_CONSTRAINTS.minimumShelfOpeningMm}
              max={spec.height_mm}
              step={1}
              onChange={(value) => onShelfOpeningChange(shelfIndex, value)}
            />
            <PartNumberField
              label="Fritt över"
              value={shelfOpenings[shelfIndex + 1] ?? DESIGN_CONSTRAINTS.minimumShelfOpeningMm}
              min={DESIGN_CONSTRAINTS.minimumShelfOpeningMm}
              max={spec.height_mm}
              step={1}
              onChange={(value) => onShelfOpeningChange(shelfIndex + 1, value)}
            />
          </div>
        </fieldset>
      ) : null}

      {(part.kind === "divider" || part.kind === "shelf" || part.kind === "top" || (part.kind === "bottom" && spec.furniture_type === "wall_library")) ? <fieldset className={styles.inspectorSection}>
        <legend>Konstruktionsplacering</legend>
        <div className={`${styles.inspectorFieldGrid} part-inspector-field-grid`}>
          {part.kind === "divider" ? <PartNumberField label="Avdelarcentrum från vänster (X)" value={part.position_mm.x} min={0} max={spec.width_mm} step={1} onChange={(position_x_mm) => onChange({ position_x_mm })} /> : null}
          {part.kind !== "divider" ? (
            <PartNumberField
              label="Nivå från golv (Z)"
              value={part.position_mm.z}
              min={verticalPositionBounds.min}
              max={verticalPositionBounds.max}
              step={1}
              onChange={(position_z_mm) => onChange({ position_z_mm })}
            />
          ) : null}
        </div>
      </fieldset> : null}

      <footer className={styles.inspectorFooter}>
        <p className={`${styles.inspectorRemovalHint} part-removal-hint`}>{removalHint}</p>
        <div className={styles.inspectorActions}>
          <button type="button" className={`${styles.inspectorReset} part-reset-button`} disabled={!hasOverride} onClick={onReset}>
            <RotateCcw aria-hidden="true" size={15} /> Återställ del
          </button>
          <button type="button" className={`${styles.inspectorDelete} part-delete-button`} onClick={onRemove}>
            <Trash2 aria-hidden="true" size={15} /> {topologyRemoval ? "Ta bort och bygg om" : "Ta bort del"}
          </button>
        </div>
      </footer>
      <p className={`${styles.inspectorUndoHint} part-inspector-undo-hint`}>Ändringen kan ångras i verktygsraden.</p>
    </section>
  );
}
