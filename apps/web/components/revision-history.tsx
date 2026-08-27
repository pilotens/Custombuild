"use client";

import { History, LockKeyhole, ShieldX } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { DesignVersionRead } from "@/lib/api-client";
import styles from "./revision-history.module.css";

interface RevisionHistoryApi {
  configured: boolean;
  listVersions?: (projectId: string) => Promise<DesignVersionRead[]>;
}

interface RevisionHistoryProps {
  active: boolean;
  api: RevisionHistoryApi;
  projectId?: string;
  localDesignHash: string;
  currentRevision?: number;
  revisionRefreshKey?: string;
  unavailableReason?: "concept";
}

type LoadState =
  | { phase: "idle"; versions: DesignVersionRead[] }
  | { phase: "loading"; versions: DesignVersionRead[]; retrying: boolean }
  | { phase: "ready"; versions: DesignVersionRead[] }
  | { phase: "error"; versions: DesignVersionRead[] };

const validatedStatuses = new Set<DesignVersionRead["status"]>([
  "design_validated",
  "cam_validated",
  "approved",
  "released",
]);

function revisionStatusLabel(status: DesignVersionRead["status"]): string {
  switch (status) {
    case "concept":
    case "draft":
      return "Serverutkast";
    case "design_validated":
      return "Designvaliderad";
    case "cam_validated":
      return "CAM-validerad";
    case "approved":
      return "Godkänd serverrevision";
    case "released":
      return "Historiskt frisläppt serverrevision";
    case "superseded":
      return "Ersatt serverrevision";
    case "archived":
      return "Arkiverad serverrevision";
  }
}

function validationLabel(status: DesignVersionRead["status"]): string {
  if (validatedStatuses.has(status)) return "Designvaliderad";
  if (status === "concept" || status === "draft") return "Ej designvaliderad";
  return "Valideringsstatus framgår inte";
}

function formatCreatedAt(value: string): string {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return "Tidpunkt saknas";
  return new Intl.DateTimeFormat("sv-SE", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(timestamp);
}

function StaticHistoryState({ children }: { children: ReactNode }) {
  return <p className={styles.state} role="status">{children}</p>;
}

export function RevisionHistory({
  active,
  api,
  projectId,
  localDesignHash,
  currentRevision,
  revisionRefreshKey = "none",
  unavailableReason,
}: RevisionHistoryProps) {
  const headingId = useId();
  const [loadState, setLoadState] = useState<LoadState>({ phase: "idle", versions: [] });
  const activeRequestTokenRef = useRef<symbol | undefined>(undefined);
  const inFlightRequestsRef = useRef(new Map<string, Promise<DesignVersionRead[]>>());

  const loadVersions = useCallback((retrying = false): void => {
    const activeProjectId = projectId;
    const listVersions = api.listVersions;
    if (
      !active
      || unavailableReason
      || !api.configured
      || !activeProjectId
      || !listVersions
      || activeRequestTokenRef.current
    ) return;

    const requestKey = `${activeProjectId}:${revisionRefreshKey}`;
    const requestToken = Symbol(requestKey);
    activeRequestTokenRef.current = requestToken;
    setLoadState({ phase: "loading", versions: [], retrying });

    let request = inFlightRequestsRef.current.get(requestKey);
    if (!request) {
      request = Promise.resolve().then(() => listVersions.call(api, activeProjectId));
      inFlightRequestsRef.current.set(requestKey, request);
      const clearRequest = () => {
        if (inFlightRequestsRef.current.get(requestKey) === request) {
          inFlightRequestsRef.current.delete(requestKey);
        }
      };
      void request.then(clearRequest, clearRequest);
    }

    void (async () => {
      try {
        const versions = await request;
        if (activeRequestTokenRef.current !== requestToken) return;
        setLoadState({
          phase: "ready",
          versions: [...versions]
            .filter((version) => version.project_id === activeProjectId)
            .sort((left, right) => right.revision - left.revision),
        });
      } catch {
        if (activeRequestTokenRef.current !== requestToken) return;
        setLoadState({ phase: "error", versions: [] });
      } finally {
        if (activeRequestTokenRef.current === requestToken) {
          activeRequestTokenRef.current = undefined;
        }
      }
    })();
  }, [active, api, projectId, revisionRefreshKey, unavailableReason]);

  useEffect(() => {
    loadVersions();
    return () => { activeRequestTokenRef.current = undefined; };
  }, [loadVersions]);

  if (!active) return null;

  let content: ReactNode;
  if (unavailableReason === "concept") {
    content = (
      <StaticHistoryState>
        Konceptmodeller har ingen serverbaserad versionshistorik. Välj en screenad mall och spara
        projektet först.
      </StaticHistoryState>
    );
  } else if (!api.configured) {
    content = (
      <StaticHistoryState>
        Versionshistoriken är inte tillgänglig utan serveranslutning.
      </StaticHistoryState>
    );
  } else if (!projectId) {
    content = (
      <StaticHistoryState>
        Inget serverprojekt är valt. Spara projektet för att börja versionshistoriken.
      </StaticHistoryState>
    );
  } else if (!api.listVersions) {
    content = (
      <StaticHistoryState>
        Servern erbjuder ingen read-only versionslista i den här klienten.
      </StaticHistoryState>
    );
  } else if (loadState.phase === "idle" || loadState.phase === "loading") {
    content = loadState.phase === "loading" && loadState.retrying ? (
      <div className={`${styles.state} ${styles.stateWithAction}`} role="status">
        <span>Laddar versionshistorik…</span>
        <button type="button" className={styles.retryButton} disabled>Försök igen</button>
      </div>
    ) : <StaticHistoryState>Laddar versionshistorik…</StaticHistoryState>;
  } else if (loadState.phase === "error") {
    content = (
      <div className={styles.error} role="alert">
        <span>Versionshistoriken kunde inte hämtas. Hämtningen påverkar inte din arbetsmodell.</span>
        <button
          type="button"
          className={styles.retryButton}
          onClick={() => loadVersions(true)}
        >
          Försök igen
        </button>
      </div>
    );
  } else if (loadState.versions.length === 0) {
    content = (
      <StaticHistoryState>
        Inga serverrevisioner finns ännu. Den lokala modellen är inte sparad som en designrevision.
      </StaticHistoryState>
    );
  } else {
    const matchingRevision = loadState.versions.find(
      (candidate) => candidate.design_hash === localDesignHash,
    );
    content = (
      <>
        <div className={styles.localModel} role="status" aria-label="Lokal modell och serverrevision">
          <span>Lokal arbetsmodell</span>
          <strong>
            {matchingRevision
              ? `Matchar serverrevision R${matchingRevision.revision}`
              : "Saknar motsvarande serverrevision"}
          </strong>
          <small>
            {matchingRevision && currentRevision !== undefined
              && matchingRevision.revision !== currentRevision
              ? "Matchningen avser en äldre serverrevision."
              : "Serverhistoriken är read-only och ändrar inte din lokala modell."}
          </small>
        </div>

        <ol className={styles.list} aria-label="Serverrevisioner">
          {loadState.versions.map((revision) => {
            const current = revision.revision === currentRevision;
            return (
              <li
                key={revision.id}
                className={current ? styles.current : undefined}
                aria-current={current ? "true" : undefined}
              >
                <span className={styles.marker} aria-hidden="true" />
                <div className={styles.revisionBody}>
                  <div className={styles.revisionHeading}>
                    <strong>Revision R{revision.revision}</strong>
                    <time dateTime={revision.created_at}>{formatCreatedAt(revision.created_at)}</time>
                  </div>
                  <div className={styles.facets}>
                    <span>{revisionStatusLabel(revision.status)}</span>
                    <span>
                      {revision.immutable ? <LockKeyhole aria-hidden="true" size={12} /> : null}
                      {revision.immutable
                        ? "Oföränderlig designrevision"
                        : "Ändringsbar serverrevision"}
                    </span>
                    <span>{validationLabel(revision.status)}</span>
                    <span className={styles.physicalState}>
                      <ShieldX aria-hidden="true" size={12} />
                      Ej frisläppt för fysisk kapning i detta Underlag
                    </span>
                  </div>
                  <details className={styles.technical}>
                    <summary>Tekniska detaljer</summary>
                    <dl>
                      <div><dt>Serverstatus</dt><dd><code>{revision.status}</code></dd></div>
                      <div><dt>Design-hash</dt><dd><code>{revision.design_hash}</code></dd></div>
                      <div><dt>Kontext-hash</dt><dd><code>{revision.context_hash}</code></dd></div>
                      <div><dt>Motor</dt><dd><code>{revision.engine_version}</code></dd></div>
                      <div><dt>Mall</dt><dd><code>{revision.template_id}@{revision.template_version}</code></dd></div>
                      <div><dt>Regler</dt><dd><code>{revision.rule_version}</code></dd></div>
                    </dl>
                    <p>
                      Versionslistan innehåller inget verifierbart frisläppningsbevis. Fysisk
                      kapning visas därför fail-closed i Underlag.
                    </p>
                  </details>
                </div>
              </li>
            );
          })}
        </ol>
      </>
    );
  }

  return (
    <section className={styles.history} aria-labelledby={headingId}>
      <header>
        <History aria-hidden="true" size={17} />
        <div>
          <h3 id={headingId}>Versionshistorik</h3>
          <p>Serverns sparade designrevisioner · endast läsning</p>
        </div>
      </header>
      {content}
    </section>
  );
}
