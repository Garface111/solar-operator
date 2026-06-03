import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Public URL is solaroperator.org/accounts (Netlify 200-proxies it to the
// FastAPI mount at /app/* on Railway). Assets + router must be prefixed with
// /accounts so they resolve through the proxy. Dev server runs at
// http://localhost:5174/accounts/.
export default defineConfig({
  base: "/accounts/",
  plugins: [react()],
  server: {
    port: 5174,
  },
});
