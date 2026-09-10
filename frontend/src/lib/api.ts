import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";

// Relative by default: in production the SPA and the API share one origin behind
// Caddy, so no host is ever baked into the bundle. Vite proxies it in dev.
export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? "/api/v1",
  withCredentials: true,
});

// The access token lives in memory only. The refresh token is an HttpOnly cookie
// the browser sends on its own, so nothing long-lived is reachable from JS.
let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

api.interceptors.request.use((config) => {
  if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`;
  return config;
});

type Retryable = InternalAxiosRequestConfig & { _retried?: boolean };

let refreshing: Promise<string | null> | null = null;

async function refreshAccessToken(): Promise<string | null> {
  // One in-flight refresh shared by every caller: three parallel 401s must not
  // fire three refreshes and race each other's tokens.
  refreshing ??= api
    .post<{ access_token: string }>("/auth/refresh", null, { headers: { Authorization: "" } })
    .then((response) => {
      setAccessToken(response.data.access_token);
      return response.data.access_token;
    })
    .catch(() => {
      setAccessToken(null);
      return null;
    })
    .finally(() => {
      refreshing = null;
    });
  return refreshing;
}

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const request = error.config as Retryable | undefined;
    const isAuthCall = request?.url?.startsWith("/auth/");

    // 403 is "approved yet?", not "logged in?" — refreshing would not help and
    // would bounce the user back to a login button that does nothing.
    if (error.response?.status !== 401 || !request || request._retried || isAuthCall) {
      throw error;
    }

    request._retried = true;
    const token = await refreshAccessToken();
    if (!token) throw error;
    request.headers.Authorization = `Bearer ${token}`;
    return api.request(request);
  },
);

export type ReadyResponse = {
  status: "ok" | "degraded";
  checks: Record<string, string>;
};

export async function fetchReady(): Promise<ReadyResponse> {
  // /ready sits outside /api/v1 — it is infrastructure, not part of the API surface.
  const response = await axios.get<ReadyResponse>("/ready", {
    // A degraded backend answers 503 with a useful body; don't throw it away.
    validateStatus: (status) => status === 200 || status === 503,
  });
  return response.data;
}
