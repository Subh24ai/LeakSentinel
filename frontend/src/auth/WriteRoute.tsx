import { Navigate, Outlet } from "react-router-dom";

import { useAuth } from "./AuthContext";

/** Ops/admin-only gate (sits inside ProtectedRoute). Viewers are sent home. */
export function WriteRoute() {
  const { user } = useAuth();
  if (user?.role !== "admin" && user?.role !== "ops") {
    return <Navigate to="/" replace />;
  }
  return <Outlet />;
}
