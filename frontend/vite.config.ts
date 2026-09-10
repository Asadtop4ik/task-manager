import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    host: true,
    port: 5173,
    // Dev only. In production Caddy owns this split, so the app never learns the
    // API's host and the same build works on any domain.
    proxy: {
      "/api": "http://localhost:8000",
      "/ready": "http://localhost:8000",
    },
  },
});
