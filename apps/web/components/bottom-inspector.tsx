"use client";

import { useMemo, useState } from "react";
import { Boxes, Layers3, ListChecks, Route, ShieldCheck } from "lucide-react";
import type { DesignSpec, ResolvedDesign } from "@/lib/design-types";
import {
  ProductionWorkflow,
  type ProductionSummary,
} from "./production-workflow";

export type InspectorTabId = "parts" | "bom" | "nesting" | "operations" | "production";

const tabs: { id: InspectorTabId; label: string; icon: typeof Boxes }[] = [
  { id: "parts", label: "Delar", icon: Boxes },
  { id: "bom", label: "BOM", icon: ListChecks },
  { id: "nesting", label: "Nesting", icon: Layers3 },
  { id: "operations", label: "Operationer", icon: Route },
  { id: "production", label: "Frisläppning", icon: ShieldCheck },
];

interface BottomInspectorProps {
  design: ResolvedDesign;
  spec: DesignSpec;
  selectedPartId?: string;
  onSelectPart: (partId: string) => void;
  onProductionSummaryChange: (summary: ProductionSummary) => void;
  activeTab: InspectorTabId;
  onActiveTabChange: (tab: InspectorTabId) => void;
}

function PartTable({ design, selectedPartId, onSelectPart }: BottomInspectorProps) {
  return (
    <div className="table-scroll">
      <table>
        <thead><tr><th>Part-ID</th><th>Benämning</th><th>Färdigmått</th><th>Material</th><th>Bruttovikt*</th><th>Features</th></tr></thead>
        <tbody>
          {design.parts.map((part) => (
            <tr key={part.part_id} className={selectedPartId === part.part_id ? "selected-row" : ""}>
              <td><code>{part.part_id}</code></td>
              <td><button type="button" className="table-link" onClick={() => onSelectPart(part.part_id)}>{part.name}</button></td>
              <td>{part.width_mm.toFixed(1)} × {part.depth_mm.toFixed(1)} × {part.thickness_mm.toFixed(1)} mm</td>
              <td>{design.spec.material_name}</td>
              <td>{part.weight_kg.toFixed(2)} kg</td>
              <td><span className="count-chip">{part.features.length}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="table-note">* Konservativ massa för färdigämnet före spår, hål och annan materialavverkning.</p>
    </div>
  );
}

function BomTable({ design, onSelectPart }: BottomInspectorProps) {
  return (
    <div className="table-scroll">
      <table>
        <thead><tr><th>Rad</th><th>Artikel</th><th>Mått</th><th>Material</th><th>Antal</th></tr></thead>
        <tbody>
          {design.bom.map((line) => (
            <tr key={line.id}>
              <td><code>{line.id}</code></td>
              <td>
                {line.part_ids[0] ? <button type="button" className="table-link" onClick={() => onSelectPart(line.part_ids[0] ?? "")}>{line.item}</button> : line.item}
              </td>
              <td>{line.dimensions ?? "—"}</td>
              <td>{line.material ?? "Beslag"}</td>
              <td><strong>{line.quantity}</strong> {line.unit}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NestingView({ design, onSelectPart }: BottomInspectorProps) {
  const [sheet, setSheet] = useState(1);
  const activeSheet = Math.min(sheet, Math.max(design.nesting.sheet_count, 1));
  const placements = design.nesting.placements.filter((placement) => placement.sheet === activeSheet);
  const labels = new Map(design.parts.map((part) => [part.part_id, part.name]));
  return (
    <div className="nesting-layout">
      <div className="nesting-canvas-wrap">
        <svg
          className="nesting-svg"
          viewBox={`0 0 ${design.spec.stock_width_mm} ${design.spec.stock_height_mm}`}
          role="img"
          aria-label={`Nestingkarta för skiva ${activeSheet}`}
          preserveAspectRatio="xMidYMid meet"
        >
          <rect width={design.spec.stock_width_mm} height={design.spec.stock_height_mm} className="sheet-outline" />
          <defs>
            <pattern id="stock-grid" width="100" height="100" patternUnits="userSpaceOnUse">
              <path d="M 100 0 L 0 0 0 100" className="stock-grid-line" />
            </pattern>
          </defs>
          <rect width={design.spec.stock_width_mm} height={design.spec.stock_height_mm} fill="url(#stock-grid)" />
          {placements.map((placement, index) => (
            <g
              key={placement.part_id}
              role="button"
              tabIndex={0}
              aria-label={`${labels.get(placement.part_id) ?? placement.part_id}, ${placement.width_mm} gånger ${placement.height_mm} millimeter`}
              onClick={() => onSelectPart(placement.part_id)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") onSelectPart(placement.part_id);
              }}
              className="nesting-part"
            >
              <rect x={placement.x_mm} y={placement.y_mm} width={placement.width_mm} height={placement.height_mm} rx="5" className={`nesting-fill nesting-fill-${index % 5}`} />
              <text x={placement.x_mm + 16} y={placement.y_mm + 34}>{placement.part_id}</text>
              <text className="nesting-size" x={placement.x_mm + 16} y={placement.y_mm + 64}>{placement.width_mm} × {placement.height_mm}</text>
            </g>
          ))}
        </svg>
      </div>
      <aside className="nesting-sidebar">
        <div className="metric-row"><span>Utnyttjande</span><strong>{design.nesting.utilization_percent} %</strong></div>
        <div className="utilization-bar"><span style={{ width: `${Math.min(design.nesting.utilization_percent, 100)}%` }} /></div>
        <div className="metric-row"><span>Skivor</span><strong>{design.nesting.sheet_count}</strong></div>
        <div className="metric-row"><span>Mellanrum</span><strong>8 mm</strong></div>
        <div className="sheet-switcher" aria-label="Välj skiva">
          {Array.from({ length: design.nesting.sheet_count }, (_, index) => index + 1).map((sheetNumber) => (
            <button key={sheetNumber} type="button" className={sheetNumber === activeSheet ? "active" : ""} aria-pressed={sheetNumber === activeSheet} onClick={() => setSheet(sheetNumber)}>{sheetNumber}</button>
          ))}
        </div>
      </aside>
    </div>
  );
}

function OperationsTable({ design, selectedPartId, onSelectPart }: BottomInspectorProps) {
  const operations = useMemo(
    () => selectedPartId ? design.operations.filter((operation) => operation.part_id === selectedPartId) : design.operations,
    [design.operations, selectedPartId],
  );
  return (
    <div className="table-scroll">
      <table>
        <thead><tr><th>#</th><th>Part-ID</th><th>Operation</th><th>Sida</th><th>Verktyg</th><th>Djup</th><th>Status</th></tr></thead>
        <tbody>
          {operations.map((operation) => (
            <tr key={operation.id}>
              <td>{operation.sequence}</td>
              <td><button type="button" className="table-link mono" onClick={() => onSelectPart(operation.part_id)}>{operation.part_id}</button></td>
              <td>{operation.operation}</td>
              <td>{operation.side}</td>
              <td>{operation.tool}</td>
              <td>{operation.depth_mm} mm</td>
              <td><span className="operation-preview">Ej CAM-validerad</span></td>
            </tr>
          ))}
        </tbody>
      </table>
      {operations.length === 0 ? <p className="empty-table">Den valda delen har inga operationer.</p> : null}
    </div>
  );
}

export function BottomInspector(props: BottomInspectorProps) {
  const activeTab = props.activeTab;
  const activePanelId = `inspector-panel-${activeTab}`;
  return (
    <section className="panel bottom-inspector" id="production-tabs" aria-label="Produktionsdata">
      <div className="inspector-tabs" role="tablist" aria-label="Produktionsdata">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          const count = tab.id === "parts"
            ? props.design.parts.length
            : tab.id === "bom"
              ? props.design.bom.length
              : tab.id === "nesting"
                ? props.design.nesting.sheet_count
                : tab.id === "operations"
                  ? props.design.operations.length
                  : undefined;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.id}
              aria-controls={`inspector-panel-${tab.id}`}
              id={`inspector-tab-${tab.id}`}
              className={activeTab === tab.id ? "active" : ""}
              onClick={() => props.onActiveTabChange(tab.id)}
            >
              <Icon aria-hidden="true" size={15} /> {tab.label}
              {count === undefined ? null : <span>{count}</span>}
            </button>
          );
        })}
        <div className="inspector-meta">
          Hash <code>{props.design.design_hash.slice(0, 10)}</code>
        </div>
      </div>
      <div id={activePanelId} role="tabpanel" aria-labelledby={`inspector-tab-${activeTab}`} className="inspector-content">
        {activeTab !== "parts" && activeTab !== "production" ? (
          <p className="production-preview-note" role="status">
            Klientberäknad förhandsvisning – frisläppningsbar BOM, nesting och CAM genereras endast av serverns produktionsjobb.
          </p>
        ) : null}
        {activeTab === "parts" ? <PartTable {...props} /> : null}
        {activeTab === "bom" ? <BomTable {...props} /> : null}
        {activeTab === "nesting" ? <NestingView {...props} /> : null}
        {activeTab === "operations" ? <OperationsTable {...props} /> : null}
        <div className="production-panel" hidden={activeTab !== "production"}>
          <ProductionWorkflow
            spec={props.spec}
            design={props.design}
            onSummaryChange={props.onProductionSummaryChange}
          />
        </div>
      </div>
    </section>
  );
}
