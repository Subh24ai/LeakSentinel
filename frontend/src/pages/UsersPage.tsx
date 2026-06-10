import { useState, type FormEvent } from "react";

import { ApiError, api, type Role } from "../api";
import { Chip, ErrorState, Loading } from "../components/ui";
import { useAuth } from "../auth/AuthContext";
import { formatDateTime } from "../lib/format";
import { useAsync } from "../lib/useAsync";

const ROLES: Role[] = ["admin", "ops", "viewer"];

export function UsersPage() {
  const { user: me } = useAuth();
  const { data: users, loading, error, reload } = useAsync(() => api.getUsers(), []);
  const [showAdd, setShowAdd] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function run(fn: () => Promise<unknown>) {
    setActionError(null);
    try {
      await fn();
      reload();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Administration</div>
          <h1 className="page-title">Users</h1>
          <p className="page-sub">
            Create operators, set roles, and deactivate accounts. New users get a
            temporary password they must change on first login.
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => setShowAdd((s) => !s)}>
          {showAdd ? "Close" : "Add user"}
        </button>
      </div>

      {actionError && (
        <div className="toast" style={{ background: "var(--danger-wash)", color: "var(--danger)" }}>
          {actionError}
        </div>
      )}

      {showAdd && (
        <AddUserForm
          onCreated={() => {
            setShowAdd(false);
            setActionError(null);
            reload();
          }}
          onError={setActionError}
        />
      )}

      {loading && !users && <Loading label="Loading users…" />}
      {error && <ErrorState message={error} onRetry={reload} />}

      {users && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Email</th>
                <th>Role</th>
                <th>Status</th>
                <th>Last login</th>
                <th>Created by</th>
                <th>Password</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const isSelf = me?.id === u.id;
                return (
                  <tr key={u.id}>
                    <td className="mono">
                      {u.email}
                      {isSelf && <span className="self-tag">you</span>}
                    </td>
                    <td>
                      <select
                        value={u.role}
                        disabled={isSelf}
                        title={isSelf ? "You cannot change your own role" : "Change role"}
                        onChange={(e) =>
                          run(() => api.patchUser(u.id, { role: e.target.value as Role }))
                        }
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      {u.is_active ? (
                        <Chip tone="ok">Active</Chip>
                      ) : (
                        <Chip tone="neutral">Inactive</Chip>
                      )}
                    </td>
                    <td>{formatDateTime(u.last_login_at)}</td>
                    <td>{u.created_by ?? "—"}</td>
                    <td>
                      {u.must_change_password ? (
                        <Chip tone="warn">Must change</Chip>
                      ) : (
                        <Chip tone="neutral">Set</Chip>
                      )}
                    </td>
                    <td className="col-actions">
                      <button
                        className="btn btn-ghost btn-sm"
                        disabled={isSelf}
                        title={isSelf ? "You cannot deactivate yourself" : undefined}
                        onClick={() =>
                          run(() =>
                            u.is_active
                              ? api.deactivateUser(u.id)
                              : api.patchUser(u.id, { is_active: true }),
                          )
                        }
                      >
                        {u.is_active ? "Deactivate" : "Reactivate"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function AddUserForm({
  onCreated,
  onError,
}: {
  onCreated: () => void;
  onError: (msg: string) => void;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [submitting, setSubmitting] = useState(false);

  const canSubmit = email.includes("@") && password.length >= 8 && !submitting;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await api.registerUser(email.trim(), password, role);
      onCreated();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="card card-pad add-user" onSubmit={onSubmit}>
      <div className="add-user-fields">
        <label className="field">
          <span>Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="ops@leaksentinel.local"
            required
          />
        </label>
        <label className="field">
          <span>Temporary password</span>
          <input
            type="text"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="at least 8 characters"
            required
            minLength={8}
          />
        </label>
        <label className="field">
          <span>Role</span>
          <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        <button className="btn btn-primary" type="submit" disabled={!canSubmit}>
          {submitting ? "Creating…" : "Create user"}
        </button>
      </div>
      <p className="add-user-hint">
        The user will sign in with this temporary password and be required to
        change it immediately.
      </p>
    </form>
  );
}
