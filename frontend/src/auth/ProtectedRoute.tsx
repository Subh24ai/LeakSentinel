import { Navigate, Outlet } from "react-router-dom";

import { useAuth } from "./AuthContext";

/** Gate for authenticated routes: no token → redirect to /login. */
export function ProtectedRoute() {
  const { token } = useAuth();
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  return <Outlet />;
}
