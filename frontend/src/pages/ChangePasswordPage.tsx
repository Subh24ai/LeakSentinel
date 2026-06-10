import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../api";
import { useAuth } from "../auth/AuthContext";

const MIN_LEN = 8;

export function ChangePasswordPage() {
  const { token, mustChangePassword, changePassword } = useAuth();
  const navigate = useNavigate();

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Must be authenticated to change a password (but a temporary password is OK —
  // this is the one screen that's reachable while must_change_password is true).
  useEffect(() => {
    if (!token) navigate("/login", { replace: true });
  }, [token, navigate]);

  const tooShort = next.length > 0 && next.length < MIN_LEN;
  const canSubmit = current.length > 0 && next.length >= MIN_LEN && !submitting;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await changePassword(current, next);
      // changePassword navigates to "/" on success.
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.status === 400
            ? "Your current password is incorrect."
            : err.message
          : String(err),
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-shell">
      <form className="login-card card card-pad" onSubmit={onSubmit}>
        <div className="login-brand">
          <div className="brand-mark">L</div>
          <div>
            <div className="brand-name">LeakSentinel</div>
            <div className="brand-sub">Commission recovery</div>
          </div>
        </div>

        <h1 className="login-title">Change password</h1>

        {mustChangePassword && (
          <div className="login-banner">
            You must change your password before continuing.
          </div>
        )}

        {error && <div className="login-error">{error}</div>}

        <label className="login-field">
          <span>Current password</span>
          <input
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            required
            autoFocus
          />
        </label>

        <label className="login-field">
          <span>New password</span>
          <input
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            required
            minLength={MIN_LEN}
          />
          {tooShort && (
            <small className="field-hint-err">At least {MIN_LEN} characters.</small>
          )}
        </label>

        <button
          className="btn btn-primary login-submit"
          type="submit"
          disabled={!canSubmit}
        >
          {submitting ? <span className="spinner" /> : null}
          {submitting ? "Saving…" : "Change password"}
        </button>
      </form>
    </div>
  );
}
