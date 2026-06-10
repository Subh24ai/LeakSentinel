import { Navigate, Outlet } from "react-router-dom";

import { useAuth } from "./AuthContext";

/** Admin-only gate (sits inside ProtectedRoute). Non-admins are sent home. */
export function AdminRoute() {
  const { user } = useAuth();
  if (user?.role !== "admin") {
    return <Navigate to="/" replace />;
  }
  return <Outlet />;
}
