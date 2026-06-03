import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by FastAPI StaticFiles at /onboarding/* in production, so asset URLs
// must be prefixed accordingly. Dev server still works at http://localhost:5173/onboarding/.
export default defineConfig({
  base: "/onboarding/",
  plugins: [react()],
  server: {
    port: 5173,
  },
});
