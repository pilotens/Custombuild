import { CloudOff } from "lucide-react";

interface CanvasStateBannersProps {
  apiMessage: string;
  apiState?: "error" | "offline";
  authError?: string;
  canRetryApi: boolean;
  onRetryApi: () => void;
  projectError?: string;
}

export function CanvasStateBanners({
  apiMessage,
  apiState,
  authError,
  canRetryApi,
  onRetryApi,
  projectError,
}: CanvasStateBannersProps) {
  if (!authError && !projectError && !apiState) return null;

  return (
    <div className="canvas-state-banners" data-testid="canvas-state-banners">
      {authError ? (
        <div className="offline-banner error canvas-state-banner" role="alert">
          <CloudOff aria-hidden="true" size={14} />
          <span><strong>Inloggningen misslyckades.</strong> {authError}</span>
        </div>
      ) : null}
      {projectError ? (
        <div className="offline-banner error canvas-state-banner" role="alert">
          <CloudOff aria-hidden="true" size={14} />
          <span><strong>Projektet kunde inte öppnas.</strong> {projectError}</span>
        </div>
      ) : null}
      {apiState ? (
        <div className={`offline-banner canvas-state-banner ${apiState === "error" ? "error" : ""}`} role="status">
          <CloudOff aria-hidden="true" size={14} />
          <span>
            <strong>{apiState === "error" ? "Servermodellen är inte tillgänglig." : "Lokalt konstruktionsläge."}</strong>{" "}
            {apiState === "error"
              ? apiMessage
              : "Produktionsfiler och serverauktoritativ geometri är inte tillgängliga."}
          </span>
          {apiState === "error" && canRetryApi ? (
            <button type="button" onClick={onRetryApi}>Försök igen</button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
