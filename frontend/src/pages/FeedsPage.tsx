import { useEffect, useRef, useState, type DragEvent } from "react";

import { ApiError, api, type FeedUpload } from "../api";
import { Chip, Empty, ErrorState, Loading } from "../components/ui";
import { formatDateTime, humanize } from "../lib/format";
import { useAsync } from "../lib/useAsync";

// Display names must match the backend normalizer registry keys.
const INSURERS = ["ICICI Lombard", "Bajaj", "Digit", "Tata AIG"];

const STATUS_TONE: Record<string, "neutral" | "warn" | "ok" | "danger"> = {
  uploaded: "neutral",
  processing: "warn",
  processed: "ok",
  failed: "danger",
};

function StatusBadge({ status }: { status: string }) {
  return <Chip tone={STATUS_TONE[status] ?? "neutral"}>{humanize(status)}</Chip>;
}

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function FeedsPage() {
  const { data: feeds, loading, error, reload } = useAsync(() => api.getFeeds(), []);

  // Auto-refresh while anything is still in flight.
  useEffect(() => {
    const inFlight = feeds?.some(
      (f) => f.status === "processing" || f.status === "uploaded",
    );
    if (!inFlight) return;
    const t = setInterval(reload, 5000);
    return () => clearInterval(t);
  }, [feeds, reload]);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Ingestion</div>
          <h1 className="page-title">Insurer feeds</h1>
          <p className="page-sub">
            Upload a commission statement (CSV or PDF) for a known insurer. It's
            normalized and loaded immediately; the history shows the outcome.
          </p>
        </div>
      </div>

      <div className="feeds-grid">
        <UploadPanel onUploaded={reload} />

        <section className="card card-pad">
          <h2 className="card-title">Upload history</h2>
          {loading && !feeds && <Loading label="Loading uploads…" />}
          {error && <ErrorState message={error} onRetry={reload} />}
          {feeds &&
            (feeds.length === 0 ? (
              <Empty message="No feeds uploaded yet. Upload your first insurer feed." />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Filename</th>
                      <th>Insurer</th>
                      <th>Type</th>
                      <th>Status</th>
                      <th className="col-num">Rows loaded</th>
                      <th>Uploaded by</th>
                      <th>Uploaded at</th>
                    </tr>
                  </thead>
                  <tbody>
                    {feeds.map((f) => (
                      <tr key={f.id}>
                        <td className="mono">{f.filename}</td>
                        <td>{f.insurer_name}</td>
                        <td className="uppercase">{f.file_type}</td>
                        <td>
                          <StatusBadge status={f.status} />
                          {f.status === "failed" && f.error_message && (
                            <div className="feed-error" title={f.error_message}>
                              {f.error_message}
                            </div>
                          )}
                        </td>
                        <td className="col-num">{f.rows_loaded ?? "—"}</td>
                        <td>{f.uploaded_by}</td>
                        <td>{formatDateTime(f.uploaded_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
        </section>
      </div>
    </>
  );
}

function UploadPanel({ onUploaded }: { onUploaded: () => void }) {
  const [insurer, setInsurer] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [phase, setPhase] = useState<"idle" | "uploading" | "done" | "error">("idle");
  const [result, setResult] = useState<FeedUpload | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function pickFile(f: File | null) {
    setFileError(null);
    setResult(null);
    setUploadError(null);
    setPhase("idle");
    if (!f) {
      setFile(null);
      return;
    }
    const ext = f.name.toLowerCase().split(".").pop();
    if (ext !== "csv" && ext !== "pdf") {
      setFileError("Only .csv and .pdf files are accepted.");
      setFile(null);
      return;
    }
    setFile(f);
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragOver(false);
    pickFile(e.dataTransfer.files?.[0] ?? null);
  }

  async function upload() {
    if (!insurer || !file) return;
    setPhase("uploading");
    setUploadError(null);
    setResult(null);
    try {
      const r = await api.uploadFeed(file, insurer);
      setResult(r);
      if (r.status === "failed") {
        setPhase("error");
        setUploadError(r.error_message ?? "Processing failed.");
      } else {
        setPhase("done");
      }
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      onUploaded();
    } catch (e) {
      setPhase("error");
      setUploadError(e instanceof ApiError ? e.message : String(e));
    }
  }

  const canUpload = !!insurer && !!file && phase !== "uploading";

  return (
    <section className="card card-pad upload-panel">
      <h2 className="card-title">Upload feed</h2>

      <label className="field">
        <span>Insurer</span>
        <select value={insurer} onChange={(e) => setInsurer(e.target.value)}>
          <option value="">Select insurer…</option>
          {INSURERS.map((i) => (
            <option key={i} value={i}>
              {i}
            </option>
          ))}
        </select>
      </label>

      <div
        className={`dropzone${dragOver ? " dropzone-over" : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        role="button"
        tabIndex={0}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".csv,.pdf"
          style={{ display: "none" }}
          onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
        />
        {file ? (
          <div className="dropzone-file">
            <span className="mono">{file.name}</span>
            <span className="dropzone-size">{fmtSize(file.size)}</span>
          </div>
        ) : (
          <div className="dropzone-hint">
            Drop a .csv or .pdf here, or click to choose
          </div>
        )}
      </div>
      {fileError && <div className="field-hint-err">{fileError}</div>}

      <button className="btn btn-primary" disabled={!canUpload} onClick={upload}>
        {phase === "uploading" ? <span className="spinner" /> : null}
        {phase === "uploading" ? "Uploading & processing…" : "Upload"}
      </button>

      {phase === "done" && result && (
        <div className="toast toast-ok">
          Processed <b>{result.filename}</b> — {result.rows_extracted ?? 0} rows
          extracted, <b>{result.rows_loaded ?? 0}</b> loaded.
          {result.rows_loaded === 0 && (
            <> No rows matched the {result.insurer_name} format.</>
          )}
        </div>
      )}
      {phase === "error" && uploadError && (
        <div
          className="toast"
          style={{ background: "var(--danger-wash)", color: "var(--danger)" }}
        >
          {uploadError}
        </div>
      )}
    </section>
  );
}
