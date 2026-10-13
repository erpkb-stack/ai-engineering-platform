/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";


// The browser talks to ONE origin (localhost:5173): /api is proxied to the api (:8000), so the
// api needs no CORS. Keycloak (:8090) allows this origin itself (realm webOrigins, ADR-027).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true, // the realm's redirect URI is exactly this origin
    proxy: { "/api": { target: process.env.AEOI_API_URL ?? "http://localhost:8000", changeOrigin: false } },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
