import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Public URL is nepooloperator.com/accounts (Netlify 200-proxies it to the
// FastAPI mount at /app/* on Railway). Assets + router must be prefixed with
// /accounts so they resolve through the proxy. Dev server runs at
// http://localhost:5174/accounts/.
//
// In dev we proxy /v1/* and /app/* straight to prod Railway so you can use
// your real session + real data while iterating against HMR in <100ms.
// Override the upstream with VITE_API_PROXY env var on the npm run dev call.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const API_PROXY = (globalThis as any)?.process?.env?.VITE_API_PROXY ?? "https://nepooloperator.com";

export default defineConfig({
  base: "/accounts/",
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom", "react-router-dom"],
          reactflow: ["@xyflow/react"],
        },
      },
    },
  },
  server: {
    port: 5174,
    proxy: API_PROXY
      ? {
          "/v1": { target: API_PROXY, changeOrigin: true, secure: true },
          "/app": { target: API_PROXY, changeOrigin: true, secure: true },
        }
      : undefined,
  },
});
