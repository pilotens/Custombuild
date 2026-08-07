"use client";

import { ChevronDown, Info, Ruler, SlidersHorizontal, Wrench } from "lucide-react";
import { MACHINES, MATERIALS, type DesignSpec } from "@/lib/design-types";

type NumericKey = {
  [Key in keyof DesignSpec]: DesignSpec[Key] extends number ? Key : never;
}[keyof DesignSpec];

interface ParameterPanelProps {
  spec: DesignSpec;
  mode: "guided" | "expert";
  onChange: (patch: Partial<DesignSpec>, reason?: string) => void;
}

function NumericField({
  id,
  label,
  field,
  value,
  unit,
  min,
  max,
  step = 1,
  onChange,
  hint,
}: {
  id: string;
  label: string;
  field: NumericKey;
  value: number;
  unit: string;
  min: number;
  max: number;
  step?: number;
  onChange: ParameterPanelProps["onChange"];
  hint?: string;
}) {
  return (
    <label className="field" htmlFor={id}>
      <span className="field-label">
        {label}
        {hint ? <span title={hint}><Info aria-label={hint} size={13} /></span> : null}
      </span>
      <span className="number-input-wrap">
        <input
          id={id}
          type="number"
          value={value}
          min={min}
          max={max}
          step={step}
          inputMode="decimal"
          onChange={(event) => onChange({ [field]: Number(event.target.value) }, `${label} ändrades`)}
        />
        <span>{unit}</span>
      </span>
    </label>
  );
}

function PanelSection({
  title,
  icon,
  children,
  open = true,
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  open?: boolean;
}) {
  return (
    <details className="parameter-section" open={open}>
      <summary>
        <span>{icon}{title}</span>
        <ChevronDown aria-hidden="true" size={16} className="summary-chevron" />
      </summary>
      <div className="parameter-section-body">{children}</div>
    </details>
  );
}

export function ParameterPanel({ spec, mode, onChange }: ParameterPanelProps) {
  return (
    <section className="panel parameter-panel" id="parameters" aria-labelledby="parameter-heading">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">DesignSpec 1.0</p>
          <h2 id="parameter-heading">Parametrar</h2>
        </div>
        <span className="mode-chip">{mode === "expert" ? "Expert" : "Guidad"}</span>
      </div>
      <div className="panel-scroll">
        <PanelSection title="Yttermått" icon={<Ruler aria-hidden="true" size={16} />}>
          <div className="field-grid three-fields">
            <NumericField id="width" label="Bredd" field="width_mm" value={spec.width_mm} unit="mm" min={300} max={3_000} onChange={onChange} />
            <NumericField id="height" label="Höjd" field="height_mm" value={spec.height_mm} unit="mm" min={400} max={3_000} onChange={onChange} />
            <NumericField id="depth" label="Djup" field="depth_mm" value={spec.depth_mm} unit="mm" min={180} max={900} onChange={onChange} />
          </div>
        </PanelSection>

        <PanelSection title="Material" icon={<SlidersHorizontal aria-hidden="true" size={16} />}>
          <label className="field field-wide" htmlFor="material-field">
            <span className="field-label">Skivmaterial</span>
            <select
              id="material-field"
              value={spec.material_id}
              onChange={(event) => {
                const material = MATERIALS.find((candidate) => candidate.id === event.target.value);
                if (!material) return;
                onChange(
                  {
                    material_id: material.id,
                    material_name: material.name,
                    nominal_thickness_mm: material.nominalThicknessMm,
                    measured_thickness_mm: material.measuredThicknessMm,
                  },
                  "Materialversion ändrades",
                );
              }}
            >
              {MATERIALS.map((material) => (
                <option key={material.id} value={material.id}>{material.name} · {material.nominalThicknessMm} mm</option>
              ))}
            </select>
          </label>
          <div className="field-grid two-fields">
            <NumericField id="nominal-thickness" label="Nominell" field="nominal_thickness_mm" value={spec.nominal_thickness_mm} unit="mm" min={18} max={18} step={0.1} onChange={onChange} />
            <NumericField
              id="measured-thickness"
              label="Uppmätt"
              field="measured_thickness_mm"
              value={spec.measured_thickness_mm}
              unit="mm"
              min={17}
              max={19}
              step={0.1}
              onChange={onChange}
              hint="Detta mått driver all fog- och tillverkningsgeometri."
            />
          </div>
          <div className="material-note">
            <Info aria-hidden="true" size={14} />
            Screeningvärden. Batchens materialdata ska verifieras före release.
          </div>
        </PanelSection>

        <PanelSection title="Inredning & last" icon={<SlidersHorizontal aria-hidden="true" size={16} />}>
          <div className="field-grid two-fields">
            <NumericField id="shelf-count" label="Hyllor" field="shelf_count" value={spec.shelf_count} unit="st" min={0} max={30} onChange={onChange} />
            <NumericField id="shelf-load" label="Last / hylla" field="load_per_shelf_kg" value={spec.load_per_shelf_kg} unit="kg" min={0} max={250} step={1} onChange={onChange} />
          </div>
          <fieldset className="segmented-field">
            <legend>Hylltyp</legend>
            <div className="segmented-control full-width">
              <button type="button" className={spec.fixed_shelves ? "active" : ""} aria-pressed={spec.fixed_shelves} onClick={() => onChange({ fixed_shelves: true }, "Fasta hyllor valdes")}>Fasta</button>
              <button type="button" className={!spec.fixed_shelves ? "active" : ""} aria-pressed={!spec.fixed_shelves} onClick={() => onChange({ fixed_shelves: false }, "Justerbara hyllor valdes")}>Justerbara</button>
            </div>
          </fieldset>
          <div className="switch-list">
            <label className="switch-row">
              <span><strong>Bakstycke</strong><small>6 mm i not</small></span>
              <input type="checkbox" checked={spec.back_panel} onChange={(event) => onChange({ back_panel: event.target.checked }, "Bakstycke ändrades")} />
            </label>
            <label className="switch-row">
              <span><strong>Sockel</strong><small>Främre, 80 mm</small></span>
              <input type="checkbox" checked={spec.plinth} onChange={(event) => onChange({ plinth: event.target.checked }, "Sockel ändrades")} />
            </label>
          </div>
          <NumericField id="divider-count" label="Vertikala avdelare" field="divider_count" value={spec.divider_count} unit="st" min={0} max={8} onChange={onChange} />
        </PanelSection>

        <PanelSection title="Fogar & kanter" icon={<Wrench aria-hidden="true" size={16} />} open={mode === "expert"}>
          <div className="field field-wide" id="joint-system">
            <span className="field-label">Fogsystem</span>
            <output>Not/spår (enda produktionsstödda MVP-fog)</output>
            <small>Enda fogsystemet med komplett geometri-, DFM-, CAM- och monteringskedja i MVP:n.</small>
          </div>
          <NumericField id="edge-band" label="Kantlist" field="edge_band_mm" value={spec.edge_band_mm} unit="mm" min={0} max={5} step={0.1} onChange={onChange} />
        </PanelSection>

        {mode === "expert" ? (
          <PanelSection title="Råmaterial & maskin" icon={<Wrench aria-hidden="true" size={16} />}>
            <div className="field-grid two-fields">
              <NumericField id="stock-width" label="Skivlängd" field="stock_width_mm" value={spec.stock_width_mm} unit="mm" min={300} max={5_000} onChange={onChange} />
              <NumericField id="stock-height" label="Skivbredd" field="stock_height_mm" value={spec.stock_height_mm} unit="mm" min={300} max={2_500} onChange={onChange} />
              <NumericField id="stock-count" label="Stomskivor" field="stock_count" value={spec.stock_count} unit="st" min={1} max={100} onChange={onChange} />
              <NumericField id="back-stock-count" label="Ryggskivor" field="back_stock_count" value={spec.back_stock_count} unit="st" min={1} max={100} onChange={onChange} />
              <NumericField id="back-stock-width" label="Rygglängd" field="back_stock_width_mm" value={spec.back_stock_width_mm} unit="mm" min={300} max={5_000} onChange={onChange} />
              <NumericField id="back-stock-height" label="Ryggbredd" field="back_stock_height_mm" value={spec.back_stock_height_mm} unit="mm" min={300} max={2_500} onChange={onChange} />
            </div>
            <label className="field field-wide" htmlFor="machine-field">
              <span className="field-label">Maskinprofil</span>
              <select id="machine-field" value={spec.machine_profile_id} onChange={(event) => onChange({ machine_profile_id: event.target.value }, "Maskinprofil ändrades")}>
                {MACHINES.map((machine) => <option key={machine.id} value={machine.id}>{machine.name}</option>)}
              </select>
            </label>
            <div className="release-gate-note">
              <span className="gate-dot" />
              Maskinprofilen är en valideringsprofil. Fysisk air-cut och referensdel krävs.
            </div>
          </PanelSection>
        ) : null}
      </div>
    </section>
  );
}
