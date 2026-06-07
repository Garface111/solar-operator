import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ToastProvider } from "./ui/Toast";
import "./index.css";
import { initPreviewSync } from "./lib/previewSync";

// v0.6.7 — MC Lens-Picker live iframe sync. No-ops in normal traffic;
// activates only when ?mc_preview_id= query param is present.
initPreviewSync();

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
