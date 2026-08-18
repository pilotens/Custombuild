"use client";

import { useState } from "react";
import {
  ArrowRight,
  Box,
  Check,
  Grid3X3,
  Layers3,
  Minus,
  Plus,
  Ruler,
} from "lucide-react";
import {
  currentEqualBayWidthMm,
  MAX_BAY_COUNT,
  MAX_TARGET_BAY_WIDTH_MM,
  MIN_TARGET_BAY_WIDTH_MM,
  setBayWidthRatio,
  setShelfHeightRatio,
  symmetrizeBayRatios,
  symmetrizeShelfRatios,
  targetBayLayout,
} from "@/lib/design-engine";
import {
  DESIGN_CONSTRAINTS,
  maximumBaseCabinetHeightMm,
} from "@/lib/design-constraints";
import { MATERIALS, type DesignSpec, type ValidationStatus } from "@/lib/design-types";
import { ContextPanel, DimensionInput, SegmentedControl } from "./design-system";
import styles from "./studio-inspector.module.css";

interface StudioInspectorProps {
  spec: DesignSpec;
  status: ValidationStatus;
  partCount: number;
  onChange: (patch: Partial<DesignSpec>, reason?: string) => void;
  onOpenExplore: () => void;
  onOpenCheck: () => void;
}

function clampInteger(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)));
}

function Counter({
  label,
  value,
  min,
  max,
  disabled = false,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <div className={styles.counter}>
      <span>{label}</span>
      <div>
        <button type="button" aria-label={`Minska ${label.toLowerCase()}`} disabled={disabled || value <= min} onClick={() => onChange(value - 1)}>
          <Minus aria-hidden="true" size={15} />
        </button>
        <output aria-label={label}>{value}</output>
        <button type="button" aria-label={`Öka ${label.toLowerCase()}`} disabled={disabled || value >= max} onClick={() => onChange(value + 1)}>
          <Plus aria-hidden="true" size={15} />
        </button>
      </div>
    </div>
  );
}

function equalRatios(count: number): number[] {
  return Array.from({ length: count }, () => 1 / count);
}

const RATIO_COMPARISON_TOLERANCE = 1e-9;

function customBayRatiosAreEditable(count: number): boolean {
  return Number.isInteger(count) && count > 0 && count * 0.08 <= 1 + RATIO_COMPARISON_TOLERANCE;
}

function customShelfRatiosAreEditable(count: number): boolean {
  return Number.isInteger(count)
    && count > 0
    && 0.05 + (count - 1) * 0.05 <= 0.95 + RATIO_COMPARISON_TOLERANCE;
}

function normalizedBayRatios(ratios: number[], count: number): number[] {
  if (ratios.length !== count || ratios.some((ratio) => !Number.isFinite(ratio) || ratio <= 0)) {
    return equalRatios(count);
  }
  const total = ratios.reduce((sum, ratio) => sum + ratio, 0);
  return total > 0 ? ratios.map((ratio) => ratio / total) : equalRatios(count);
}

function editableShelfRatios(ratios: number[], count: number): number[] {
  const valid = ratios.length === count && ratios.every((ratio, index) => (
    Number.isFinite(ratio)
    && ratio >= 0.05 - RATIO_COMPARISON_TOLERANCE
    && ratio <= 0.95 + RATIO_COMPARISON_TOLERANCE
    && (index === 0 || ratio - ratios[index - 1]! >= 0.05 - RATIO_COMPARISON_TOLERANCE)
  ));
  return valid
    ? ratios
    : Array.from({ length: count }, (_, index) => (index + 1) / (count + 1));
}

function DetailedLayout({
  spec,
  onChange,
}: Pick<StudioInspectorProps, "spec" | "onChange">) {
  const hasCustomLayout = spec.bay_width_ratios.length > 0 || spec.shelf_height_ratios.length > 0;
  const [expanded, setExpanded] = useState(hasCustomLayout);
  const bayCount = Math.max(1, spec.divider_count + 1);
  const bayRatiosEditable = customBayRatiosAreEditable(bayCount);
  const rawBayRatios = bayRatiosEditable
    ? normalizedBayRatios(spec.bay_width_ratios, bayCount)
    : [];
  const bayRatios = bayRatiosEditable && spec.symmetry_locked
    ? symmetrizeBayRatios(rawBayRatios, bayCount)
    : rawBayRatios;
  const innerWidthMm = Math.max(
    spec.width_mm - (bayCount + 1) * spec.measured_thickness_mm,
    1,
  );
  const shelfCount = Math.max(0, spec.shelf_count);
  const shelfRatiosEditable = customShelfRatiosAreEditable(shelfCount);
  const rawShelfRatios = shelfRatiosEditable
    ? editableShelfRatios(spec.shelf_height_ratios, shelfCount)
    : [];
  const shelfRatios = shelfRatiosEditable && spec.symmetry_locked
    ? symmetrizeShelfRatios(rawShelfRatios, shelfCount)
    : rawShelfRatios;

  const updateBay = (index: number, requested: number) => {
    if (!bayRatiosEditable) return;
    const next = setBayWidthRatio(
      { ...spec, bay_width_ratios: bayRatios },
      index,
      requested,
    );
    onChange({
      bay_sizing_mode: "count",
      reinforcement_mode: "manual",
      bay_width_ratios: next.bay_width_ratios,
    }, spec.symmetry_locked ? "Speglade fackbredder ändrades" : "Individuella fackbredder ändrades");
  };

  const updateShelf = (index: number, requested: number) => {
    if (!shelfRatiosEditable) return;
    const next = setShelfHeightRatio(
      { ...spec, shelf_height_ratios: shelfRatios },
      index,
      requested,
    );
    onChange(
      { shelf_height_ratios: next.shelf_height_ratios },
      spec.symmetry_locked ? "Speglade hyllnivåer ändrades" : "Individuella hyllnivåer ändrades",
    );
  };

  return (
    <details
      className={styles.advancedLayout}
      open={expanded}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        <span><strong>Detaljerad indelning</strong><small>Anpassa enskilda fack och hyllnivåer.</small></span>
        <span aria-hidden="true">+</span>
      </summary>
      <div className={styles.advancedBody}>
        <section className={styles.ratioGroup} aria-labelledby="studio-bay-widths-heading">
          <header>
            <div><h4 id="studio-bay-widths-heading">Individuella fackbredder</h4><p>{spec.symmetry_locked ? "Vänster och höger ändras som spegelpar." : "Varje fack kan avvika från mittlinjen."}</p></div>
            <button type="button" disabled={!bayRatiosEditable} onClick={() => onChange({ bay_sizing_mode: "count", reinforcement_mode: "manual", bay_width_ratios: [] }, "Likstora fack återställdes")}>Likadana</button>
          </header>
          {bayRatiosEditable ? <div className={styles.ratioControls}>
            {bayRatios.map((ratio, index) => {
              const mirror = bayCount - 1 - index;
              const slots = index === mirror ? 1 : 2;
              const maxPercent = spec.symmetry_locked
                ? Math.floor(((1 - 0.08 * (bayCount - slots)) / slots) * 100)
                : bayCount < 2
                  ? 100
                  : Math.round((ratio + bayRatios[index === bayCount - 1 ? index - 1 : index + 1]!) * 100 - 8);
              return (
                <label key={index}>
                  <span>Fack {index + 1}<output>{Math.round(ratio * innerWidthMm).toLocaleString("sv-SE")} mm</output></span>
                  <input
                    type="range"
                    aria-label={`Bredd för fack ${index + 1}`}
                    min={8}
                    max={Math.max(8, maxPercent)}
                    step={1}
                    value={Math.round(ratio * 100)}
                    disabled={bayCount < 2}
                    onChange={(event) => updateBay(index, Number(event.target.value) / 100)}
                  />
                </label>
              );
            })}
          </div> : (
            <p className={styles.inlineNote}>
              Individuella fackbredder är avstängda vid 13–17 fack eftersom varje fack måste vara minst 8 %. Likstora fack används.
            </p>
          )}
        </section>

        {shelfCount > 0 ? (
          <section className={styles.ratioGroup} aria-labelledby="studio-shelf-levels-heading">
            <header>
              <div><h4 id="studio-shelf-levels-heading">Individuella hyllnivåer</h4><p>{spec.symmetry_locked ? "Nivåerna balanseras kring möbelns mittlinje." : "Placera varje hyllplan separat."}</p></div>
              <button type="button" disabled={!shelfRatiosEditable} onClick={() => onChange({ shelf_height_ratios: [] }, "Jämna hyllnivåer återställdes")}>Fördela jämnt</button>
            </header>
            {shelfRatiosEditable ? <div className={styles.ratioControls}>
              {shelfRatios.map((ratio, index) => (
                <label key={index}>
                  <span>Hylla {index + 1}<output>{Math.round(ratio * 100)} % i hyllzonen</output></span>
                  <input
                    type="range"
                    aria-label={`Höjd för hylla ${index + 1}`}
                    min={Math.round((index === 0 ? 0.05 : shelfRatios[index - 1]! + 0.05) * 100)}
                    max={Math.round((index === shelfCount - 1 ? 0.95 : shelfRatios[index + 1]! - 0.05) * 100)}
                    step={1}
                    value={Math.round(ratio * 100)}
                    onChange={(event) => updateShelf(index, Number(event.target.value) / 100)}
                  />
                </label>
              ))}
            </div> : (
              <p className={styles.inlineNote}>
                Individuella hyllnivåer är avstängda vid 20–40 nivåer eftersom nivåerna måste ha minst 5 % mellanrum. Jämn fördelning används.
              </p>
            )}
          </section>
        ) : null}
      </div>
    </details>
  );
}

export function StudioInspector({
  spec,
  status,
  partCount,
  onChange,
  onOpenExplore,
  onOpenCheck,
}: StudioInspectorProps) {
  const bayCount = spec.divider_count + 1;
  const equalWidth = currentEqualBayWidthMm(spec);
  const targetLayout = targetBayLayout(spec);
  const statusLabel = status === "PASS" ? "Godkänt" : status === "WARNING" ? "Behöver beslut" : "Måste lösas";
  const baseCabinetMaximum = maximumBaseCabinetHeightMm(
    spec.height_mm,
    spec.measured_thickness_mm,
  );

  const setBayCount = (nextBayCount: number) => {
    const next = clampInteger(nextBayCount, 1, MAX_BAY_COUNT);
    onChange({
      bay_sizing_mode: "count",
      divider_count: next - 1,
      bay_width_ratios: [],
      reinforcement_mode: "manual",
      ...(spec.furniture_type === "wall_library" && spec.base_cabinet_count > 0
        ? { base_cabinet_count: next }
        : {}),
    }, `Antalet fack ändrades till ${next}`);
  };

  const setTargetWidth = (target: number) => {
    const requested = Math.min(MAX_TARGET_BAY_WIDTH_MM, Math.max(MIN_TARGET_BAY_WIDTH_MM, Math.round(target)));
    const layout = targetBayLayout({ ...spec, target_bay_width_mm: requested });
    onChange({
      bay_sizing_mode: "target_width",
      target_bay_width_mm: requested,
      divider_count: layout.dividerCount,
      bay_width_ratios: [],
      ...(spec.furniture_type === "wall_library" && spec.base_cabinet_count > 0
        ? { base_cabinet_count: layout.bayCount }
        : {}),
    }, `Minsta fackbredd ändrades till ${requested} mm`);
  };

  return (
    <ContextPanel
      className={styles.panel}
      eyebrow="Studio · Aktuell konstruktion"
      title="Möbel"
      description="Forma den riktiga modellen. Alla ändringar sparas i samma konstruktionsunderlag."
      actions={(
        <button type="button" className={styles.changeDesign} onClick={onOpenExplore}>Byt startdesign</button>
      )}
      footer={(
        <button type="button" className={styles.checkAction} onClick={onOpenCheck}>
          Kontrollera konstruktionen <ArrowRight aria-hidden="true" size={16} />
        </button>
      )}
    >
      <div className={styles.summary}>
        <span className={styles.status} data-status={status.toLowerCase()}><Check aria-hidden="true" size={14} /> {statusLabel}</span>
        <span>{partCount} delar</span>
        <span>{spec.material_name}</span>
      </div>

      <section className={styles.section} aria-labelledby="studio-size-heading">
        <header><Ruler aria-hidden="true" size={18} /><div><h3 id="studio-size-heading">Yttermått</h3><p>Dra även måtthandtagen direkt i modellen.</p></div></header>
        <div className={styles.dimensions}>
          <DimensionInput label="Bredd" value={spec.width_mm} min={DESIGN_CONSTRAINTS.widthMm.minimum} max={DESIGN_CONSTRAINTS.widthMm.maximum} step={10} onCommit={(width_mm) => onChange({ width_mm }, "Bredd ändrades")} />
          <DimensionInput label="Höjd" value={spec.height_mm} min={DESIGN_CONSTRAINTS.heightMm.minimum} max={DESIGN_CONSTRAINTS.heightMm.maximum} step={10} onCommit={(height_mm) => onChange({ height_mm }, "Höjd ändrades")} />
          <DimensionInput label="Djup" value={spec.depth_mm} min={DESIGN_CONSTRAINTS.depthMm.minimum} max={DESIGN_CONSTRAINTS.depthMm.maximum} step={10} onCommit={(depth_mm) => onChange({ depth_mm }, "Djup ändrades")} />
        </div>
      </section>

      <section className={styles.section} aria-labelledby="studio-grid-heading">
        <header><Grid3X3 aria-hidden="true" size={18} /><div><h3 id="studio-grid-heading">Fack och hyllor</h3><p>Välj antal eller maximera antalet utifrån önskad fri bredd.</p></div></header>
        <SegmentedControl
          label="Hur vill du bestämma facken?"
          value={spec.bay_sizing_mode}
          options={[
            { id: "count", label: "Antal fack" },
            { id: "target_width", label: "Minsta bredd" },
          ]}
          onChange={(mode) => {
            if (mode === "count") setBayCount(bayCount);
            else setTargetWidth(spec.target_bay_width_mm);
          }}
        />
        {spec.bay_sizing_mode === "count" ? (
          <Counter label="Vertikala fack" value={bayCount} min={1} max={MAX_BAY_COUNT} onChange={setBayCount} />
        ) : (
          <DimensionInput
            label="Minsta fria fackbredd"
            value={spec.target_bay_width_mm}
            min={MIN_TARGET_BAY_WIDTH_MM}
            max={MAX_TARGET_BAY_WIDTH_MM}
            step={1}
            hint={`Ger ${targetLayout.bayCount} fack med cirka ${targetLayout.actualClearWidthMm.toLocaleString("sv-SE")} mm fri bredd.`}
            onCommit={setTargetWidth}
          />
        )}
        <Counter
          label="Hyllnivåer"
          value={spec.shelf_count}
          min={0}
          max={DESIGN_CONSTRAINTS.shelfCount.maximum}
          onChange={(shelf_count) => onChange({ shelf_count, shelf_height_ratios: [] }, `Antalet hyllnivåer ändrades till ${shelf_count}`)}
        />
        <div className={styles.gridResult} role="status">
          <strong>{bayCount} fack · {spec.shelf_count} hyllnivåer</strong>
          <span>{spec.bay_width_ratios.length ? "Individuella bredder" : `Cirka ${equalWidth.toLocaleString("sv-SE")} mm fri bredd per fack`}</span>
        </div>
        <DetailedLayout spec={spec} onChange={onChange} />
      </section>

      {spec.furniture_type === "wall_library" ? (
        <section className={styles.section} aria-labelledby="studio-base-heading">
          <header><Box aria-hidden="true" size={18} /><div><h3 id="studio-base-heading">Underskåp</h3><p>Den verkliga nedre stommen inom möbelns yttermått.</p></div></header>
          <div className={styles.baseDimensions}>
            <DimensionInput
              label="Höjd underskåp"
              value={spec.base_cabinet_height_mm}
              min={DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm}
              max={baseCabinetMaximum}
              step={10}
              hint={`Måste lämna minst ${DESIGN_CONSTRAINTS.baseCabinetUpperClearanceMm} mm plus materialtjockleken till överdelen. Max ${baseCabinetMaximum.toLocaleString("sv-SE")} mm med aktuell möbelhöjd.`}
              onCommit={(base_cabinet_height_mm) => onChange({ base_cabinet_height_mm }, "Underskåpshöjden ändrades")}
            />
            <div className={styles.readOnlyMetric}>
              <span>Djup underskåp</span>
              <strong>{spec.depth_mm.toLocaleString("sv-SE")} mm</strong>
              <small>Följer möbelns djup.</small>
            </div>
          </div>
          <Counter
            label="Skåpsmoduler"
            value={spec.base_cabinet_count}
            min={1}
            max={DESIGN_CONSTRAINTS.baseCabinetModuleCount.maximum}
            disabled={spec.reinforcement_mode === "auto"}
            onChange={(base_cabinet_count) => onChange({ base_cabinet_count }, `Antalet skåpsmoduler ändrades till ${base_cabinet_count}`)}
          />
          {spec.reinforcement_mode === "auto" ? <p className={styles.inlineNote}>Skåpsmodulerna följer automatiskt de bärande centrumlinjerna.</p> : null}
        </section>
      ) : null}

      <section className={styles.section} aria-labelledby="studio-material-heading">
        <header><Layers3 aria-hidden="true" size={18} /><div><h3 id="studio-material-heading">Material</h3><p>Detta val används i vikt- och hållfasthetskontrollen.</p></div></header>
        <div className={styles.materials}>
          {MATERIALS.map((material) => (
            <button
              key={material.id}
              type="button"
              aria-pressed={spec.material_id === material.id}
              onClick={() => onChange({
                material_id: material.id,
                material_name: material.name,
                nominal_thickness_mm: material.nominalThicknessMm,
                measured_thickness_mm: material.measuredThicknessMm,
              }, `Material ändrades till ${material.name}`)}
            >
              <span className={styles.swatch} data-material={material.id} />
              <span><strong>{material.name}</strong><small>{material.nominalThicknessMm} mm</small></span>
              {spec.material_id === material.id ? <Check aria-hidden="true" size={15} /> : null}
            </button>
          ))}
        </div>
        <DimensionInput
          label="Total planerad last per hyllrad"
          value={spec.load_per_shelf_kg}
          min={DESIGN_CONSTRAINTS.shelfLoadKg.minimum}
          max={DESIGN_CONSTRAINTS.shelfLoadKg.maximum}
          step={1}
          unit="kg"
          hint="Summan för alla fack på samma hyllnivå, jämnt fördelad i hållfasthetskontrollen."
          onCommit={(load_per_shelf_kg) => onChange({ load_per_shelf_kg }, "Total planerad last per hyllrad ändrades")}
        />
      </section>

      <section className={styles.section} aria-labelledby="studio-construction-heading">
        <header><Box aria-hidden="true" size={18} /><div><h3 id="studio-construction-heading">Konstruktion</h3><p>Vanliga val som påverkar byggbarheten.</p></div></header>
        <SegmentedControl
          label="Konstruktionsstöd"
          value={spec.reinforcement_mode}
          options={[
            { id: "auto", label: "Automatiskt", description: "Systemet dimensionerar bärande avdelare från bredd och last." },
            { id: "manual", label: "Manuellt", description: "Din valda indelning behålls och granskas." },
          ]}
          onChange={(reinforcement_mode) => onChange({ reinforcement_mode }, reinforcement_mode === "auto" ? "Automatiska konstruktionsstöd valdes" : "Manuella konstruktionsstöd valdes")}
        />
        <div className={styles.toggles}>
          {([
            ["symmetry_locked", "Symmetrisk indelning", "Speglar ändringar runt mitten"],
            ["back_panel", "Bakstycke", "Stabiliserar stommen"],
            ["plinth", "Sockel", "Indragen bas under möbeln"],
            ["fixed_shelves", "Fasta hyllor", "Frästa bärande spår"],
          ] as const).map(([field, label, hint]) => (
            <label key={field}>
              <span><strong>{label}</strong><small>{hint}</small></span>
              <input type="checkbox" checked={spec[field]} onChange={(event) => onChange({ [field]: event.target.checked }, `${label} ändrades`)} />
            </label>
          ))}
        </div>
      </section>
    </ContextPanel>
  );
}
