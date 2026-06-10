import { Navigate, Outlet } from "react-router-dom";

import { useAuth } from "./AuthContext";

/**
 * Gate for authenticated app routes: no token → /login; a temporary password
 * that hasn't been changed → /change-password (the user can't use the app until
 * they set their own password).
 */
export function ProtectedRoute() {
  const { token, mustChangePassword } = useAuth();
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  if (mustChangePassword) {
    return <Navigate to="/change-password" replace />;
  }
  return <Outlet />;
}
