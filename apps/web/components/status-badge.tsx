import { AlertTriangle, Check, CircleX } from "lucide-react";
import type { ValidationStatus } from "@/lib/design-types";

const LABELS: Record<ValidationStatus, string> = {
  PASS: "Godkänd",
  WARNING: "Varning",
  BLOCK: "Blockerar",
};

export function StatusBadge({ status, compact = false }: { status: ValidationStatus; compact?: boolean }) {
  const Icon = status === "PASS" ? Check : status === "WARNING" ? AlertTriangle : CircleX;
  return (
    <span className={`status-badge status-${status.toLowerCase()}`} data-testid="status-badge">
      <Icon aria-hidden="true" size={compact ? 12 : 14} strokeWidth={2.4} />
      {compact ? status : LABELS[status]}
    </span>
  );
}
