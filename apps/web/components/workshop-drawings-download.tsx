"use client";

import { useEffect, useRef, useState } from "react";
import type { CustombuildApiClient } from "@/lib/api-client";
import { downloadFile } from "@/lib/download-file";
import type { FurnitureExportReady } from "@/lib/furniture-workspace";

export function WorkshopDrawingsDownload({api, project, revision, hash, artifact}: {
  api: CustombuildApiClient; project: string; revision: number; hash: string; artifact: FurnitureExportReady;
}) {
  const inFlight = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  useEffect(() => () => inFlight.current?.abort(), []);

  const download = async () => {
    if (inFlight.current) return;
    const controller = new AbortController();
    inFlight.current = controller;
    setBusy(true);
    setError(undefined);
    try {
      // Another session can save a new revision between job completion and this click.
      const blob = await api.downloadFurnitureExport(
        project, artifact.job_id, revision, hash, artifact, controller.signal,
      );
      if (!controller.signal.aborted) downloadFile(blob, artifact.file_name);
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError(reason instanceof Error ? reason.message : "Ritningarna kunde inte hämtas.");
      }
    } finally {
      inFlight.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  return <div>
    <button type="button" disabled={busy} onClick={() => { void download(); }}>
      Hämta frisläppta verkstadsritningar · revision {revision}
    </button>
    {error && <p role="status">{error}</p>}
  </div>;
}
