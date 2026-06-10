import { Route, Routes } from "react-router-dom";

import { AdminRoute } from "./auth/AdminRoute";
import { AuthProvider } from "./auth/AuthContext";
import { LoginPage } from "./auth/LoginPage";
import { ProtectedRoute } from "./auth/ProtectedRoute";
import { Layout } from "./components/Layout";
import { AuditEscalations } from "./pages/AuditEscalations";
import { ChangePasswordPage } from "./pages/ChangePasswordPage";
import { Dashboard } from "./pages/Dashboard";
import { LeakDetail } from "./pages/LeakDetail";
import { Leaks } from "./pages/Leaks";
import { UsersPage } from "./pages/UsersPage";

export function App() {
  return (
    <AuthProvider>
      <Routes>
        {/* Public */}
        <Route path="/login" element={<LoginPage />} />

        {/* Authenticated, but reachable even with a temporary password (it's the
            one screen the password-change gate must not block). */}
        <Route path="/change-password" element={<ChangePasswordPage />} />

        {/* Everything else requires a token AND a changed password */}
        <Route element={<ProtectedRoute />}>
          <Route element={<Layout />}>
            <Route index element={<Dashboard />} />
            <Route path="leaks" element={<Leaks />} />
            <Route path="leaks/:policyNo" element={<LeakDetail />} />
            <Route path="audit" element={<AuditEscalations />} />
            {/* Admin only */}
            <Route element={<AdminRoute />}>
              <Route path="users" element={<UsersPage />} />
            </Route>
          </Route>
        </Route>
      </Routes>
    </AuthProvider>
  );
}
