"use client";

import { AlertTriangle, FileArchive, ShieldCheck, X } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import type { CustombuildApiClient } from "@/lib/api-client";
import { useDialogFocus } from "@/lib/use-dialog-focus";
import type { ResolvedDesign, DesignSpec } from "@/lib/design-types";
import { hasPartCustomization, isReferenceImageDesign, type FurnitureTemplate } from "@/lib/furniture-templates";
import type { WorkspaceIdentity } from "@/lib/workspace-draft-storage";
import { ProjectHeader } from "./design-system";
import { ProductionWorkflow, type ProductionSummary } from "./production-workflow";
import type { WorkshopContextDraftState } from "./workshop-context-editor";

interface ProductionDrawerProps {
  open: boolean;
  presentation?: "modal" | "embedded";
  spec: DesignSpec;
  design: ResolvedDesign;
  template: FurnitureTemplate;
  onClose: () => void;
  onOpenTemplates: () => void;
  onApplyDesignChange?: (patch: Partial<DesignSpec>, reason: string) => void;
  onRequestServerPreviewRetry?: () => void;
  projectId?: string;
  projectName?: string;
  principal?: WorkspaceIdentity;
  apiClient?: CustombuildApiClient;
  workshopContextDraftState?: WorkshopContextDraftState;
  onWorkshopContextDraftStateChange?: (state: WorkshopContextDraftState) => void;
}

function productionStateLabel(summary: ProductionSummary): string {
  if (summary.stale) return "Behöver uppdateras";
  if (summary.designReviewReady) return "Granskningsklar";
  switch (summary.status) {
    case "syncing":
      return "Förbereder";
    case "draft":
      return "Kontrollerar";
    case "design_validated":
      return "Kontrollerad";
    case "approved":
    case "cam_validated":
    case "released":
      return "Kontrollerad";
    case "superseded":
    case "archived":
      return "Behöver uppdateras";
    default:
      return "Inte skapad";
  }
}

export function ProductionDrawer({
  open,
  presentation = "modal",
  spec,
  design,
  template,
  onClose,
  onOpenTemplates,
  onApplyDesignChange,
  onRequestServerPreviewRetry,
  projectId,
  projectName,
  principal,
  apiClient,
  workshopContextDraftState,
  onWorkshopContextDraftStateChange,
}: ProductionDrawerProps) {
  const [summary, setSummary] = useState<ProductionSummary>({ status: "unsaved", stale: false });
  const dialogRef = useRef<HTMLElement>(null);
  const updateSummary = useCallback((next: ProductionSummary) => setSummary(next), []);
  useDialogFocus(open && presentation === "modal", dialogRef, onClose);

  const referenceImage = isReferenceImageDesign(spec);
  const partCustomization = hasPartCustomization(spec);
  // The furniture family is a safety boundary too: a stale or tampered local
  // template id must never make wall-library geometry production-capable.
  const conceptTemplate = template.productionLevel === "concept" || spec.furniture_type === "wall_library";
  const productionBlocked = conceptTemplate || referenceImage || partCustomization;
  const limitation = referenceImage
    ? "Modellen är tolkad från en referensbild. Bilden visar inte säkra verkliga mått, materialtjocklek, beslag, infästningar eller dolda förband. Bekräfta konstruktionen manuellt innan ett underlag kan skapas."
    : partCustomization
      ? "En eller flera fria deländringar saknar en säker parametrisk transformation. Stödda flyttar och sammanslagningar regenererar redan anslutande brädor; övriga ändringar blockeras tills förband, upplag och bärighet kan räknas om på servern."
      : template.limitation ?? (
        spec.furniture_type === "wall_library"
          ? "Väggbibliotekets gångjärn, beslag, borrbilder, frontspel och limfria mekaniska retention är ännu inte versionsbundna och verifierade."
          : undefined
      );

  const content = (
    <section
      ref={dialogRef}
      tabIndex={presentation === "modal" ? -1 : undefined}
      className={`production-drawer ${presentation === "embedded" ? "production-drawer-embedded" : ""}`}
      {...(presentation === "modal"
        ? { role: "dialog" as const, "aria-modal": true, "aria-labelledby": "production-drawer-title" }
        : { "aria-labelledby": "production-drawer-title" })}
    >
      <div className="production-drawer-header">
        <span className="production-drawer-icon"><FileArchive aria-hidden="true" size={20} /></span>
        <ProjectHeader
          headingLevel={2}
          eyebrow={presentation === "embedded" ? "Underlag · Aktuell konstruktion" : "Underlag"}
          title={<span id="production-drawer-title">Skapa underlag</span>}
          description={`${projectName ?? "Ditt projekt"} · ${template.name}`}
        />
        <span className={`production-drawer-state ${summary.stale ? "stale" : ""}`}>{productionStateLabel(summary)}</span>
        {presentation === "modal" ? (
          <button type="button" className="production-drawer-close" aria-label="Stäng underlaget och återgå till modellen" onClick={onClose}>
            <X aria-hidden="true" size={21} />
            <span>Stäng</span>
          </button>
        ) : null}
      </div>

      <div className="production-drawer-scroll" tabIndex={0} aria-label="Underlagets innehåll">
        {productionBlocked ? (
          <div className="concept-production-block" role="alert">
            <AlertTriangle aria-hidden="true" size={28} />
            <div>
              <p className="eyebrow">Kan inte skapa underlag</p>
              <h3>{referenceImage ? "Referensbilden måste konstruktionsgranskas" : partCustomization ? "Deländringarna måste konstruktionsgranskas" : "Den här mallen är fortfarande en konceptmodell"}</h3>
              <p>{limitation}</p>
              <p>Du kan fortsätta utforma och kontrollera proportionerna, men inget designgranskningspaket skapas för den här mallen.</p>
              <div>
                <button type="button" onClick={onClose}>Fortsätt utforma</button>
                <button type="button" onClick={() => { onClose(); onOpenTemplates(); }}>Välj screenad mall</button>
              </div>
            </div>
          </div>
        ) : (
          <>
            <div className="screened-production-note" role="status">
              <ShieldCheck aria-hidden="true" size={18} />
              <span><strong>Redo för kontroll.</strong> Vi kontrollerar den senaste designen innan ett designgranskningspaket skapas.</span>
            </div>
            <ProductionWorkflow
              key={principal
                ? `${principal.organization_id}:${principal.user_id}:${projectId ?? ""}:${spec.design_id}`
                : `anonymous:${spec.design_id}`}
              active={open}
              spec={spec}
              design={design}
              projectName={projectName}
              projectId={projectId}
              principal={principal}
              apiClient={apiClient}
              templateId={template.id}
              onSummaryChange={updateSummary}
              onApplyDesignChange={onApplyDesignChange}
              onRequestServerPreviewRetry={onRequestServerPreviewRetry}
              workshopContextDraftState={workshopContextDraftState}
              onWorkshopContextDraftStateChange={onWorkshopContextDraftStateChange}
              showRevisionHistory={presentation === "embedded"}
            />
          </>
        )}
      </div>
    </section>
  );

  if (presentation === "embedded") return open ? content : null;

  return (
    <div
      className="production-drawer-backdrop"
      role="presentation"
      data-modal-root="true"
      hidden={!open}
      aria-hidden={!open}
    >
      <button
        type="button"
        className="production-drawer-scrim"
        aria-label="Stäng underlaget och återgå till modellen"
        onClick={onClose}
      />
      {content}
    </div>
  );
}
