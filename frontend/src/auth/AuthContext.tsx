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
  mustChangePassword: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string) => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | undefined>(undefined);

/**
 * Holds the JWT and the current user in React state (in memory only — never
 * localStorage). Mirrors the token into the API client so requests carry
 * `Authorization: Bearer …`, wires the API's 401 handler to log out + redirect,
 * and routes a temporary-password user to the change-password screen.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [mustChangePassword, setMustChangePassword] = useState(false);
  const navigate = useNavigate();

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
    setMustChangePassword(false);
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

  const login = useCallback(
    async (email: string, password: string) => {
      const tok = await api.login(email, password);
      // Set on the client immediately so follow-up calls carry the token.
      setAuthToken(tok.access_token);
      setToken(tok.access_token);
      setMustChangePassword(tok.must_change_password);

      if (tok.must_change_password) {
        // Don't fetch /auth/me — it's password-gated. Go straight to the gate.
        navigate("/change-password", { replace: true });
        return;
      }
      try {
        setUser(await api.getMe());
      } catch {
        setUser(null);
      }
      navigate("/", { replace: true });
    },
    [navigate],
  );

  const signup = useCallback(
    async (email: string, password: string) => {
      // Sign up returns a token (self-chosen password ⇒ never must-change), so
      // the new user lands straight in the app, exactly like a fresh login.
      const tok = await api.signup(email, password);
      setAuthToken(tok.access_token);
      setToken(tok.access_token);
      setMustChangePassword(false);
      try {
        setUser(await api.getMe());
      } catch {
        setUser(null);
      }
      navigate("/", { replace: true });
    },
    [navigate],
  );

  const changePassword = useCallback(
    async (currentPassword: string, newPassword: string) => {
      await api.changePassword(currentPassword, newPassword);
      setMustChangePassword(false);
      try {
        setUser(await api.getMe());
      } catch {
        /* keep whatever user we have */
      }
      navigate("/", { replace: true });
    },
    [navigate],
  );

  const value = useMemo(
    () => ({ token, user, mustChangePassword, login, signup, changePassword, logout }),
    [token, user, mustChangePassword, login, signup, changePassword, logout],
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
