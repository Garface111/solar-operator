import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by FastAPI SPAStaticFiles at /app/* in production, so asset URLs must
// be prefixed accordingly. Dev server runs at http://localhost:5174/app/.
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    port: 5174,
  },
});
