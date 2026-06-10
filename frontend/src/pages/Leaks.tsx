import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, type LeakItem } from "../api";
import {
  BarChart,
  DispositionChip,
  Empty,
  ErrorState,
  Loading,
  SeverityChip,
} from "../components/ui";
import { formatMoney, humanize } from "../lib/format";
import { useAsync } from "../lib/useAsync";

interface Filters {
  status: string;
  insurer: string;
  reason_code: string;
  severity: string;
}

const EMPTY: Filters = { status: "", insurer: "", reason_code: "", severity: "" };

export function Leaks() {
  const navigate = useNavigate();
  const [filters, setFilters] = useState<Filters>(EMPTY);

  // Unfiltered fetch (once) to populate the filter dropdown options.
  const all = useAsync(() => api.getLeaks({}), []);
  // Filtered fetch for the table — re-runs whenever a filter changes.
  const { data, loading, error, reload } = useAsync(
    () => api.getLeaks(filters),
    [filters.status, filters.insurer, filters.reason_code, filters.severity],
  );

  const options = useMemo(() => buildOptions(all.data?.items ?? []), [all.data]);
  const hasFilter = Object.values(filters).some(Boolean);

  function set(key: keyof Filters, value: string) {
    setFilters((f) => ({ ...f, [key]: value }));
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Findings</div>
          <h1 className="page-title">Detected leaks</h1>
          <p className="page-sub">
            Every flagged policy with a plain-English reason and the rupee amount at
            stake. Select a row for the full reconciliation and any action taken.
          </p>
        </div>
      </div>

      <div className="filters">
        <Select label="Status" value={filters.status} options={options.status} onChange={(v) => set("status", v)} />
        <Select label="Insurer" value={filters.insurer} options={options.insurer} onChange={(v) => set("insurer", v)} />
        <Select label="Reason code" value={filters.reason_code} options={options.reason_code} onChange={(v) => set("reason_code", v)} humanizeLabels />
        <Select label="Severity" value={filters.severity} options={options.severity} onChange={(v) => set("severity", v)} humanizeLabels />
        {hasFilter && (
          <button className="btn btn-ghost" onClick={() => setFilters(EMPTY)}>
            Clear filters
          </button>
        )}
      </div>

      {loading && !data && <Loading label="Loading leaks…" />}
      {error && <ErrorState message={error} onRetry={reload} />}

      {data && (
        <>
          <div style={{ color: "var(--ink-2)", fontSize: 13, marginBottom: 12 }}>
            {data.count} {data.count === 1 ? "leak" : "leaks"}
            {hasFilter ? " matching filters" : ""}
          </div>
          {data.items.length === 0 ? (
            <Empty message="No leaks match these filters." />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Policy</th>
                    <th>Insurer</th>
                    <th>Reason</th>
                    <th>Explanation</th>
                    <th className="col-num">Amount</th>
                    <th>Severity</th>
                    <th>Disposition</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((it) => (
                    <tr
                      key={it.policy_no}
                      className="clickable"
                      onClick={() => navigate(`/leaks/${encodeURIComponent(it.policy_no)}`)}
                    >
                      <td className="mono">{it.policy_no}</td>
                      <td>{it.insurer ?? "—"}</td>
                      <td>{humanize(it.reason_code)}</td>
                      <td className="cell-explain">{it.explanation}</td>
                      <td className="col-num">{formatMoney(it.amount)}</td>
                      <td>
                        <SeverityChip severity={it.severity} />
                      </td>
                      <td>
                        <DispositionChip disposition={it.disposition} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {!hasFilter && data.items.length > 0 && (
            <section className="card card-pad section-gap">
              <h2 className="card-title">By reason code</h2>
              <BarChart data={countBy(data.items, (i) => i.reason_code)} />
            </section>
          )}
        </>
      )}
    </>
  );
}

function Select({
  label,
  value,
  options,
  onChange,
  humanizeLabels,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
  humanizeLabels?: boolean;
}) {
  return (
    <div className="field">
      <label>{label}</label>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">All</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {humanizeLabels ? humanize(o) : o}
          </option>
        ))}
      </select>
    </div>
  );
}

function buildOptions(items: LeakItem[]) {
  const uniq = (vals: (string | null)[]) =>
    [...new Set(vals.filter((v): v is string => !!v))].sort();
  return {
    status: uniq(items.map((i) => i.recon_status)),
    insurer: uniq(items.map((i) => i.insurer)),
    reason_code: uniq(items.map((i) => i.reason_code)),
    severity: uniq(items.map((i) => i.severity)),
  };
}

function countBy(items: LeakItem[], key: (i: LeakItem) => string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const it of items) out[key(it)] = (out[key(it)] ?? 0) + 1;
  return out;
}
