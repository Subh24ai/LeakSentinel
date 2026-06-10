import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { IconAudit, IconDashboard, IconLeaks, IconLogout } from "./icons";

export function Layout() {
  const { user, logout } = useAuth();

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">L</div>
          <div>
            <div className="brand-name">LeakSentinel</div>
            <div className="brand-sub">Commission recovery</div>
          </div>
        </div>

        <nav className="nav">
          <div className="nav-label">Operations</div>
          <NavLink to="/" end>
            <IconDashboard />
            Dashboard
          </NavLink>
          <NavLink to="/leaks">
            <IconLeaks />
            Leaks
          </NavLink>
          <NavLink to="/audit">
            <IconAudit />
            Audit &amp; Escalations
          </NavLink>
        </nav>

        <div className="user-box">
          <div className="user-meta">
            <div className="user-email">{user?.email ?? "Signed in"}</div>
            {user?.role && <div className="user-role">{user.role}</div>}
          </div>
          <button className="btn-logout" onClick={logout} title="Sign out">
            <IconLogout />
            <span>Sign out</span>
          </button>
        </div>

        <div className="sidebar-foot">
          Synthetic data only. No real insurer statements or customer records.
        </div>
      </aside>

      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
