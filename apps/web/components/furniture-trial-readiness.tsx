import type { FurnitureTrialReadiness } from "@/lib/furniture-workspace";
import styles from "./furniture-workspace.module.css";

const checkLabels = {
  checked: "Kontrollerat i modellen",
  blocked: "Blockerar provet",
  requires_evidence: "Behöver styrkas",
} as const;

export function FurnitureTrialReadinessPanel({ report, designHash, dirty }: {
  report?: FurnitureTrialReadiness;
  designHash?: string;
  dirty: boolean;
}) {
  const current = report && designHash && report.design_hash === designHash ? report : undefined;
  return <section className={styles.review} aria-label="Inför verkstadsprov">
    <h2>Inför verkstadsprov</h2>
    {!current ? <p>En aktuell provbedömning saknas. Uppdatera förhandsgranskningen för att se vad som behöver lösas.</p> : <>
      <p><strong>{current.state === "requires_design_change"
        ? "Designen behöver åtgärdas före provet."
        : "Verkstadsunderlag och praktisk provning återstår."}</strong></p>
      <p>{current.blocker_count} {current.blocker_count === 1 ? "punkt blockerar" : "punkter blockerar"} provet · {current.evidence_required_count} {current.evidence_required_count === 1 ? "punkt behöver" : "punkter behöver"} styrkas.</p>
      <div className={styles.notice}><p><strong>Nästa steg:</strong> {current.next_action}</p></div>
      <p>{dirty
        ? "Bedömningen gäller din aktuella arbetskopia. Spara ändringarna innan du tar fram underlaget till verkstaden."
        : "Bedömningen gäller den aktuella förhandsgranskningen. Ändrade mått, material eller tillverkningsval kräver en ny bedömning."}</p>
      <p>Modellkontrollerna ger inget tillstånd att köra maskinen. Material, fogar och verkstadens utrustning måste styrkas för det verkliga provet.</p>
      <div className={styles.rules}>{current.checks.map(check => <details key={check.code} open={check.state === "blocked"}>
        <summary><span className={check.state === "blocked" ? styles.blocked
          : check.state === "checked" ? styles.pass : styles.requiresReview}>{checkLabels[check.state]}</span>{check.title}</summary>
        <p>{check.detail}</p>
        <p><strong>Att göra:</strong> {check.action}</p>
      </details>)}</div>
    </>}
  </section>;
}
