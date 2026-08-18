"use client";

import { AlertTriangle, Check, Image as ImageIcon, LoaderCircle, Ruler, Sparkles, Upload, X } from "lucide-react";
import { useEffect, useRef, useState, type ChangeEvent, type ClipboardEvent, type DragEvent, type KeyboardEvent } from "react";
import { DESIGN_CONSTRAINTS } from "@/lib/design-constraints";
import {
  analyzeReferencePixels,
  draftFromReferenceAnalysis,
  maximumReferenceBaseCabinetHeightMm,
  REFERENCE_IMAGE_INFERENCE_LIMITS,
  referenceDraftValidationError,
  referenceResult,
  type ReferenceAssetInspection,
  type ReferenceImageAnalysis,
  type ReferenceImageDraft,
  type ReferenceImageResult,
} from "@/lib/reference-image";
import { useDialogFocus } from "@/lib/use-dialog-focus";

interface ReferenceImageImporterProps {
  open: boolean;
  onClose: () => void;
  onInspect: (file: File) => Promise<ReferenceAssetInspection>;
  onApply: (result: ReferenceImageResult) => void;
}

const ACCEPTED_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
const MAX_FILE_BYTES = 12 * 1024 * 1024;

interface ClipboardPayload {
  files: ArrayLike<File>;
  items: ArrayLike<{
    kind: string;
    type: string;
    getAsFile: () => File | null;
  }>;
}

export function imageFileFromClipboard(clipboardData: ClipboardPayload): File | undefined {
  const directFile = Array.from(clipboardData.files).find((file) => ACCEPTED_TYPES.has(file.type));
  if (directFile) return directFile;
  for (const item of Array.from(clipboardData.items)) {
    if (item.kind !== "file" || !ACCEPTED_TYPES.has(item.type)) continue;
    const file = item.getAsFile();
    if (file) return file;
  }
  return undefined;
}

interface NumberControlProps {
  label: string;
  value: number;
  unit?: string;
  minimum: number;
  maximum: number;
  onChange: (value: number) => void;
}

function NumberControl({ label, value, unit, minimum, maximum, onChange }: NumberControlProps) {
  const [draftValue, setDraftValue] = useState<string>();
  const [validationMessage, setValidationMessage] = useState<string>();
  const inputValue = draftValue ?? String(value);

  const commit = () => {
    const next = Number(inputValue);
    if (!inputValue.trim() || !Number.isFinite(next)) {
      setValidationMessage(`Ange ett tal mellan ${minimum} och ${maximum}.`);
      return;
    }
    if (next < minimum || next > maximum) {
      setValidationMessage(`Värdet måste vara mellan ${minimum} och ${maximum}.`);
      return;
    }
    setValidationMessage(undefined);
    setDraftValue(undefined);
    onChange(next);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      setDraftValue(undefined);
      setValidationMessage(undefined);
    }
  };

  return (
    <label className="reference-number-control">
      <span>{label}</span>
      <span className="reference-number-input">
        <input
          type="number"
          value={inputValue}
          min={minimum}
          max={maximum}
          aria-invalid={validationMessage ? "true" : undefined}
          onChange={(event) => {
            setDraftValue(event.target.value);
            setValidationMessage(undefined);
          }}
          onBlur={commit}
          onKeyDown={handleKeyDown}
        />
        {unit ? <small>{unit}</small> : null}
      </span>
      {validationMessage ? <small role="alert">{validationMessage}</small> : null}
    </label>
  );
}

function confidenceLabel(confidence: number): string {
  if (confidence >= 0.78) return "Tydlig struktur";
  if (confidence >= 0.55) return "Granska tolkningen";
  return "Osäker tolkning";
}

function importErrorMessage(error: unknown): string {
  if (!(error instanceof Error)) return "Bilden kunde inte analyseras.";
  const solution = "solution" in error && typeof error.solution === "string"
    ? error.solution
    : undefined;
  if (!solution || error.message.includes("Lösning:")) return error.message;
  return `${error.message} Lösning: ${solution}`;
}

export function ReferenceImageImporter({ open, onClose, onInspect, onApply }: ReferenceImageImporterProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const dropzoneRef = useRef<HTMLDivElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const objectUrlRef = useRef<string | undefined>(undefined);
  const [processing, setProcessing] = useState(false);
  const [error, setError] = useState<string>();
  const [previewUrl, setPreviewUrl] = useState<string>();
  const [fileName, setFileName] = useState("");
  const [analysis, setAnalysis] = useState<ReferenceImageAnalysis>();
  const [draft, setDraft] = useState<ReferenceImageDraft>();
  const [asset, setAsset] = useState<ReferenceAssetInspection>();

  useEffect(() => {
    return () => {
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    };
  }, []);

  const reset = () => {
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    objectUrlRef.current = undefined;
    setProcessing(false);
    setError(undefined);
    setPreviewUrl(undefined);
    setFileName("");
    setAnalysis(undefined);
    setDraft(undefined);
    setAsset(undefined);
  };

  const close = () => {
    reset();
    onClose();
  };

  useDialogFocus(open, dialogRef, close, !processing);

  useEffect(() => {
    if (!open || analysis) return;
    const frame = window.requestAnimationFrame(() => dropzoneRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [analysis, open]);

  useEffect(() => {
    if (!open || !error) return;
    const frame = window.requestAnimationFrame(() => errorRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [error, open]);

  if (!open) return null;

  const draftValidationError = draft ? referenceDraftValidationError(draft) : undefined;
  const maximumBaseHeightMm = draft
    ? maximumReferenceBaseCabinetHeightMm(draft.heightMm)
    : DESIGN_CONSTRAINTS.baseCabinetHeightMm.maximum;

  const chooseFile = () => inputRef.current?.click();

  const analyzeFile = async (file: File) => {
    setError(undefined);
    if (!ACCEPTED_TYPES.has(file.type)) {
      setError("Välj en JPG-, PNG- eller WebP-bild.");
      return;
    }
    if (file.size > MAX_FILE_BYTES) {
      setError("Bilden är större än 12 MB. Komprimera eller beskär den först.");
      return;
    }

    setProcessing(true);
    try {
      const objectUrl = URL.createObjectURL(file);
      const image = new window.Image();
      try {
        await new Promise<void>((resolve, reject) => {
          image.onload = () => resolve();
          image.onerror = () => reject(new Error("Bildfilen kunde inte läsas."));
          image.src = objectUrl;
        });
        if (image.naturalWidth < 160 || image.naturalHeight < 160) {
          throw new Error("Bilden måste vara minst 160 × 160 pixlar.");
        }
        const scale = Math.min(1, 960 / Math.max(image.naturalWidth, image.naturalHeight));
        const width = Math.max(160, Math.round(image.naturalWidth * scale));
        const height = Math.max(160, Math.round(image.naturalHeight * scale));
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        if (!context) throw new Error("Webbläsaren kunde inte starta bildanalysen.");
        context.drawImage(image, 0, 0, width, height);
        const pixels = context.getImageData(0, 0, width, height);
        const nextAnalysis = analyzeReferencePixels(pixels);
        const nextAsset = await onInspect(file);
        if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
        objectUrlRef.current = objectUrl;
        setPreviewUrl(objectUrl);
        setFileName(file.name);
        setAnalysis(nextAnalysis);
        setDraft(draftFromReferenceAnalysis(nextAnalysis));
        setAsset(nextAsset);
      } catch (error) {
        URL.revokeObjectURL(objectUrl);
        throw error;
      }
    } catch (unknownError) {
      setError(importErrorMessage(unknownError));
    } finally {
      setProcessing(false);
    }
  };

  const handleInput = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) void analyzeFile(file);
    event.target.value = "";
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    const file = event.dataTransfer.files[0];
    if (file) void analyzeFile(file);
  };

  const handlePaste = (event: ClipboardEvent<HTMLElement>) => {
    const file = imageFileFromClipboard(event.clipboardData);
    if (!file) {
      if (!analysis) setError("Urklippet innehåller ingen JPG-, PNG- eller WebP-bild.");
      return;
    }
    event.preventDefault();
    void analyzeFile(file);
  };

  const updateDraft = (patch: Partial<ReferenceImageDraft>) => {
    setDraft((current) => current ? { ...current, ...patch } : current);
  };

  const apply = () => {
    if (!analysis || !draft || !asset || !analysis.boundaryDetected) return;
    onApply(referenceResult(analysis, draft, fileName, asset));
    // Applying is not the same as cancelling. The parent closes the importer
    // and marks onboarding complete synchronously; invoking its close callback
    // afterwards can observe the previous `workspaceSelected=false` render and
    // reopen the mandatory planner on top of the newly created model.
    reset();
  };

  return (
    <div className="reference-import-backdrop" role="presentation" data-modal-root="true">
      <section ref={dialogRef} tabIndex={-1} className="reference-importer" role="dialog" aria-modal="true" aria-labelledby="reference-import-title" onPaste={handlePaste}>
        <header className="reference-import-header">
          <span className="reference-import-icon"><ImageIcon aria-hidden="true" size={21} /></span>
          <div>
            <p className="eyebrow">Bild till parametrisk modell</p>
            <h2 id="reference-import-title">Skapa från referensbild</h2>
            <p>Vi läser synliga proportioner och låter dig kontrollera tolkningen.</p>
          </div>
          <button type="button" aria-label="Stäng bildimporten" onClick={close} disabled={processing}>
            <X aria-hidden="true" size={22} />
          </button>
        </header>

        <input
          ref={inputRef}
          className="reference-file-input"
          type="file"
          accept="image/jpeg,image/png,image/webp"
          aria-label="Välj referensbild"
          tabIndex={-1}
          onChange={handleInput}
        />

        {error ? (
          <div ref={errorRef} tabIndex={-1} className="reference-import-error" role="alert">
            <AlertTriangle aria-hidden="true" size={18} />
            <span><strong>Bilden kunde inte användas.</strong> {error} Den tidigare tolkningen har inte ändrats.</span>
          </div>
        ) : null}

        {!analysis || !draft || !previewUrl ? (
          <div className="reference-import-start">
            <div
              ref={dropzoneRef}
              className={`reference-dropzone ${processing ? "processing" : ""}`}
              tabIndex={0}
              aria-label="Uppladdningsruta för referensbild"
              aria-keyshortcuts="Control+V Meta+V"
              onDragOver={(event) => event.preventDefault()}
              onDrop={handleDrop}
            >
              {processing ? <LoaderCircle aria-hidden="true" className="spin" size={38} /> : <Upload aria-hidden="true" size={38} />}
              <h3>{processing ? "Analyserar möbelns struktur…" : "Ladda upp en möbelbild"}</h3>
              <p>En rak frontbild med tydliga ytterkanter, hyllor och avdelare ger bäst resultat.</p>
              <button type="button" onClick={chooseFile} disabled={processing}>
                <Upload aria-hidden="true" size={17} /> Välj bild
              </button>
              <span className="reference-paste-hint"><kbd>Ctrl</kbd><b>+</b><kbd>V</kbd> Klistra in skärmklipp</span>
              <small>JPG, PNG eller WebP · högst 12 MB</small>
            </div>
            <div className="reference-import-guidance">
              <span><Check aria-hidden="true" size={16} /><strong>Bra bild</strong> Rak framifrån, hela möbeln synlig och jämnt ljus.</span>
              <span><AlertTriangle aria-hidden="true" size={16} /><strong>Kontroll krävs</strong> Djup, material och dolda infästningar kan inte avläsas säkert.</span>
            </div>
          </div>
        ) : (
          <div className="reference-review">
            <div className="reference-review-visual">
              <div className="reference-analysis-status">
                <span className={`reference-confidence ${analysis.confidence < 0.55 ? "low" : ""}`}>
                  <Sparkles aria-hidden="true" size={15} /> {Math.round(analysis.confidence * 100)} % · {confidenceLabel(analysis.confidence)}
                </span>
                <button type="button" onClick={chooseFile} disabled={processing}>
                  {processing ? <LoaderCircle aria-hidden="true" className="spin" size={15} /> : <Upload aria-hidden="true" size={15} />}
                  {processing ? " Sparar ny bild…" : " Byt bild"}
                </button>
              </div>
              <svg
                className="reference-analysis-preview"
                viewBox={`0 0 ${analysis.imageWidth} ${analysis.imageHeight}`}
                role="img"
                aria-label="Referensbild med detekterade möbellinjer"
              >
                <image href={previewUrl} width={analysis.imageWidth} height={analysis.imageHeight} />
                <rect
                  x={analysis.bounds.x}
                  y={analysis.bounds.y}
                  width={analysis.bounds.width}
                  height={analysis.bounds.height}
                  className="reference-detected-boundary"
                />
                {analysis.verticalGuides.map((ratio, index) => (
                  <line
                    key={`v-${index}`}
                    x1={analysis.bounds.x + ratio * analysis.bounds.width}
                    x2={analysis.bounds.x + ratio * analysis.bounds.width}
                    y1={analysis.bounds.y}
                    y2={analysis.bounds.y + (analysis.baseSplitRatio ?? 1) * analysis.bounds.height}
                    className="reference-detected-line"
                  />
                ))}
                {analysis.horizontalGuides.map((ratio, index) => (
                  <line
                    key={`h-${index}`}
                    x1={analysis.bounds.x}
                    x2={analysis.bounds.x + analysis.bounds.width}
                    y1={analysis.bounds.y + ratio * analysis.bounds.height}
                    y2={analysis.bounds.y + ratio * analysis.bounds.height}
                    className="reference-detected-line"
                  />
                ))}
                {analysis.baseSplitRatio === undefined ? null : (
                  <line
                    x1={analysis.bounds.x}
                    x2={analysis.bounds.x + analysis.bounds.width}
                    y1={analysis.bounds.y + analysis.baseSplitRatio * analysis.bounds.height}
                    y2={analysis.bounds.y + analysis.baseSplitRatio * analysis.bounds.height}
                    className="reference-detected-base"
                  />
                )}
              </svg>
              <p><span /> Gröna linjer visar vad som blir hyllor och avdelare. Den orange linjen visar underskåpets överkant.</p>
              <div className="reference-validation-pass" role="status">
                <Check aria-hidden="true" size={15} /> Källbilden är sparad oföränderligt · SHA-256 {asset?.image_sha256.slice(0, 12)}…
              </div>
              {analysis.warnings.length > 0 ? (
                <ul className="reference-warnings">
                  {analysis.warnings.map((warning) => <li key={warning}><AlertTriangle aria-hidden="true" size={14} /> {warning}</li>)}
                </ul>
              ) : (
                <div className="reference-validation-pass"><Check aria-hidden="true" size={15} /> Bilden har tillräcklig upplösning och en läsbar möbelstruktur.</div>
              )}
              {!analysis.boundaryDetected ? (
                <div className="reference-import-error" role="alert"><AlertTriangle aria-hidden="true" size={17} /> Bilden kan inte skapa en modell ännu. Byt till en tydligare bild med hela möbeln synlig.</div>
              ) : null}
            </div>

            <div className="reference-review-controls">
              <div className="reference-review-heading">
                <span><Ruler aria-hidden="true" size={18} /></span>
                <div><h3>Kontrollera tolkningen</h3><p>Bilden saknar skala. Bekräfta verkliga yttermått.</p></div>
              </div>
              <div className="reference-control-grid dimensions">
                <NumberControl
                  label="Bredd"
                  value={draft.widthMm}
                  unit="mm"
                  minimum={DESIGN_CONSTRAINTS.widthMm.minimum}
                  maximum={DESIGN_CONSTRAINTS.widthMm.maximum}
                  onChange={(widthMm) => updateDraft({ widthMm })}
                />
                <NumberControl
                  label="Höjd"
                  value={draft.heightMm}
                  unit="mm"
                  minimum={DESIGN_CONSTRAINTS.heightMm.minimum}
                  maximum={DESIGN_CONSTRAINTS.heightMm.maximum}
                  onChange={(heightMm) => {
                    const maximum = maximumReferenceBaseCabinetHeightMm(heightMm);
                    updateDraft({
                      heightMm,
                      ...(draft.furnitureType === "wall_library"
                        && maximum >= DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm
                        ? {
                            baseCabinetHeightMm: Math.min(
                              maximum,
                              Math.max(
                                DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
                                draft.baseCabinetHeightMm,
                              ),
                            ),
                          }
                        : {}),
                    });
                  }}
                />
                <NumberControl
                  label="Djup"
                  value={draft.depthMm}
                  unit="mm"
                  minimum={DESIGN_CONSTRAINTS.depthMm.minimum}
                  maximum={DESIGN_CONSTRAINTS.depthMm.maximum}
                  onChange={(depthMm) => updateDraft({
                    depthMm,
                    ...(draft.furnitureType === "wall_library" ? { baseCabinetDepthMm: depthMm } : {}),
                  })}
                />
              </div>
              <div className="reference-control-section">
                <h4>Synlig indelning</h4>
                <div className="reference-control-grid">
                  <NumberControl label="Hyllor" value={draft.shelfCount} minimum={DESIGN_CONSTRAINTS.shelfCount.minimum} maximum={DESIGN_CONSTRAINTS.shelfCount.maximum} onChange={(shelfCount) => updateDraft({ shelfCount })} />
                  <NumberControl label="Avdelare" value={draft.dividerCount} minimum={DESIGN_CONSTRAINTS.dividerCount.minimum} maximum={DESIGN_CONSTRAINTS.dividerCount.maximum} onChange={(dividerCount) => updateDraft({ dividerCount })} />
                </div>
                <small>Automatisk bildtolkning identifierar högst {REFERENCE_IMAGE_INFERENCE_LIMITS.shelfCount} hyllor och {REFERENCE_IMAGE_INFERENCE_LIMITS.dividerCount} avdelare. Bekräftade värden kan justeras till hela intervallet ovan.</small>
              </div>
              <label className="reference-base-toggle">
                <input
                  type="checkbox"
                  checked={draft.furnitureType === "wall_library"}
                  onChange={(event) => updateDraft(event.target.checked
                    ? {
                        furnitureType: "wall_library",
                        baseCabinetHeightMm: maximumBaseHeightMm
                          >= DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm
                          ? Math.min(
                              maximumBaseHeightMm,
                              Math.max(
                                DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
                                draft.baseCabinetHeightMm || 650,
                              ),
                            )
                          : DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
                        baseCabinetDepthMm: draft.depthMm,
                        baseCabinetCount: Math.min(
                          DESIGN_CONSTRAINTS.baseCabinetModuleCount.maximum,
                          draft.baseCabinetCount || Math.max(1, draft.dividerCount + 1),
                        ),
                      }
                    : { furnitureType: "bookcase", baseCabinetHeightMm: 0, baseCabinetDepthMm: 0, baseCabinetCount: 0 })}
                />
                <span><strong>Underskåp finns i bilden</strong><small>Aktivera om möbeln har en sammanhållen nedre skåpsdel.</small></span>
              </label>
              {draft.furnitureType === "wall_library" ? (
                <div className="reference-control-grid">
                  <NumberControl
                    label="Höjd underskåp"
                    value={draft.baseCabinetHeightMm}
                    unit="mm"
                    minimum={DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm}
                    maximum={Math.max(DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm, maximumBaseHeightMm)}
                    onChange={(baseCabinetHeightMm) => updateDraft({ baseCabinetHeightMm })}
                  />
                  <NumberControl label="Skåpsmoduler" value={draft.baseCabinetCount} minimum={1} maximum={DESIGN_CONSTRAINTS.baseCabinetModuleCount.maximum} onChange={(baseCabinetCount) => updateDraft({ baseCabinetCount })} />
                </div>
              ) : null}
              {draftValidationError ? (
                <div className="reference-import-error" role="alert">
                  <AlertTriangle aria-hidden="true" size={17} /> {draftValidationError}
                </div>
              ) : null}
              <div className="reference-concept-note" role="status">
                <AlertTriangle aria-hidden="true" size={18} />
                <span><strong>Skapas som koncept.</strong> Produktionsfiler spärras tills mått, material, bärighet och infästningar har verifierats.</span>
              </div>
            </div>
          </div>
        )}

        {analysis && draft && previewUrl ? (
          <footer className="reference-import-footer">
            <button type="button" onClick={close}>Avbryt</button>
            <button type="button" className="reference-create-button" onClick={apply} disabled={processing || !asset || !analysis.boundaryDetected || Boolean(draftValidationError)}>
              <Sparkles aria-hidden="true" size={17} /> Skapa konceptmodell
            </button>
          </footer>
        ) : null}
      </section>
    </div>
  );
}
