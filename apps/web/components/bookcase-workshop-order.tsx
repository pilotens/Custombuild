"use client";

import { useEffect, useState } from "react";
import type { CustombuildApiClient } from "@/lib/api-client";

export function BookcaseWorkshopOrder({ api, project, revision, hash, disabled, onOrder }: {
  api: CustombuildApiClient; project: string; revision: number; hash: string;
  disabled: boolean; onOrder: () => void;
}) {
  const [status, setStatus] = useState<{ available: boolean; detail: string }>();
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void api.workshopEligibility(project, revision, hash, controller.signal).then(result => {
      if (!controller.signal.aborted) setStatus(result);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setStatus({available: false,
        detail: error instanceof Error ? error.message : "Verkstadsorderns status kunde inte hämtas."});
    });
    return () => controller.abort();
  }, [api, project, revision, hash, attempt]);
  return <div>
    <p role="status">{status?.detail ?? "Kontrollerar om den sparade revisionen täcks av en provprofil…"}</p>
    <button disabled={disabled || !status?.available} onClick={onOrder}>Beställ verkstadsritningar</button>
    {status && !status.available && <button type="button" disabled={disabled} onClick={() => {
      setStatus(undefined); setAttempt(current => current + 1);
    }}>Kontrollera orderstatus igen</button>}
  </div>;
}
