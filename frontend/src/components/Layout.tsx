import { NavLink, Outlet } from "react-router-dom";

import { IconAudit, IconDashboard, IconLeaks } from "./icons";

export function Layout() {
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
