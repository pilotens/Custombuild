"use client";

import NextImage from "next/image";
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Box,
  Check,
  Image as ImageIcon,
  LayoutGrid,
  MessageSquareText,
  PackageOpen,
  Ruler,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import { useMemo, useRef, useState, type ReactNode } from "react";
import {
  DEFAULT_PLANNING_BRIEF,
  PLANNING_DIMENSION_LIMITS,
  PLANNING_LABELS,
  recommendedTemplateId,
  templateWithPlanningBrief,
  type FurniturePlanningBrief,
  type PlanningPriority,
  type PlanningSpace,
  type PlanningStyle,
  type PlanningUse,
} from "@/lib/furniture-planning";
import { useDialogFocus } from "@/lib/use-dialog-focus";
import {
  FURNITURE_TEMPLATES,
  furnitureTemplate,
  templatePreviewGeometry,
  validateTemplatePreview,
  type FurnitureTemplate,
  type FurnitureTemplateId,
} from "@/lib/furniture-templates";
import styles from "./template-picker-studio.module.css";

export interface TemplatePickerProps {
  open: boolean;
  selectedId: FurnitureTemplateId;
  required?: boolean;
  initialBrief?: FurniturePlanningBrief;
  onBriefChange?: (brief: FurniturePlanningBrief) => void;
  onSelect: (template: FurnitureTemplate, brief: FurniturePlanningBrief) => void;
  onUploadImage: (brief: FurniturePlanningBrief) => void;
  onClose: () => void;
  presentation?: "modal" | "embedded";
}

interface Choice<T extends string> {
  id: T;
  title: string;
  description: string;
  icon: ReactNode;
}

type ExploreView = "home" | "designs" | "needs";
type PlanningDimension = "width_mm" | "height_mm" | "depth_mm";
type DimensionDrafts = Record<PlanningDimension, string>;

const GALLERY_TEMPLATE_IDS: readonly FurnitureTemplateId[] = [
  "shelving",
  "wall-library",
  "room-divider",
];

const TEMPLATE_IMAGE_PATHS: Partial<Record<FurnitureTemplateId, string>> = {
  shelving: "/assets/inspiration/concept-airy.jpg",
  "wall-library": "/assets/inspiration/concept-base.jpg",
  "room-divider": "/assets/inspiration/concept-rhythmic.jpg",
};

const SPACE_CHOICES: readonly Choice<PlanningSpace>[] = [
  { id: "wall", title: "Mot en vägg", description: "Förankrad längs en väggyta.", icon: <Box aria-hidden="true" /> },
  { id: "alcove", title: "I en nisch", description: "Måttanpassad mellan två sidor.", icon: <LayoutGrid aria-hidden="true" /> },
  { id: "freestanding", title: "Fristående", description: "Kräver extra stabilitetskontroll.", icon: <Sparkles aria-hidden="true" /> },
  { id: "unsure", title: "Vet inte ännu", description: "Välj placering senare i Studio.", icon: <MessageSquareText aria-hidden="true" /> },
];

const USE_CHOICES: readonly Choice<PlanningUse>[] = [
  { id: "books", title: "Mest böcker", description: "Tätare hyllor och hög last.", icon: <BookOpen aria-hidden="true" /> },
  { id: "display", title: "Visa saker", description: "Luftigare fack och blickfång.", icon: <Sparkles aria-hidden="true" /> },
  { id: "mixed", title: "Öppet och dolt", description: "Hyllor med sammanhållen nederdel.", icon: <PackageOpen aria-hidden="true" /> },
  { id: "concealed", title: "Dold förvaring", description: "Prioritera skåpsvolym.", icon: <LayoutGrid aria-hidden="true" /> },
];

const PRIORITY_CHOICES: readonly Choice<PlanningPriority>[] = [
  { id: "balanced", title: "Balans", description: "Jämn balans mellan uttryck och kapacitet.", icon: <Check aria-hidden="true" /> },
  { id: "capacity", title: "Maximal förvaring", description: "Mer kapacitet och högre last.", icon: <PackageOpen aria-hidden="true" /> },
  { id: "flexibility", title: "Flexibel indelning", description: "Lättare att justera över tid.", icon: <LayoutGrid aria-hidden="true" /> },
  { id: "budget", title: "Enkel konstruktion", description: "Färre delar och enkel bearbetning.", icon: <Ruler aria-hidden="true" /> },
];

const STYLE_CHOICES: readonly Choice<PlanningStyle>[] = [
  { id: "light", title: "Ljust och luftigt", description: "Lugn rytm och öppna fack.", icon: <Sparkles aria-hidden="true" /> },
  { id: "natural", title: "Naturligt trä", description: "Materialet står för karaktären.", icon: <Box aria-hidden="true" /> },
  { id: "calm", title: "Lugnt och enhetligt", description: "Symmetriska proportioner.", icon: <Check aria-hidden="true" /> },
  { id: "contrast", title: "Tydlig kontrast", description: "Markera nederdel eller utvalda fack.", icon: <LayoutGrid aria-hidden="true" /> },
];

const DIMENSION_LIMITS: Record<PlanningDimension, { label: string; min: number; max: number }> = {
  width_mm: {
    label: "Bredd",
    min: PLANNING_DIMENSION_LIMITS.width_mm.minimum,
    max: PLANNING_DIMENSION_LIMITS.width_mm.maximum,
  },
  height_mm: {
    label: "Höjd",
    min: PLANNING_DIMENSION_LIMITS.height_mm.minimum,
    max: PLANNING_DIMENSION_LIMITS.height_mm.maximum,
  },
  depth_mm: {
    label: "Djup",
    min: PLANNING_DIMENSION_LIMITS.depth_mm.minimum,
    max: PLANNING_DIMENSION_LIMITS.depth_mm.maximum,
  },
};

function classes(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

function dimensionError(value: string, min: number, max: number): string | undefined {
  if (value.trim() === "") return "Ange ett mått i millimeter.";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "Ange endast siffror.";
  if (parsed < min || parsed > max) {
    return `Tillåtet intervall är ${min.toLocaleString("sv-SE")}–${max.toLocaleString("sv-SE")} mm.`;
  }
  if (!Number.isInteger(parsed * 1_000)) {
    return "Ange högst tre decimaler (en tusendels millimeter).";
  }
  return undefined;
}

function inferIntent(text: string): Partial<FurniturePlanningBrief> {
  const value = text.toLocaleLowerCase("sv-SE");
  const patch: Partial<FurniturePlanningBrief> = {};

  if (/fristående|rumsavdel/.test(value)) patch.space = "freestanding";
  else if (/nisch|mellan två vägg/.test(value)) patch.space = "alcove";
  else if (/vägg|golv till tak/.test(value)) patch.space = "wall";

  if (/dold|stängd|skåp|dörr/.test(value) && /öppen|visa|bok|böck|bibliotek/.test(value)) patch.primaryUse = "mixed";
  else if (/dold|stängd|skåp|dörr/.test(value)) patch.primaryUse = "concealed";
  else if (/bok|böck|bibliotek/.test(value)) patch.primaryUse = "books";
  else if (/visa|display|konst|prydnad|vas/.test(value)) patch.primaryUse = "display";

  if (/max|mycket|mest|kapacitet/.test(value)) patch.priority = "capacity";
  else if (/flex|juster|föränder/.test(value)) patch.priority = "flexibility";
  else if (/enkel|budget|få delar/.test(value)) patch.priority = "budget";
  else if (/balans|balanser/.test(value)) patch.priority = "balanced";

  if (/ljus|luftig/.test(value)) patch.style = "light";
  else if (/natur|trä|ek|björk/.test(value)) patch.style = "natural";
  else if (/lugn|enhet|symmet/.test(value)) patch.style = "calm";
  else if (/mörk|kontrast|marker/.test(value)) patch.style = "contrast";

  return patch;
}

function choiceButton<T extends string>(
  choice: Choice<T>,
  value: T,
  onChange: (value: T) => void,
) {
  const selected = value === choice.id;
  return (
    <button
      key={choice.id}
      type="button"
      className={classes(styles.choice, selected && styles.choiceSelected)}
      aria-pressed={selected}
      onClick={() => onChange(choice.id)}
    >
      <span className={styles.choiceIcon}>{choice.icon}</span>
      <span className={styles.choiceCopy}><strong>{choice.title}</strong><small>{choice.description}</small></span>
      {selected ? <Check className={styles.choiceCheck} aria-hidden="true" size={16} /> : null}
    </button>
  );
}

export function TemplateIllustration({ template }: { template: FurnitureTemplate }) {
  const geometry = templatePreviewGeometry(template);
  const errors = validateTemplatePreview(template);
  const { x, top, width, openHeight, baseHeight, openColumns, shelfLines, baseColumns, hasBase, showBackEdge } = geometry;
  return (
    <svg
      className="template-illustration"
      viewBox="0 0 260 160"
      role="img"
      aria-label={`Kontrollerad förhandsvisning av ${template.name}`}
      data-preview={template.preview}
      data-open-columns={openColumns}
      data-shelf-lines={shelfLines}
      data-base-columns={baseColumns}
      data-has-base={String(hasBase)}
      data-visual-errors={errors.length}
    >
      <defs>
        <linearGradient id={`wood-${template.id}`} x1="0" x2="1">
          <stop offset="0" stopColor="#f7f3ea" />
          <stop offset="1" stopColor="#ded6c7" />
        </linearGradient>
      </defs>
      <ellipse cx="130" cy="146" rx="94" ry="8" fill="rgba(47,58,52,.08)" />
      <rect x={x} y={top} width={width} height={openHeight + baseHeight} rx="1.5" fill={`url(#wood-${template.id})`} stroke="#9f9688" strokeWidth="1.5" />
      {Array.from({ length: openColumns - 1 }, (_, index) => (
        <line key={`c-${index}`} x1={x + width * (index + 1) / openColumns} x2={x + width * (index + 1) / openColumns} y1={top} y2={top + openHeight} stroke="#aaa092" strokeWidth="2" />
      ))}
      {Array.from({ length: shelfLines }, (_, index) => (
        <line key={`r-${index}`} x1={x} x2={x + width} y1={top + openHeight * (index + 1) / (shelfLines + 1)} y2={top + openHeight * (index + 1) / (shelfLines + 1)} stroke="#aaa092" strokeWidth="2" />
      ))}
      {hasBase ? (
        <>
          <rect x={x} y={top + openHeight} width={width} height={baseHeight} fill="#b9aa96" stroke="#8b7d6c" strokeWidth="1.5" />
          {Array.from({ length: baseColumns - 1 }, (_, index) => (
            <line key={`b-${index}`} x1={x + width * (index + 1) / baseColumns} x2={x + width * (index + 1) / baseColumns} y1={top + openHeight} y2={top + openHeight + baseHeight} stroke="#8b7d6c" strokeWidth="1.5" />
          ))}
        </>
      ) : null}
      {showBackEdge ? <line x1={x + 6} x2={x + 6} y1={top + 5} y2={top + openHeight - 4} stroke="rgba(255,255,255,.8)" strokeWidth="2" /> : null}
    </svg>
  );
}

function InspirationVisual({
  template,
  src,
  priority = false,
}: {
  template: FurnitureTemplate;
  src?: string;
  priority?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <span className={styles.visual}>
      {src && !failed ? (
        <NextImage
          className={styles.visualImage}
          src={src}
          alt={`Inspirationsbild för ${template.name}`}
          fill
          priority={priority}
          sizes="(max-width: 760px) 100vw, 42vw"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className={styles.illustrationFallback}><TemplateIllustration template={template} /></span>
      )}
      <span className={styles.visualCaption}>Inspirationsbild – exakt modell visas i Studio</span>
    </span>
  );
}

function TemplateCard({
  template,
  selected,
  onChoose,
}: {
  template: FurnitureTemplate;
  selected: boolean;
  onChoose: () => void;
}) {
  const dimensions = `${Number(template.patch.width_mm ?? 0).toLocaleString("sv-SE")} × ${Number(template.patch.height_mm ?? 0).toLocaleString("sv-SE")} × ${Number(template.patch.depth_mm ?? 0).toLocaleString("sv-SE")} mm`;
  return (
    <button
      type="button"
      className={classes(styles.templateCard, "template-card", selected && styles.templateCardSelected)}
      aria-pressed={selected}
      onClick={onChoose}
    >
      <InspirationVisual template={template} src={TEMPLATE_IMAGE_PATHS[template.id]} />
      <span className={styles.templateCardBody}>
        <span className={styles.templateCardHeading}>
          <strong>{template.name}</strong>
          {selected ? <span className={styles.selectedMark} aria-label="Vald"><Check aria-hidden="true" size={16} /></span> : null}
        </span>
        <span>{template.description}</span>
        <small>{dimensions}</small>
        <span className={styles.tagRow}>
          <span>{template.productionLevel === "screened" ? "Konstruktionsscreenad startmodell" : "Koncept · fortsatt kontroll krävs"}</span>
          <span>{template.feature}</span>
        </span>
        {template.limitation ? <small className={styles.limitation}>{template.limitation}</small> : null}
      </span>
    </button>
  );
}

export function TemplatePicker(props: TemplatePickerProps) {
  if (!props.open) return null;
  return <OpenTemplatePicker {...props} />;
}

function OpenTemplatePicker({
  selectedId,
  required = false,
  initialBrief,
  onBriefChange,
  onSelect,
  onUploadImage,
  onClose,
  presentation = "modal",
}: TemplatePickerProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const initial = initialBrief ?? { ...DEFAULT_PLANNING_BRIEF, selectedTemplateId: selectedId };
  const initialGalleryId = GALLERY_TEMPLATE_IDS.includes(selectedId) ? selectedId : recommendedTemplateId(initial);
  const [view, setView] = useState<ExploreView>("home");
  const [brief, setBrief] = useState<FurniturePlanningBrief>(initial);
  const [selectedTemplateId, setSelectedTemplateId] = useState<FurnitureTemplateId>(initialGalleryId);
  const [intent, setIntent] = useState("");
  const [suggestionsVisible, setSuggestionsVisible] = useState(false);
  const [planningError, setPlanningError] = useState<string>();
  const [dimensionDrafts, setDimensionDrafts] = useState<DimensionDrafts>({
    width_mm: String(initial.width_mm),
    height_mm: String(initial.height_mm),
    depth_mm: String(initial.depth_mm),
  });

  const modal = presentation === "modal";
  useDialogFocus(modal, dialogRef, onClose, !required);

  const dimensionErrors = {
    width_mm: dimensionError(dimensionDrafts.width_mm, DIMENSION_LIMITS.width_mm.min, DIMENSION_LIMITS.width_mm.max),
    height_mm: dimensionError(dimensionDrafts.height_mm, DIMENSION_LIMITS.height_mm.min, DIMENSION_LIMITS.height_mm.max),
    depth_mm: dimensionError(dimensionDrafts.depth_mm, DIMENSION_LIMITS.depth_mm.min, DIMENSION_LIMITS.depth_mm.max),
  };
  const dimensionsValid = !dimensionErrors.width_mm && !dimensionErrors.height_mm && !dimensionErrors.depth_mm;
  const selectedTemplate = furnitureTemplate(selectedTemplateId);
  const galleryTemplates = GALLERY_TEMPLATE_IDS.map(furnitureTemplate);
  const suggestedTemplates = useMemo(() => {
    const first = recommendedTemplateId(brief);
    return [furnitureTemplate(first), ...FURNITURE_TEMPLATES.filter((template) => template.id !== first)].slice(0, 3);
  }, [brief]);

  const updateBrief = (patch: Partial<FurniturePlanningBrief>): FurniturePlanningBrief => {
    const next = { ...brief, ...patch };
    setBrief(next);
    onBriefChange?.(next);
    return next;
  };

  const dimensionsForTemplate = (id: FurnitureTemplateId) => {
    const template = furnitureTemplate(id);
    return {
      width_mm: Number(template.patch.width_mm),
      height_mm: Number(template.patch.height_mm),
      depth_mm: Number(template.patch.depth_mm),
    };
  };

  const openDesignGallery = () => {
    setPlanningError(undefined);
    setView("designs");
    updateBrief({
      startMode: "template",
      selectedTemplateId,
      ...(brief.dimensionsConfirmed ? {} : dimensionsForTemplate(selectedTemplateId)),
    });
  };

  const chooseDesign = (id: FurnitureTemplateId) => {
    setPlanningError(undefined);
    setSelectedTemplateId(id);
    updateBrief({
      startMode: "template",
      selectedTemplateId: id,
      ...(brief.dimensionsConfirmed ? {} : dimensionsForTemplate(id)),
    });
  };

  const openInStudio = (templateId = selectedTemplateId, currentBrief = brief) => {
    const next = {
      ...currentBrief,
      startMode: "template" as const,
      selectedTemplateId: templateId,
    };
    setBrief(next);
    onBriefChange?.(next);
    try {
      onSelect(templateWithPlanningBrief(furnitureTemplate(templateId), next), next);
      setPlanningError(undefined);
    } catch (error) {
      setPlanningError(error instanceof Error ? error.message : "Startmodellen kan inte skapas med de valda måtten.");
    }
  };

  const startFromImage = () => {
    const next = updateBrief({ startMode: "reference" });
    onUploadImage(next);
  };

  const startFromScratch = () => {
    const next = updateBrief({ startMode: "scratch", selectedTemplateId: "shelving" });
    onSelect(templateWithPlanningBrief(furnitureTemplate("shelving"), next), next);
  };

  const showSuggestions = () => {
    if (!dimensionsValid) return;
    setPlanningError(undefined);
    const dimensions = {
      width_mm: Number(dimensionDrafts.width_mm),
      height_mm: Number(dimensionDrafts.height_mm),
      depth_mm: Number(dimensionDrafts.depth_mm),
    };
    const next = updateBrief({
      ...inferIntent(intent),
      ...dimensions,
      dimensionsConfirmed: true,
      startMode: "recommended",
    });
    const first = recommendedTemplateId(next);
    setSelectedTemplateId(first);
    setSuggestionsVisible(true);
  };

  const content = (
    <section
      ref={dialogRef}
      tabIndex={modal ? -1 : undefined}
      className={classes(styles.root, "template-picker", modal ? styles.modal : styles.embedded)}
      role={modal ? "dialog" : undefined}
      aria-modal={modal ? true : undefined}
      aria-labelledby="explore-title"
      aria-describedby="explore-intro"
      data-presentation={presentation}
    >
      <header className={styles.header}>
        <button
          type="button"
          className={classes(styles.backButton, view === "home" && styles.hiddenAction)}
          onClick={() => { setView("home"); setSuggestionsVisible(false); }}
          aria-hidden={view === "home"}
          tabIndex={view === "home" ? -1 : 0}
        >
          <ArrowLeft aria-hidden="true" size={18} /> Till start
        </button>
        <p className={styles.brand}><span aria-hidden="true">◇</span> Custombuild</p>
        {modal && !required ? (
          <button type="button" className={styles.closeButton} aria-label="Stäng Explore" onClick={onClose}>
            <X aria-hidden="true" size={20} />
          </button>
        ) : <span className={styles.headerSpacer} aria-hidden="true" />}
      </header>

      {planningError ? <p role="alert" className={styles.fieldError}>{planningError}</p> : null}

      {view === "home" ? (
        <div className={styles.home}>
          <div className={styles.homeContent}>
            <p className={styles.eyebrow}>Explore</p>
            <h2 id="explore-title">Vad vill du skapa?</h2>
            <p id="explore-intro" className={styles.lead}>Välj en startpunkt. Möbeln öppnas direkt i Studio, där du formar den och Custombuild håller reda på vad som går att bygga.</p>
            <div className={styles.routeGrid} aria-label="Välj hur du vill börja">
              <button type="button" className={styles.routeCard} onClick={openDesignGallery}>
                <span className={styles.routeIcon}><LayoutGrid aria-hidden="true" /></span>
                <span><strong>Välj en design</strong><small>Bläddra bland verkliga startmodeller och forma dem vidare.</small></span>
                <ArrowRight aria-hidden="true" size={20} />
              </button>
              <button type="button" className={styles.routeCard} onClick={() => setView("needs")}>
                <span className={styles.routeIcon}><MessageSquareText aria-hidden="true" /></span>
                <span><strong>Skapa med Custombuild</strong><small>Beskriv behovet och få tre strukturerade startförslag.</small></span>
                <ArrowRight aria-hidden="true" size={20} />
              </button>
              <button type="button" className={styles.routeCard} onClick={startFromImage}>
                <span className={styles.routeIcon}><ImageIcon aria-hidden="true" /></span>
                <span><strong>Utgå från en bild</strong><small>Ladda upp inspiration och verifiera tolkningen innan produktion.</small></span>
                <Upload aria-hidden="true" size={20} />
              </button>
              <button type="button" className={styles.routeCard} onClick={startFromScratch}>
                <span className={styles.routeIcon}><Sparkles aria-hidden="true" /></span>
                <span><strong>Börja tomt</strong><small>Öppna en tom, screenad hyllstomme och lägg själv till fack och hyllnivåer.</small></span>
                <ArrowRight aria-hidden="true" size={20} />
              </button>
            </div>
          </div>
          <div className={styles.heroVisual}>
            <InspirationVisual template={furnitureTemplate("wall-library")} src="/assets/inspiration/explore-hero.jpg" priority />
            <div className={styles.heroStatement}>
              <span>Studio</span>
              <strong>Här är din möbel. Forma den.</strong>
              <small>Exakta mått, delar och kontroller visas när du öppnar modellen.</small>
            </div>
          </div>
        </div>
      ) : null}

      {view === "designs" ? (
        <div className={styles.page}>
          <div className={styles.pageHeading}>
            <p className={styles.eyebrow}>Välj en design</p>
            <h2 id="explore-title">Välj en startmodell att forma vidare.</h2>
            <p id="explore-intro">Kortens miljöbilder är inspiration. När du öppnar Studio visas mallens verkliga parametriska geometri.</p>
          </div>
          <div className={styles.summaryStrip} aria-label="Vald startmodell">
            <span><LayoutGrid aria-hidden="true" size={18} /><strong>{selectedTemplate.name}</strong><small>{selectedTemplate.archetypeLabel}</small></span>
            <span><Ruler aria-hidden="true" size={18} /><strong>{Number(selectedTemplate.patch.width_mm).toLocaleString("sv-SE")} × {Number(selectedTemplate.patch.height_mm).toLocaleString("sv-SE")} × {Number(selectedTemplate.patch.depth_mm).toLocaleString("sv-SE")} mm</strong><small>Bredd × höjd × djup</small></span>
            <span><Check aria-hidden="true" size={18} /><strong>{selectedTemplate.productionLevel === "screened" ? "Screenad grundstomme" : "Konceptmodell"}</strong><small>Full kontroll sker i Check</small></span>
          </div>
          <div className={styles.templateGrid}>
            {galleryTemplates.map((template) => (
              <TemplateCard key={template.id} template={template} selected={selectedTemplateId === template.id} onChoose={() => chooseDesign(template.id)} />
            ))}
          </div>
          <footer className={styles.pageFooter}>
            <p><Check aria-hidden="true" size={17} /> Inget produktionsunderlag skapas från inspirationsbilden. Studio använder alltid den verkliga modellen.</p>
            <button type="button" className={styles.primaryButton} onClick={() => openInStudio()}>
              Öppna {selectedTemplate.name} i Studio <ArrowRight aria-hidden="true" size={18} />
            </button>
          </footer>
        </div>
      ) : null}

      {view === "needs" ? (
        <div className={styles.page}>
          <div className={styles.pageHeading}>
            <p className={styles.eyebrow}>Skapa med Custombuild</p>
            <h2 id="explore-title">Vad ska möbeln göra för dig?</h2>
            <p id="explore-intro">Beskriv med egna ord eller välj det viktigaste. Texten matchas lokalt mot strukturerade behov; den skapar inte CNC-geometri.</p>
          </div>
          <div className={styles.needsLayout}>
            <div className={styles.needsForm}>
              <label className={styles.intentField}>
                <span>Beskriv ditt behov</span>
                <textarea
                  value={intent}
                  onChange={(event) => setIntent(event.target.value)}
                  placeholder="Exempel: Ett väggbibliotek för mest böcker, några större föremål och dold förvaring längst ned."
                  rows={4}
                />
                <small>Beskrivningen översätts till valen nedan. Den sparade briefen är strukturerad och går att ändra i Studio.</small>
              </label>
              <fieldset className={styles.dimensionGroup}>
                <legend>Dina exakta yttermått</legend>
                {(Object.keys(DIMENSION_LIMITS) as PlanningDimension[]).map((field) => {
                  const limits = DIMENSION_LIMITS[field];
                  const errorId = `explore-${field}-error`;
                  return (
                    <label key={field}>
                      <span>{limits.label}</span>
                      <span className={styles.dimensionInput}>
                        <input
                          type="number"
                          inputMode="decimal"
                          min={limits.min}
                          max={limits.max}
                          step={0.001}
                          value={dimensionDrafts[field]}
                          aria-label={`Planerad ${limits.label.toLocaleLowerCase("sv-SE")}`}
                          aria-invalid={Boolean(dimensionErrors[field])}
                          aria-describedby={dimensionErrors[field] ? errorId : undefined}
                          onChange={(event) => {
                            setPlanningError(undefined);
                            setDimensionDrafts((current) => ({ ...current, [field]: event.target.value }));
                          }}
                        />
                        <small>mm</small>
                      </span>
                      {dimensionErrors[field] ? <small id={errorId} className={styles.fieldError}>{dimensionErrors[field]}</small> : null}
                    </label>
                  );
                })}
                <small className={styles.truthNote}>Måtten används i Studio utan avrundning, med högst tre decimaler.</small>
              </fieldset>
              <div className={styles.choiceSections}>
                <fieldset>
                  <legend>Placering</legend>
                  <div>{SPACE_CHOICES.map((choice) => choiceButton(choice, brief.space, (space) => updateBrief({ space })))}</div>
                </fieldset>
                <fieldset>
                  <legend>Huvudsaklig användning</legend>
                  <div>{USE_CHOICES.map((choice) => choiceButton(choice, brief.primaryUse, (primaryUse) => updateBrief({ primaryUse })))}</div>
                </fieldset>
                <fieldset>
                  <legend>Viktigast för dig</legend>
                  <div>{PRIORITY_CHOICES.map((choice) => choiceButton(choice, brief.priority, (priority) => updateBrief({ priority })))}</div>
                </fieldset>
                <fieldset>
                  <legend>Uttryck</legend>
                  <div>{STYLE_CHOICES.map((choice) => choiceButton(choice, brief.style, (style) => updateBrief({ style })))}</div>
                </fieldset>
              </div>
              <button type="button" className={styles.primaryButton} disabled={!dimensionsValid} onClick={showSuggestions}>
                Visa tre startförslag <ArrowRight aria-hidden="true" size={18} />
              </button>
            </div>

            <aside className={styles.suggestions} aria-live="polite">
              {!suggestionsVisible ? (
                <div className={styles.suggestionEmpty}>
                  <Sparkles aria-hidden="true" size={28} />
                  <h3>Tre verkliga startmodeller</h3>
                  <p>Förslagen bygger på dina val och sorterar endast befintliga, parametriska mallar.</p>
                </div>
              ) : (
                <>
                  <div className={styles.suggestionHeading}>
                    <span>Matchade startpunkter</span>
                    <strong>{PLANNING_LABELS.primaryUse[brief.primaryUse]} · {PLANNING_LABELS.priority[brief.priority]}</strong>
                  </div>
                  <div className={styles.suggestionList}>
                    {suggestedTemplates.map((template, index) => (
                      <button
                        key={template.id}
                        type="button"
                        className={classes(styles.suggestionCard, selectedTemplateId === template.id && styles.suggestionCardSelected)}
                        aria-pressed={selectedTemplateId === template.id}
                        onClick={() => chooseDesign(template.id)}
                      >
                        <span className={styles.suggestionNumber}>{String(index + 1).padStart(2, "0")}</span>
                        <span><strong>{template.name}</strong><small>{template.description}</small></span>
                        {selectedTemplateId === template.id ? <Check aria-label="Vald" size={17} /> : <ArrowRight aria-hidden="true" size={17} />}
                      </button>
                    ))}
                  </div>
                  <button type="button" className={styles.primaryButton} onClick={() => openInStudio()}>
                    Öppna vald modell i Studio <ArrowRight aria-hidden="true" size={18} />
                  </button>
                  <p className={styles.truthNote}>Förslaget är en startpunkt. Den parametriska motorn och konstruktionskontrollen är fortsatt auktoritativa.</p>
                </>
              )}
            </aside>
          </div>
        </div>
      ) : null}
    </section>
  );

  if (!modal) return content;
  return (
    <div className={classes(styles.backdrop, "template-picker-backdrop", "cb-planner-backdrop")} role="presentation" data-modal-root="true">
      {content}
    </div>
  );
}
