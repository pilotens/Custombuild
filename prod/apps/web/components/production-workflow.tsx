"use client";

import { Check, Download, LoaderCircle, ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  CustombuildApiClient,
  type ArtifactRead,
  type DesignVersionRead,
  type JobRead,
  type ReleaseRead,
} from "@/lib/api-client";
import { localDesignHash } from "@/lib/design-engine";
import type { DesignSpec, ResolvedDesign } from "@/lib/design-types";

type BusyAction =
  | "save"
  | "validate"
  | "design-approval"
  | "generation"
  | "cam-approval"
  | "release"
  | "download";

export type ProductionApi = Pick<
  CustombuildApiClient,
  | "configured"
  | "ensureProject"
  | "createVersion"
  | "validateVersion"
  | "approveVersion"
  | "generateVersion"
  | "getJob"
  | "listArtifacts"
  | "releaseVersion"
>;

export interface ProductionSummary {
  revision?: number;
  status: string;
  stale: boolean;
}

interface ProductionWorkflowProps {
  spec: DesignSpec;
  design: ResolvedDesign;
  onSummaryChange: (summary: ProductionSummary) => void;
  apiClient?: ProductionApi;
  pollIntervalMs?: number;
}

const PROJECT_NAME = "Arkitektväggen";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Ett okänt produktionsfel inträffade.";
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    draft: "Utkast",
    design_validated: "Design validerad",
    cam_validated: "CAM validerad",
    approved: "Godkänd",
    released: "Frisläppt",
    superseded: "Ersatt",
    archived: "Arkiverad",
  };
  return labels[status] ?? status;
}

export function ProductionWorkflow({
  spec,
  design,
  onSummaryChange,
  apiClient,
  pollIntervalMs = 2_000,
}: ProductionWorkflowProps) {
  const defaultApi = useMemo(() => new CustombuildApiClient(), []);
  const api = apiClient ?? defaultApi;
  const fingerprint = localDesignHash(spec);
  const [savedFingerprint, setSavedFingerprint] = useState<string>();
  const [version, setVersion] = useState<DesignVersionRead>();
  const [job, setJob] = useState<JobRead>();
  const [artifacts, setArtifacts] = useState<ArtifactRead[]>([]);
  const [release, setRelease] = useState<ReleaseRead>();
  const [designApproved, setDesignApproved] = useState(false);
  const [reviewReason, setReviewReason] = useState("");
  const [camReason, setCamReason] = useState("");
  const [releaseNumber, setReleaseNumber] = useState("R1");
  const [releaseConfirmed, setReleaseConfirmed] = useState(false);
  const [busy, setBusy] = useState<BusyAction>();
  const [error, setError] = useState<string>();

  const stale = Boolean(version && savedFingerprint !== fingerprint);
  const warningRuleIds = design.rule_evaluations
    .filter((evaluation) => evaluation.status === "WARNING")
    .map((evaluation) => evaluation.rule_id);
  const current = Boolean(version && !stale);
  const camApproved = version?.status === "approved" || version?.status === "released";
  const productionBundle = artifacts.find((artifact) => artifact.kind === "production_bundle");
  const reviewReasonValid = reviewReason.trim().length >= (warningRuleIds.length ? 10 : 5);
  const camReasonValid = camReason.trim().length >= 5;

  useEffect(() => {
    onSummaryChange({
      revision: version?.revision,
      status: stale ? "stale" : (version?.status ?? "unsaved"),
      stale,
    });
  }, [onSummaryChange, stale, version?.revision, version?.status]);

  async function perform(action: BusyAction, operation: () => Promise<void>) {
    setBusy(action);
    setError(undefined);
    try {
      await operation();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(undefined);
    }
  }

  function resetDownstream() {
    setJob(undefined);
    setArtifacts([]);
    setRelease(undefined);
    setDesignApproved(false);
    setReleaseConfirmed(false);
  }

  function saveRevision() {
    void perform("save", async () => {
      if (!api.configured) throw new ApiError("Produktions-API:t är inte konfigurerat.");
      if (design.source !== "server-preview") {
        throw new ApiError("Invänta serverns konstruktionspreview innan revisionen sparas.");
      }
      if (design.status === "BLOCK") {
        throw new ApiError("Blockerande konstruktionsfel måste lösas före versionsfrysning.");
      }
      const project = await api.ensureProject(PROJECT_NAME);
      const saved = await api.createVersion(project.id, spec);
      resetDownstream();
      setVersion(saved);
      setSavedFingerprint(fingerprint);
    });
  }

  function validateRevision() {
    if (!version) return;
    void perform("validate", async () => {
      const validated = await api.validateVersion(version.project_id, version.revision);
      setVersion(validated);
    });
  }

  function approveDesign() {
    if (!version) return;
    void perform("design-approval", async () => {
      const approved = await api.approveVersion(version.project_id, version.revision, {
        approval_type: "design",
        reason: reviewReason.trim(),
        generation_job_id: null,
        warning_overrides: warningRuleIds.map((ruleId) => ({
          rule_id: ruleId,
          reason: reviewReason.trim(),
        })),
      });
      setVersion(approved);
      setDesignApproved(true);
    });
  }

  function generatePackage() {
    if (!version) return;
    void perform("generation", async () => {
      const queued = await api.generateVersion(version.project_id, version.revision, {
        stock_width_mm: spec.stock_width_mm,
        stock_height_mm: spec.stock_height_mm,
        stock_count: spec.stock_count,
        back_stock_width_mm: spec.back_stock_width_mm,
        back_stock_height_mm: spec.back_stock_height_mm,
        back_stock_count: spec.back_stock_count,
        machine_profile_id: spec.machine_profile_id,
        postprocessor_id: "linuxcnc-validation-1.0.0",
        include_step: true,
        include_validation_program: true,
      });
      setJob(queued);
      setRelease(undefined);
      setArtifacts([]);

      let currentJob = queued;
      for (let attempt = 0; attempt < 120; attempt += 1) {
        if (currentJob.status === "succeeded") break;
        if (currentJob.status === "failed" || currentJob.status === "cancelled") {
          throw new ApiError(currentJob.error ?? `Produktionsjobbet ${currentJob.status}.`);
        }
        await wait(pollIntervalMs);
        currentJob = await api.getJob(queued.id);
        setJob(currentJob);
      }
      if (currentJob.status !== "succeeded") {
        throw new ApiError("Produktionsjobbet överskred väntetiden på fyra minuter.");
      }
      const generatedArtifacts = await api.listArtifacts(currentJob.id);
      if (!generatedArtifacts.some((artifact) => artifact.kind === "production_bundle")) {
        throw new ApiError("Jobbet lyckades men saknar verifierat produktionspaket.");
      }
      setArtifacts(generatedArtifacts);
    });
  }

  function approveCam() {
    if (!version || !job) return;
    void perform("cam-approval", async () => {
      const approved = await api.approveVersion(version.project_id, version.revision, {
        approval_type: "cam",
        reason: camReason.trim(),
        generation_job_id: job.id,
        warning_overrides: [],
      });
      setVersion(approved);
    });
  }

  function releaseRevision() {
    if (!version) return;
    void perform("release", async () => {
      const released = await api.releaseVersion(
        version.project_id,
        version.revision,
        releaseNumber.trim().toUpperCase(),
      );
      setRelease(released);
      setVersion({ ...version, status: "released", immutable: true });
    });
  }

  function downloadPackage() {
    if (!job) return;
    void perform("download", async () => {
      const currentArtifacts = await api.listArtifacts(job.id);
      const bundle = currentArtifacts.find((artifact) => artifact.kind === "production_bundle");
      if (!bundle) throw new ApiError("Produktionspaketet saknas eller är inte längre aktuellt.");
      setArtifacts(currentArtifacts);
      const link = document.createElement("a");
      link.href = bundle.download_url;
      link.download = `custombuild-rev-${version?.revision ?? "unknown"}.zip`;
      link.rel = "noopener noreferrer";
      document.body.append(link);
      link.click();
      link.remove();
    });
  }

  if (!api.configured) {
    return (
      <div className="production-empty" role="status">
        Produktionsflödet är avstängt i lokalt previewläge. Konfigurera API:t för att skapa
        revisioner, köra CAD/CAM och hämta paket.
      </div>
    );
  }

  return (
    <div className="production-workflow" id="production-release">
      <div className="production-gate-summary">
        <span className={`production-state ${stale ? "stale" : "current"}`}>
          {version ? `Rev ${version.revision} · ${stale ? "Ändrad" : statusLabel(version.status)}` : "Ej sparad"}
        </span>
        <span>Maskinutdata: endast validering</span>
      </div>

      <ol className="production-steps" aria-label="Produktionsgrindar">
        <li className={version ? "done" : "active"}><span>1</span> Frys revision</li>
        <li className={version?.status !== "draft" && current ? "done" : ""}><span>2</span> Designkontroll</li>
        <li className={job?.status === "succeeded" && current ? "done" : ""}><span>3</span> CAD/CAM-jobb</li>
        <li className={camApproved && current ? "done" : ""}><span>4</span> CAM-godkännande</li>
        <li className={release ? "done" : ""}><span>5</span> Frisläpp</li>
      </ol>

      {stale ? (
        <p className="production-warning" role="alert">
          Parametrar eller produktionsval har ändrats. Den tidigare revisionens underlag får inte
          användas; spara en ny serverrevision.
        </p>
      ) : null}
      {error ? <p className="production-error" role="alert">{error}</p> : null}

      <div className="production-actions">
        <button
          type="button"
          onClick={saveRevision}
          disabled={
            Boolean(busy)
            || design.status === "BLOCK"
            || design.source !== "server-preview"
            || Boolean(version?.immutable && !stale)
          }
        >
          {busy === "save" ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}
          {version?.immutable && !stale
            ? "Revision frisläppt"
            : version
              ? "Spara ny revision"
              : "Spara revision"}
        </button>
        <button
          type="button"
          onClick={validateRevision}
          disabled={Boolean(busy) || !current || version?.status !== "draft"}
        >
          Validera design
        </button>
      </div>

      <div className="production-review-grid">
        <label>
          <span>Designgranskarens motivering</span>
          <input
            value={reviewReason}
            onChange={(event) => setReviewReason(event.target.value)}
            placeholder={warningRuleIds.length ? "Motivera samtliga varningar…" : "Beskriv designgranskningen…"}
          />
        </label>
        <button
          type="button"
          onClick={approveDesign}
          disabled={Boolean(busy) || !current || version?.status !== "design_validated" || designApproved || !reviewReasonValid}
        >
          Godkänn design{warningRuleIds.length ? ` + ${warningRuleIds.length} override` : ""}
        </button>

        <button
          type="button"
          onClick={generatePackage}
          disabled={Boolean(busy) || !current || !designApproved || job?.status === "succeeded"}
        >
          {busy === "generation" ? <LoaderCircle className="spin" size={14} /> : null}
          {job && job.status !== "succeeded" ? `Jobb: ${job.status}` : "Generera paket"}
        </button>
        <label>
          <span>CAM-granskarens motivering</span>
          <input
            value={camReason}
            onChange={(event) => setCamReason(event.target.value)}
            placeholder="Beskriv kontroll av setup och backplot…"
          />
        </label>
        <button
          type="button"
          onClick={approveCam}
          disabled={Boolean(busy) || !current || job?.status !== "succeeded" || camApproved || !camReasonValid}
        >
          Godkänn CAM för detta jobb
        </button>
      </div>

      <div className="release-controls">
        <label>
          <span>Release-nummer</span>
          <input
            value={releaseNumber}
            onChange={(event) => setReleaseNumber(event.target.value.toUpperCase())}
            pattern="[A-Z0-9][A-Z0-9._-]{0,39}"
          />
        </label>
        <label className="release-confirmation">
          <input
            type="checkbox"
            checked={releaseConfirmed}
            onChange={(event) => setReleaseConfirmed(event.target.checked)}
          />
          Jag bekräftar att revisionen ska låsas. Detta är inte ett fysiskt maskinngodkännande.
        </label>
        <button
          type="button"
          className="release-button"
          onClick={releaseRevision}
          disabled={Boolean(busy) || !current || !camApproved || version?.status !== "approved" || !releaseConfirmed || !/^[A-Z0-9][A-Z0-9._-]{0,39}$/.test(releaseNumber)}
        >
          <ShieldAlert size={14} /> Frisläpp revision
        </button>
      </div>

      {release && productionBundle ? (
        <div className="production-download" role="status">
          <div>
            <strong>{release.release_number} frisläppt</strong>
            <code>{release.manifest_sha256.slice(0, 16)}…</code>
          </div>
          <button type="button" onClick={downloadPackage} disabled={busy === "download"}>
            {busy === "download" ? <LoaderCircle className="spin" size={14} /> : <Download size={14} />}
            Ladda ned ZIP ({(productionBundle.size_bytes / 1_000_000).toFixed(1)} MB)
          </button>
        </div>
      ) : null}
    </div>
  );
}
