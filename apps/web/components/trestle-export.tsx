"use client";

import { useEffect, useRef, useState } from "react";
import { CustombuildApiClient } from "@/lib/api-client";
import type { components } from "@/lib/api-schema";
import type { PublicRuntimeConfig } from "@/lib/runtime-config";

export function TrestleExport({ construction, runtimeConfig, verified }: {
  construction: components["schemas"]["TrestleConstruction"] | components["schemas"]["SideboardConstruction"];
  runtimeConfig: PublicRuntimeConfig;
  verified: (report: components["schemas"]["MeasuredReport"]) => void;
}) {
  const controller = useRef<AbortController | null>(null);
  const savedJob = useRef<components["schemas"]["TrestleExportAccepted"] | null>(null);
  const requestKey = useRef<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  useEffect(() => () => controller.current?.abort(), []);
  async function start() {
    const abort = new AbortController(); controller.current?.abort(); controller.current = abort;
    setBusy(true); setError(""); setMessage("Köar CAD-kontroll och ritningar …");
    const api = new CustombuildApiClient(runtimeConfig.apiUrl, undefined, runtimeConfig.developmentToken);
    try {
      requestKey.current ??= crypto.randomUUID();
      const job = savedJob.current ?? (construction.schema_version === "custombuild.sideboard-construction.v1"
        ? await api.requestSideboardExport({ request: construction.request,
          expected_construction_sha256: construction.construction_sha256, idempotency_key: requestKey.current }, abort.signal)
        : await api.requestTrestleExport({ request: construction.request,
        expected_construction_sha256: construction.construction_sha256, idempotency_key: requestKey.current,
      }, abort.signal));
      savedJob.current = job;
      for (let attempt = 0; attempt < 120 && !abort.signal.aborted; attempt++) {
        const result = await api.furnitureExportResult(job.project_id, job.job_id, abort.signal);
        if (result.state === "failed" || result.state === "cancelled") {
          savedJob.current = null; requestKey.current = null; throw Error(result.message);
        }
        if (result.state === "succeeded") {
          const blob = await api.downloadFurnitureExport(job.project_id, job.job_id, 1,
            construction.construction_sha256, result, abort.signal);
          if (abort.signal.aborted) return;
          const sideboard = construction.schema_version === "custombuild.sideboard-construction.v1";
          const report = await (sideboard ? api.inspectSideboardExport(job.project_id, job.job_id,
            construction.assembly_sha256, abort.signal) : api.inspectTrestleExport(job.project_id, job.job_id,
            construction.assembly_sha256, abort.signal));
          if (abort.signal.aborted) return;
          verified(report);
          const url = URL.createObjectURL(blob), link = document.createElement("a");
          link.href = url; link.download = sideboard ? "Custombuild-Skank-S1-ritningspaket.zip" : "Custombuild-Bockbord-B1-ritningspaket.zip"; link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          setMessage(sideboard ? "Ritningspaketet är hämtat. Trädelar, borrbilder och STEP-återimport är kontrollerade. Full gångjärnsrörelse, beslagvolymer och bärförmåga återstår."
            : "Ritningspaketet är hämtat. Exakt CAD och STEP-återimport har godkänts. Bärförmåga och tillverkningsacceptans framgår av paketet.");
          return;
        }
        setMessage(result.state === "queued" ? "Väntar på CAD-arbetaren …" : "Kontrollerar bearbetningar, förband och STEP-filer …");
        await new Promise<void>(resolve => {
          const done = () => { clearTimeout(timer); abort.signal.removeEventListener("abort", done); resolve(); };
          const timer = setTimeout(done, 1500);
          abort.signal.addEventListener("abort", done, { once: true });
        });
      }
      if (!abort.signal.aborted) setMessage("Exporten pågår fortfarande. Tryck igen för att fortsätta hämta samma sparade jobb.");
    } catch (reason) {
      if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : "Exporten kunde inte slutföras.");
    } finally { if (!abort.signal.aborted) setBusy(false); }
  }
  return <div>
    <button disabled={busy} onClick={() => void start()}>{busy ? "Skapar ritningspaket …" : "Skapa CAD-kontrollerat ritningspaket"}</button>
    <p>PDF-delritningar, {construction.assembly.parts.length} bearbetade STEP-delar, monterad STEP, {construction.assembly.parts.length * 6} DXF-vyer, kaplista,
      beslag och kontrollprotokoll. Paketet skapas för just denna konstruktion.</p>
    {message && <p role="status">{message}</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
