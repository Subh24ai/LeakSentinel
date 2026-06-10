import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";

import { api, setAuthToken, setUnauthorizedHandler, type AuthUser } from "../api";

interface AuthState {
  token: string | null;
  user: AuthUser | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | undefined>(undefined);

/**
 * Holds the JWT and the current user in React state (in memory only — never
 * localStorage, so a token can't be stolen from disk). Mirrors the token into
 * the API client so requests carry `Authorization: Bearer …`, and wires the
 * API's 401 handler to log out + redirect.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const navigate = useNavigate();

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
    setAuthToken(null);
    navigate("/login", { replace: true });
  }, [navigate]);

  // Keep the API client's token in sync with state.
  useEffect(() => {
    setAuthToken(token);
  }, [token]);

  // Any 401 from the API auto-logs-out and bounces to /login.
  useEffect(() => {
    setUnauthorizedHandler(logout);
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  const login = useCallback(async (email: string, password: string) => {
    const tok = await api.login(email, password);
    // Set the token on the client immediately so the /auth/me call carries it
    // (React state updates are async and wouldn't be visible yet).
    setAuthToken(tok.access_token);
    setToken(tok.access_token);
    try {
      setUser(await api.getMe());
    } catch {
      // A token without a usable /auth/me is still a valid session; leave user
      // null rather than failing the whole login.
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({ token, user, login, logout }),
    [token, user, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (ctx === undefined) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
