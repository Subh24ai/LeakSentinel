import { Link, useParams } from "react-router-dom";

import { api, type ActionTaken } from "../api";
import {
  Chip,
  DispositionChip,
  ErrorState,
  Loading,
  SeverityChip,
} from "../components/ui";
import { IconArrowLeft } from "../components/icons";
import { formatDateTime, formatMoney, humanize } from "../lib/format";
import { useAsync } from "../lib/useAsync";

export function LeakDetail() {
  const { policyNo = "" } = useParams();
  const { data, loading, error, reload } = useAsync(
    () => api.getLeak(policyNo),
    [policyNo],
  );

  return (
    <>
      <Link to="/leaks" className="back-link">
        <IconArrowLeft className="" />
        Back to leaks
      </Link>

      {loading && !data && <Loading label="Loading policy…" />}
      {error && <ErrorState message={error} onRetry={reload} />}

      {data && (
        <>
          <div className="page-head">
            <div>
              <div className="eyebrow">Policy</div>
              <h1 className="page-title mono" style={{ fontFamily: "var(--mono)", fontSize: 24 }}>
                {data.policy_no}
              </h1>
              <p className="page-sub">{data.insurer ?? "Unknown insurer"}</p>
            </div>
            {data.finding && (
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <SeverityChip severity={data.finding.severity} />
                <DispositionChip disposition={data.finding.disposition} />
              </div>
            )}
          </div>

          <div className="detail-grid">
            <section className="card card-pad">
              <h2 className="card-title">Finding</h2>
              {data.finding ? (
                <dl className="kv">
                  <dt>Reason</dt>
                  <dd>{humanize(data.finding.reason_code)}</dd>
                  <dt>Amount at risk</dt>
                  <dd className="num">{formatMoney(data.finding.amount)}</dd>
                  <dt>Explanation</dt>
                  <dd>{data.finding.explanation}</dd>
                  <dt>Detector</dt>
                  <dd className="mono">{data.finding.detector}</dd>
                  {data.finding.score != null && (
                    <>
                      <dt>Anomaly score</dt>
                      <dd className="num">{data.finding.score.toFixed(3)}</dd>
                    </>
                  )}
                </dl>
              ) : (
                <p style={{ color: "var(--ink-2)" }}>
                  No leak detected for this policy — it reconciled cleanly.
                </p>
              )}
            </section>

            <section className="card card-pad">
              <h2 className="card-title">Reconciliation</h2>
              {data.recon ? (
                <dl className="kv">
                  <dt>Class</dt>
                  <dd>{humanize(data.recon.status)}</dd>
                  <dt>Reason code</dt>
                  <dd>{data.recon.reason_code ?? "—"}</dd>
                  <dt>Expected</dt>
                  <dd className="num">{formatMoney(data.recon.expected)}</dd>
                  <dt>Actual</dt>
                  <dd className="num">{formatMoney(data.recon.actual)}</dd>
                  <dt>Delta</dt>
                  <dd className="num">{formatMoney(data.recon.delta)}</dd>
                  <dt>Resolution</dt>
                  <dd>{data.recon.resolution_state ? humanize(data.recon.resolution_state) : "—"}</dd>
                </dl>
              ) : (
                <p style={{ color: "var(--ink-2)" }}>No reconciliation row.</p>
              )}
            </section>
          </div>

          <section className="card card-pad section-gap">
            <h2 className="card-title">Action taken</h2>
            {data.actions.length === 0 ? (
              <p style={{ color: "var(--ink-2)" }}>No action recorded for this policy.</p>
            ) : (
              <div>
                {data.actions.map((a) => (
                  <ActionRow key={`${a.kind}-${a.id}`} action={a} />
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </>
  );
}

function ActionRow({ action }: { action: ActionTaken }) {
  const isClaim = action.kind === "claim";
  return (
    <div className="action-item">
      <Chip tone={isClaim ? "ok" : "warn"}>{isClaim ? "Claim lodged" : "Escalated"}</Chip>
      <div style={{ flex: 1 }}>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "baseline" }}>
          {action.amount != null && (
            <span className="num" style={{ fontWeight: 600 }}>
              {formatMoney(action.amount)}
            </span>
          )}
          <span style={{ color: "var(--ink-2)", fontSize: 13 }}>
            {humanize(action.reason)} · {humanize(action.status)}
          </span>
          {action.created_at && (
            <span style={{ color: "var(--ink-3)", fontSize: 12.5 }}>
              {formatDateTime(action.created_at)}
            </span>
          )}
        </div>
        {action.ref && (
          <div className="mono" style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 3 }}>
            ref {action.ref}
          </div>
        )}
      </div>
    </div>
  );
}
