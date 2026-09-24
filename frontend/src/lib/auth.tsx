import {
  createContext,
  use,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { AxiosError } from "axios";
import { api, setAccessToken } from "@/lib/api";
import type { TelegramWidgetUser, User } from "@/lib/types";

type AuthState =
  | { status: "loading" }
  | { status: "anonymous" }
  // The account exists but a manager has not approved it. Distinct from
  // anonymous because showing a login button here would be a dead end.
  | { status: "pending" }
  | { status: "authenticated"; user: User };

type AuthContextValue = {
  state: AuthState;
  loginWithWidget: (payload: TelegramWidgetUser) => Promise<void>;
  magicError: boolean;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

async function loadMe(): Promise<AuthState> {
  try {
    const { data } = await api.get<User>("/auth/me");
    return { status: "authenticated", user: data };
  } catch (error) {
    if (error instanceof AxiosError && error.response?.status === 403) {
      return { status: "pending" };
    }
    return { status: "anonymous" };
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [magicError, setMagicError] = useState(false);
  const boot = useRef<Promise<{ next: AuthState; linkFailed: boolean }> | null>(null);

  useEffect(() => {
    // StrictMode reruns effects in development. Reuse the first request so a
    // one-use magic token is never redeemed twice or beaten by an old cookie.
    if (!boot.current) {
      const magicToken = new URLSearchParams(window.location.hash.slice(1)).get("token");
      if (magicToken) {
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
        boot.current = (async () => {
          try {
            const { data } = await api.post<{ access_token: string }>("/auth/magic/redeem", {
              token: magicToken,
            });
            setAccessToken(data.access_token);
            return { next: await loadMe(), linkFailed: false };
          } catch {
            setAccessToken(null);
            return { next: { status: "anonymous" } as AuthState, linkFailed: true };
          }
        })();
      } else {
        boot.current = (async () => {
          try {
            const { data } = await api.post<{ access_token: string }>("/auth/refresh");
            setAccessToken(data.access_token);
          } catch {
            setAccessToken(null);
          }
          return { next: await loadMe(), linkFailed: false };
        })();
      }
    }
    let cancelled = false;
    void boot.current.then(({ next, linkFailed }) => {
      if (!cancelled) {
        setMagicError(linkFailed);
        setState(next);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const loginWithWidget = useCallback(async (payload: TelegramWidgetUser) => {
    try {
      const { data } = await api.post<{ access_token: string }>("/auth/telegram", payload);
      setAccessToken(data.access_token);
      setState(await loadMe());
    } catch (error) {
      setAccessToken(null);
      if (error instanceof AxiosError && error.response?.status === 403) {
        setState({ status: "pending" });
        return;
      }
      setState({ status: "anonymous" });
      throw error;
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      setAccessToken(null);
      setState({ status: "anonymous" });
    }
  }, []);

  const value = useMemo(
    () => ({ state, loginWithWidget, magicError, logout }),
    [state, loginWithWidget, magicError, logout],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}

export function useAuth(): AuthContextValue {
  const context = use(AuthContext);
  if (!context) throw new Error("useAuth must be used inside <AuthProvider>");
  return context;
}
