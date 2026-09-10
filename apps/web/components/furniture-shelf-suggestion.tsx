"use client";

import { useEffect, useRef, useState } from "react";
import type { CustombuildApiClient } from "@/lib/api-client";
import type { FurnitureShelfSuggestion, FurnitureWorkspace } from "@/lib/furniture-workspace";
import { furnitureRuleMeasurement } from "./furniture-rule-values";
import styles from "./furniture-workspace.module.css";

const mm = (value: number) => new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 3 }).format(value / 1_000);
const kg = (value: number) => new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 1 }).format(value / 1_000);

export function FurnitureShelfSuggestionPanel({ api, workspace, designHash, disabled, onApply }: {
  api: CustombuildApiClient;
  workspace: FurnitureWorkspace;
  designHash: string;
  disabled: boolean;
  onApply: (workspace: FurnitureWorkspace) => void;
}) {
  const [result, setResult] = useState<FurnitureShelfSuggestion>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);
  useEffect(() => () => { epoch.current += 1; }, [workspace, designHash]);
  const calculate = async () => {
    const request = ++epoch.current;
    setBusy(true); setError(undefined); setResult(undefined);
    try {
      const proposal = await api.suggestFurnitureShelfBays(workspace);
      if (request !== epoch.current) return;
      if (proposal.current.design.design_hash !== designHash) throw new Error("Designen har ändrats. Beräkna förslaget igen.");
      setResult(proposal);
    } catch (reason) {
      if (request === epoch.current) setError(reason instanceof Error ? reason.message : "Förslaget kunde inte beräknas.");
    } finally { if (request === epoch.current) setBusy(false); }
  };
  const proposed = result?.proposed;
  const current = result?.current_bays;
  const bays = result?.proposed_bays;
  return <section className={styles.shelfSuggestion} aria-label="Förslag på hyllfack">
    <h3>Anpassa facken till hyllasten</h3>
    <p>Beräkna fler jämnt fördelade fack med samma yttermått, material och angivna last. Du granskar förslaget innan designen ändras.</p>
    <button type="button" disabled={disabled || busy} onClick={() => { void calculate(); }}>
      {busy ? "Beräknar fackindelning…" : "Föreslå fackindelning"}
    </button>
    {error ? <p role="alert" className={styles.error}>{error}</p> : null}
    {result && !disabled ? <div className={styles.proposal} aria-live="polite">
      <p><strong>{result.message}</strong></p>
      {result.can_apply && proposed && current && bays ? <>
        <p>{current.bay_count} → {bays.bay_count} fack · {current.divider_count} → {bays.divider_count} avdelare</p>
        <p>Fri fackbredd: {mm(Math.min(...bays.clear_widths_um))}–{mm(Math.max(...bays.clear_widths_um))} mm.</p>
        <p>Möbelns beräknade bruttovikt: {kg(result.current.design.total_weight_g)} → {kg(proposed.design.total_weight_g)} kg. Fler avdelare kräver mer material.</p>
        <div className={styles.tableScroll}><table className={styles.comparisonTable}>
          <caption>Beräknad hyllbärighet före och efter förslaget</caption>
          <thead><tr><th scope="col">Kontroll</th><th scope="col">Nu</th><th scope="col">Förslag</th><th scope="col">Gränsvärde efter ändring</th></tr></thead>
          <tbody>{result.screening_checks.proposed?.map(check => {
            const afterRule = proposed.rules.evaluations.find(rule => rule.rule_id === check.rule_id);
            const beforeRule = result.current.rules.evaluations.find(rule => rule.rule_id === check.rule_id);
            if (!afterRule || !beforeRule) return null;
            const after = furnitureRuleMeasurement(afterRule);
            const before = furnitureRuleMeasurement(beforeRule);
            return <tr key={check.rule_id}><th scope="row">{afterRule.title}</th>
              <td>{before?.calculated ?? "—"}</td><td>{after?.calculated ?? "—"}</td><td>{after?.allowed ?? "—"}</td></tr>;
          })}</tbody>
        </table></div>
        <p>{result.scope}</p>
        {result.remaining_issues.rules.length || result.remaining_issues.manufacturing.length ? <details>
          <summary>Kvar att lösa eller granska ({result.remaining_issues.rules.length + result.remaining_issues.manufacturing.length})</summary>
          <ul>{result.remaining_issues.rules.map(rule => <li key={rule.rule_id}>{rule.title}: {rule.detail}</li>)}
            {result.remaining_issues.manufacturing.map((issue, index) => <li key={`${issue.code}-${index}`}>{issue.message}</li>)}</ul>
        </details> : null}
        <p>Kontrollera att fackbredderna passar innehållet. Förslaget kortar inte genomgående topp, botten eller rygg för att passa råskivor.</p>
        <button type="button" className={styles.primary} onClick={() => onApply(proposed.workspace)}>Använd föreslagen fackindelning</button>
      </> : <p>{result.scope}</p>}
    </div> : null}
  </section>;
}
