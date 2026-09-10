import type { FurniturePreview } from "@/lib/furniture-workspace";

type Rule = FurniturePreview["rules"]["evaluations"][number];
const format = (value: number) => new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 6 }).format(value);

/** Display the server's assessment; never recalculate a construction limit in the browser. */
export function furnitureRuleMeasurement(rule: Rule): { calculated: string; allowed: string } | null {
  const v = rule.values;
  for (const [actual, limit, unit] of [
    ["calculated", "allowed", typeof v.unit === "string" ? v.unit : ""],
    ["calculated_mm", "allowed_mm", "mm"],
    ["calculated_mpa", "allowed_mpa", "MPa"],
  ] as const) {
    const calculated = v[actual], allowed = v[limit];
    if (typeof calculated !== "number" || typeof allowed !== "number"
      || !Number.isFinite(calculated) || !Number.isFinite(allowed)) continue;
    const value = (n: number) => unit === "µm" ? `${format(n / 1_000)} mm`
      : unit === "kPa" ? `${format(n / 1_000)} MPa` : `${format(n)}${unit ? ` ${unit}` : ""}`;
    return { calculated: value(calculated), allowed: value(allowed) };
  }
  return null;
}

export function FurnitureRuleValues({ rule }: { rule: Rule }) {
  const measurement = furnitureRuleMeasurement(rule);
  const span = rule.values.span_um;
  const moment = rule.values.residual_moment_nm;
  return <>
    {measurement ? <p><strong>Beräknat: {measurement.calculated}</strong> · Gränsvärde: {measurement.allowed}</p> : null}
    {typeof span === "number" ? <p>Fri spännvidd: {format(span / 1_000)} mm</p> : null}
    {typeof moment === "number" ? <p>Återstående stabiliserande moment: {format(moment)} Nm · {rule.values.open_drawers} öppna lådor</p> : null}
  </>;
}
