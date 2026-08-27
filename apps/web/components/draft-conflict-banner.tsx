"use client";

import { AlertTriangle, Copy, LoaderCircle, RefreshCw } from "lucide-react";

interface DraftConflictBannerProps {
  message: string;
  busy: boolean;
  copied: boolean;
  onCopy: () => void;
  onReload: () => void;
}

export function DraftConflictBanner({
  message,
  busy,
  copied,
  onCopy,
  onReload,
}: DraftConflictBannerProps) {
  return (
    <div className="draft-conflict-banner" role="alert">
      <AlertTriangle aria-hidden="true" size={19} />
      <span>
        <strong>Utkastet har ändrats i en annan flik</strong>
        <small>{message} Dina lokala ändringar finns kvar på skärmen och i den lokala projektkopian tills du hämtar serverutkastet.</small>
      </span>
      <div>
        <button type="button" onClick={onCopy} disabled={busy}>
          <Copy aria-hidden="true" size={15} /> {copied ? "Kopierat" : "Kopiera lokalt utkast"}
        </button>
        <button type="button" className="primary" onClick={onReload} disabled={busy}>
          {busy ? <LoaderCircle aria-hidden="true" className="spin" size={15} /> : <RefreshCw aria-hidden="true" size={15} />}
          {busy ? "Hämtar…" : "Hämta senaste utkast"}
        </button>
      </div>
    </div>
  );
}
