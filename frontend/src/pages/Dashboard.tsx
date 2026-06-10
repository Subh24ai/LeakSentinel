import { useState } from "react";

import { api, ApiError, type DispositionCounts, type ReconcileSummary } from "../api";
import { BarChart, ErrorState, Loading, Stat } from "../components/ui";
import { IconRefresh } from "../components/icons";
import { formatInt, formatMoney } from "../lib/format";
import { useAsync } from "../lib/useAsync";

const DISPOSITIONS = [
  { key: "auto_remediated", label: "Auto-remediated", cls: "seg-remediated" },
  { key: "escalated", label: "Escalated", cls: "seg-escalated" },
  { key: "below_threshold", label: "Below threshold", cls: "seg-below_threshold" },
  { key: "informational", label: "Informational", cls: "seg-informational" },
] as const;

export function Dashboard() {
  const { data: metrics, loading, error, reload } = useAsync(() => api.getMetrics());

  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<ReconcileSummary | null>(null);

  async function runReconcile() {
    setRunning(true);
    setRunError(null);
    try {
      const summary = await api.runReconcile();
      setLastRun(summary);
      reload();
    } catch (e) {
      setRunError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Operational intelligence</div>
          <h1 className="page-title">Commission leakage overview</h1>
          <p className="page-sub">
            Where commission owed by insurers has gone unpaid, underpaid, or
            duplicated — and what the system has done about it.
          </p>
        </div>
        <button className="btn btn-primary" onClick={runReconcile} disabled={running}>
          {running ? <span className="spinner" /> : <IconRefresh className="" />}
          {running ? "Reconciling…" : "Run reconciliation"}
        </button>
      </div>

      {lastRun && (
        <div className="toast toast-ok">
          Reconciliation complete — {formatInt(lastRun.policies_processed)} policies
          processed, {formatMoney(lastRun.total_claimed)} claimed (pending confirmation)
          across {lastRun.auto_remediated} auto-remediated and {lastRun.escalated}{" "}
          escalated leaks. Conservation {lastRun.conservation.ok ? "OK" : "MISMATCH"}.
        </div>
      )}
      {runError && (
        <div className="toast" style={{ background: "var(--danger-wash)", color: "var(--danger)" }}>
          Reconciliation failed: {runError}
        </div>
      )}

      {loading && !metrics && <Loading label="Loading metrics…" />}
      {error && !metrics && <ErrorState message={error} onRetry={reload} />}

      {metrics && (
        <>
          <div className="stat-grid">
            <Stat
              label="Underpayment exposure"
              value={formatMoney(metrics.underpayment_exposure)}
              foot={`${formatInt(metrics.policies_with_leaks)} policies — owed to us`}
              accent="danger"
            />
            <Stat
              label="Clawback liability"
              value={formatMoney(metrics.clawback_exposure)}
              foot="Duplicate payments — we owe back"
              accent="warn"
            />
            <Stat
              label="Claimed (pending confirmation)"
              value={formatMoney(metrics.total_claimed)}
              foot={`${formatInt(metrics.claims_count)} claims lodged, not yet settled`}
              accent="ok"
            />
            <Stat
              label="Open escalations"
              value={formatInt(metrics.disposition.escalated)}
              foot="Routed for human review"
            />
          </div>

          <div className="detail-grid">
            <section className="card card-pad">
              <h2 className="card-title">Leaks by insurer</h2>
              <BarChart data={metrics.leaks_by_insurer} />
            </section>
            <section className="card card-pad">
              <h2 className="card-title">Leaks by reason</h2>
              <BarChart data={metrics.leaks_by_reason_code} />
            </section>
          </div>

          <section className="card card-pad section-gap">
            <h2 className="card-title">Disposition</h2>
            <DispositionBar counts={metrics.disposition} />
          </section>
        </>
      )}
    </>
  );
}

function DispositionBar({ counts }: { counts: DispositionCounts }) {
  const total = DISPOSITIONS.reduce((sum, d) => sum + counts[d.key], 0) || 1;
  return (
    <>
      <div className="stack">
        {DISPOSITIONS.map((d) => {
          const v = counts[d.key] ?? 0;
          if (v === 0) return null;
          return (
            <span
              key={d.key}
              className={d.cls}
              style={{ width: `${(v / total) * 100}%` }}
              title={`${d.label}: ${v}`}
            />
          );
        })}
      </div>
      <div className="legend">
        {DISPOSITIONS.map((d) => (
          <div className="legend-item" key={d.key}>
            <span
              className={`legend-swatch ${d.cls}`}
              style={{ background: `var(--${swatchVar(d.cls)})` }}
            />
            <span className="legend-text">
              {d.label} — <b>{formatInt(counts[d.key] ?? 0)}</b>
            </span>
          </div>
        ))}
      </div>
    </>
  );
}

function swatchVar(cls: string): string {
  switch (cls) {
    case "seg-remediated":
      return "ok";
    case "seg-escalated":
      return "warn";
    case "seg-below_threshold":
      return "ink-3";
    default:
      return "border-strong";
  }
}
