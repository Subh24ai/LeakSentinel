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

export interface FeedUpload {
  id: number;
  filename: string;
  insurer_name: string;
  file_type: string;
  file_size_bytes: number;
  status: string; // uploaded | processing | processed | failed
  rows_extracted: number | null;
  rows_loaded: number | null;
  error_message: string | null;
  uploaded_by: string;
  uploaded_at: string;
  processed_at: string | null;
}

export interface ReconcileJob {
  id: string;
  status: string; // queued | running | complete | failed
  triggered_by: string;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  summary: ReconcileSummary | null;
}

export interface ReconcileAccepted {
  job_id: string;
  status: string;
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

// --- auth token (in memory only — never localStorage) ---------------------- //
// The token's source of truth is React state in AuthContext; it is mirrored here
// so the (non-React) fetch wrapper can attach the Authorization header. On logout
// or a page refresh both clear — nothing is persisted to disk.
let authToken: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setAuthToken(token: string | null): void {
  authToken = token;
}

/** Register the handler invoked on any 401 (AuthContext wires this to logout). */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

// --- auth response types --------------------------------------------------- //
export type Role = "admin" | "ops" | "viewer";

export interface Token {
  access_token: string;
  token_type: string;
  must_change_password: boolean;
}

export interface AuthUser {
  id: number;
  email: string;
  role: string;
  must_change_password: boolean;
}

export interface UserAdmin {
  id: number;
  email: string;
  role: Role;
  is_active: boolean;
  must_change_password: boolean;
  created_by: string | null;
  last_login_at: string | null;
  created_at: string;
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

async function request<T>(
  path: string,
  init?: RequestInit,
  opts?: { skipAuthRedirect?: boolean },
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (authToken) headers["Authorization"] = `Bearer ${authToken}`;

  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(
      0,
      `Cannot reach the API at ${API_BASE}. Is the backend running?`,
    );
  }

  // 401 interceptor: any expired/invalid token logs the user out and bounces
  // them to /login. Skipped for the login call itself (a bad password is a form
  // error, not a session expiry).
  if (resp.status === 401 && !opts?.skipAuthRedirect) {
    onUnauthorized?.();
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

  // OAuth2 password flow: the token endpoint expects form-encoded credentials.
  login: (email: string, password: string) => {
    const body = new URLSearchParams({ username: email, password });
    return request<Token>(
      "/auth/token",
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      },
      { skipAuthRedirect: true },
    );
  },

  getMe: () => request<AuthUser>("/auth/me"),

  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ status: string }>("/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    }),

  // --- user management (admin) --- //
  getUsers: () => request<UserAdmin[]>("/auth/users"),

  registerUser: (email: string, password: string, role: Role) =>
    request<UserAdmin>("/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, role }),
    }),

  patchUser: (id: number, patch: { role?: Role; is_active?: boolean }) =>
    request<UserAdmin>(`/auth/users/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),

  deactivateUser: (id: number) =>
    request<UserAdmin>(`/auth/users/${id}`, { method: "DELETE" }),

  getMetrics: () => request<Metrics>("/metrics"),

  // Async reconcile: enqueue a job (202), then poll for completion.
  runReconcile: () => request<ReconcileAccepted>("/reconcile", { method: "POST" }),
  getReconcileJob: (jobId: string) => request<ReconcileJob>(`/reconcile/${jobId}`),
  getReconcileJobs: () => request<ReconcileJob[]>("/reconcile/jobs"),

  // Feed uploads (multipart; the browser sets the Content-Type boundary).
  uploadFeed: (file: File, insurer: string) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("insurer_name", insurer);
    return request<FeedUpload>("/feeds/upload", { method: "POST", body: fd });
  },
  getFeeds: () => request<FeedUpload[]>("/feeds"),
  getFeed: (id: number) => request<FeedUpload>(`/feeds/${id}`),

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
