// Typed client for the LeakSentinel FastAPI backend.
//
// Types mirror the Pydantic v2 response models in `leaksentinel/api/schemas.py`.
// Money fields arrive as fixed-2dp STRINGS (e.g. "7736.14") and are kept as
// strings end-to-end — never parsed to float. Format them with `formatMoney`.

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

// --- Response types -------------------------------------------------------- //
export type Disposition =
  | "remediated"
  | "escalated"
  | "below_threshold"
  | "informational";

export interface LeakItem {
  policy_no: string;
  insurer: string | null;
  recon_status: string | null;
  reason_code: string;
  severity: string;
  explanation: string;
  amount: string; // money
  resolution_state: string | null;
  disposition: Disposition;
  detector: string;
  score: number | null;
}

export interface LeakList {
  count: number;
  filters: Record<string, string | null>;
  items: LeakItem[];
}

export interface ReconView {
  status: string | null;
  reason_code: string | null;
  expected: string | null; // money
  actual: string | null; // money
  delta: string | null; // money
  resolution_state: string | null;
}

export interface ActionTaken {
  kind: "claim" | "escalation";
  id: number;
  status: string;
  amount: string | null; // money
  reason: string | null;
  ref: string | null;
  created_at: string | null;
}

export interface LeakDetail {
  policy_no: string;
  insurer: string | null;
  recon: ReconView | null;
  finding: LeakItem | null;
  actions: ActionTaken[];
}

export interface ClaimItem {
  id: number;
  policy_no: string;
  claim_amount: string; // money
  reason_code: string;
  idempotency_key: string;
  status: string;
  external_ref: string | null;
  created_at: string;
}

export interface EscalationItem {
  id: number;
  policy_no: string;
  reason_code: string;
  escalation_reason: string;
  amount: string | null; // money
  status: string;
  finding_context: Record<string, unknown>;
  created_at: string;
}

export interface AuditItem {
  id: number;
  action: string;
  payload_sha256: string | null;
  actor: string | null;
  detail: string | null;
  created_at: string;
}

export interface ConservationCheck {
  accounted: number;
  total: number;
  ok: boolean;
}

export interface ReconcileSummary {
  policies_processed: number;
  leaks_by_reason: Record<string, number>;
  auto_remediated: number;
  escalated: number;
  below_threshold: number;
  informational: number;
  underpayment_exposure: string; // money owed TO us
  clawback_exposure: string; // money we owe BACK (duplicates)
  total_claimed: string; // money lodged, not confirmed received
  conservation: ConservationCheck;
}

export interface DispositionCounts {
  auto_remediated: number;
  escalated: number;
  below_threshold: number;
  informational: number;
}

export interface Metrics {
  policies_processed: number;
  policies_with_leaks: number;
  underpayment_exposure: string; // money owed TO us
  clawback_exposure: string; // money we owe BACK (duplicates)
  total_claimed: string; // money lodged, not confirmed received
  claims_count: number;
  leaks_by_insurer: Record<string, number>;
  leaks_by_reason_code: Record<string, number>;
  disposition: DispositionCounts;
}

// --- fetch wrapper --------------------------------------------------------- //
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      headers: { Accept: "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError(
      0,
      `Cannot reach the API at ${API_BASE}. Is the backend running?`,
    );
  }

  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const body = await resp.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* non-JSON error body; keep the status line */
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

function query(params: Record<string, string | null | undefined>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== "") usp.set(k, v);
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

// --- endpoints ------------------------------------------------------------- //
export const api = {
  base: API_BASE,

  getMetrics: () => request<Metrics>("/metrics"),

  runReconcile: () => request<ReconcileSummary>("/reconcile", { method: "POST" }),

  getLeaks: (filters: {
    status?: string;
    insurer?: string;
    reason_code?: string;
    severity?: string;
  }) => request<LeakList>(`/leaks${query(filters)}`),

  getLeak: (policyNo: string) =>
    request<LeakDetail>(`/leaks/${encodeURIComponent(policyNo)}`),

  getClaims: () => request<ClaimItem[]>("/claims"),

  getEscalations: () => request<EscalationItem[]>("/escalations"),

  getAudit: (limit = 200) => request<AuditItem[]>(`/audit?limit=${limit}`),
};
