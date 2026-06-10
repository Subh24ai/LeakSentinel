// Shared presentational components: chips, stat cards, bar charts, and the
// loading / error / empty states used across every page.

import type { ReactNode } from "react";

import type { Disposition } from "../api";
import { formatInt, humanize } from "../lib/format";

// --- Chips ----------------------------------------------------------------- //
type ChipTone = "neutral" | "accent" | "ok" | "warn" | "danger";

export function Chip({ tone, children }: { tone: ChipTone; children: ReactNode }) {
  return (
    <span className={`chip chip-${tone}`}>
      <span className="chip-dot" />
      {children}
    </span>
  );
}

const SEVERITY_TONE: Record<string, ChipTone> = {
  high: "danger",
  medium: "warn",
  low: "accent",
  info: "neutral",
};

export function SeverityChip({ severity }: { severity: string }) {
  return <Chip tone={SEVERITY_TONE[severity] ?? "neutral"}>{humanize(severity)}</Chip>;
}

const DISPOSITION_TONE: Record<Disposition, ChipTone> = {
  remediated: "ok",
  escalated: "warn",
  below_threshold: "neutral",
  informational: "neutral",
};

const DISPOSITION_LABEL: Record<Disposition, string> = {
  remediated: "Auto-remediated",
  escalated: "Escalated",
  below_threshold: "Below threshold",
  informational: "Informational",
};

export function DispositionChip({ disposition }: { disposition: Disposition }) {
  return <Chip tone={DISPOSITION_TONE[disposition]}>{DISPOSITION_LABEL[disposition]}</Chip>;
}

// --- Stat card ------------------------------------------------------------- //
export function Stat({
  label,
  value,
  foot,
  accent,
}: {
  label: string;
  value: ReactNode;
  foot?: ReactNode;
  accent?: "danger" | "ok" | "warn";
}) {
  return (
    <div className={`stat${accent ? ` accent-${accent}` : ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {foot != null && <div className="stat-foot">{foot}</div>}
    </div>
  );
}

// --- Horizontal bar chart -------------------------------------------------- //
export function BarChart({
  data,
  emptyLabel = "No data",
}: {
  data: Record<string, number>;
  emptyLabel?: string;
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, v]) => v));
  if (entries.length === 0) {
    return <div className="bar-label">{emptyLabel}</div>;
  }
  return (
    <div className="bars">
      {entries.map(([label, value]) => (
        <div className="bar-row" key={label}>
          <span className="bar-label" title={humanize(label)}>
            {humanize(label)}
          </span>
          <span className="bar-track">
            <span className="bar-fill" style={{ width: `${(value / max) * 100}%` }} />
          </span>
          <span className="bar-value">{formatInt(value)}</span>
        </div>
      ))}
    </div>
  );
}

// --- States ---------------------------------------------------------------- //
export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state">
      <span className="spinner" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state state-error">
      <div className="state-title">Couldn’t load this view</div>
      <div>{message}</div>
      {onRetry && (
        <button className="btn btn-ghost" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function Empty({ message }: { message: string }) {
  return (
    <div className="state">
      <span>{message}</span>
    </div>
  );
}
