import axios from "axios";

// Relative by default: in production the SPA and the API share one origin behind
// Caddy, so no host is ever baked into the bundle. Vite proxies it in dev.
export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? "/api/v1",
  withCredentials: true,
});

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
