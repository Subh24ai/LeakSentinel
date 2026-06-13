import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../api";
import { useAuth } from "./AuthContext";

export function RegisterPage() {
  // signup() mints a token and navigates to "/", so this page only collects the
  // new account's email + password (and confirms the two passwords match).
  const { signup } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    setSubmitting(true);
    try {
      await signup(email, password);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 409
          ? "An account with that email already exists."
          : err instanceof ApiError
            ? err.message
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

        <h1 className="login-title">Create your account</h1>
        <p className="login-sub">Sign up to start tracking commission leakage.</p>

        {error && <div className="login-error">{error}</div>}

        <label className="login-field">
          <span>Email</span>
          <input
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoFocus
          />
        </label>

        <label className="login-field">
          <span>Password</span>
          <input
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={8}
            required
          />
        </label>

        <label className="login-field">
          <span>Confirm password</span>
          <input
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            minLength={8}
            required
          />
        </label>

        <button
          className="btn btn-primary login-submit"
          type="submit"
          disabled={submitting}
        >
          {submitting ? <span className="spinner" /> : null}
          {submitting ? "Creating account…" : "Create account"}
        </button>

        <p className="login-alt">
          Already have an account? <Link to="/login">Sign in</Link>
        </p>
      </form>
    </div>
  );
}
