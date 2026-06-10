import { useState } from "react";

import { api, type AuditItem, type EscalationItem } from "../api";
import { Chip, Empty, ErrorState, Loading } from "../components/ui";
import { formatDateTime, formatMoney, humanize } from "../lib/format";
import { useAsync } from "../lib/useAsync";

type Tab = "audit" | "escalations";

export function AuditEscalations() {
  const [tab, setTab] = useState<Tab>("audit");

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Governance</div>
          <h1 className="page-title">Audit &amp; escalations</h1>
          <p className="page-sub">
            The immutable trail of every action and every refusal, and the queue of
            findings handed to a human.
          </p>
        </div>
      </div>

      <div className="tabs" role="tablist">
        <button
          className={`tab${tab === "audit" ? " active" : ""}`}
          onClick={() => setTab("audit")}
          role="tab"
          aria-selected={tab === "audit"}
        >
          Audit log
        </button>
        <button
          className={`tab${tab === "escalations" ? " active" : ""}`}
          onClick={() => setTab("escalations")}
          role="tab"
          aria-selected={tab === "escalations"}
        >
          Escalation queue
        </button>
      </div>

      {tab === "audit" ? <AuditLog /> : <EscalationQueue />}
    </>
  );
}

function AuditLog() {
  const { data, loading, error, reload } = useAsync(() => api.getAudit(200), []);

  if (loading && !data) return <Loading label="Loading audit log…" />;
  if (error) return <ErrorState message={error} onRetry={reload} />;
  if (!data || data.length === 0) return <Empty message="No audit entries yet — run a reconciliation." />;

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>When</th>
            <th>Action</th>
            <th>Actor</th>
            <th>Detail</th>
            <th>Payload hash</th>
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <AuditRow key={row.id} row={row} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AuditRow({ row }: { row: AuditItem }) {
  const blocked = row.action.includes("BLOCKED");
  return (
    <tr className={blocked ? "row-blocked" : undefined}>
      <td style={{ whiteSpace: "nowrap", color: "var(--ink-2)" }}>
        {formatDateTime(row.created_at)}
      </td>
      <td>
        {blocked ? (
          <Chip tone="danger">Blocked</Chip>
        ) : (
          <span className="mono" style={{ fontSize: 12 }}>
            {row.action}
          </span>
        )}
      </td>
      <td style={{ color: "var(--ink-2)" }}>{row.actor ?? "—"}</td>
      <td className="cell-explain">{row.detail ?? "—"}</td>
      <td className="mono" style={{ fontSize: 11.5, color: "var(--ink-3)" }}>
        {row.payload_sha256 ? row.payload_sha256.slice(0, 12) + "…" : "—"}
      </td>
    </tr>
  );
}

function EscalationQueue() {
  const { data, loading, error, reload } = useAsync(() => api.getEscalations(), []);

  if (loading && !data) return <Loading label="Loading escalations…" />;
  if (error) return <ErrorState message={error} onRetry={reload} />;
  if (!data || data.length === 0) return <Empty message="The human queue is empty." />;

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Policy</th>
            <th>Reason</th>
            <th>Escalation reason</th>
            <th className="col-num">Amount</th>
            <th>Status</th>
            <th>Raised</th>
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <EscalationRow key={row.id} row={row} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EscalationRow({ row }: { row: EscalationItem }) {
  const [open, setOpen] = useState(false);
  const hasContext = row.finding_context && Object.keys(row.finding_context).length > 0;
  return (
    <>
      <tr
        className={hasContext ? "clickable" : undefined}
        onClick={hasContext ? () => setOpen((o) => !o) : undefined}
      >
        <td className="mono">{row.policy_no}</td>
        <td>{humanize(row.reason_code)}</td>
        <td>
          <Chip tone="warn">{humanize(row.escalation_reason)}</Chip>
        </td>
        <td className="col-num">{formatMoney(row.amount)}</td>
        <td style={{ color: "var(--ink-2)" }}>{humanize(row.status)}</td>
        <td style={{ whiteSpace: "nowrap", color: "var(--ink-2)" }}>
          {formatDateTime(row.created_at)}
        </td>
      </tr>
      {open && hasContext && (
        <tr>
          <td colSpan={6} style={{ background: "var(--surface-2)" }}>
            <div className="context-json">{JSON.stringify(row.finding_context, null, 2)}</div>
          </td>
        </tr>
      )}
    </>
  );
}
