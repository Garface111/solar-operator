import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ToastProvider } from "./ui/Toast";
import "./index.css";
import { initPreviewSync } from "./lib/previewSync";
import { startLiveSync } from "./lib/liveSync";

// v0.6.7 — MC Lens-Picker live iframe sync. No-ops in normal traffic;
// activates only when ?mc_preview_id= query param is present.
initPreviewSync();

// The single freshness plane: one SSE stream per page pushing server-side
// changes onto the DOM bus (lib/fleetEvents) so every surface refreshes
// without waiting for the 15s poll. Idempotent — safe if anything else
// (e.g. HMR) evaluates this module again while the stream is live.
startLiveSync();

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("Root element #root not found");
}

createRoot(rootEl).render(
  <StrictMode>
    <ToastProvider>
      <App />
    </ToastProvider>
  </StrictMode>,
);
