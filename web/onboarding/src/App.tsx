import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import DummyReport from "./screens/DummyReport";
import Welcome from "./screens/Welcome";
import Info from "./screens/Info";
import ClientSetup from "./screens/ClientSetup";
import Extension from "./screens/Extension";
import Clients from "./screens/Clients";
import Done from "./screens/Done";

// basename matches Vite's `base` and the FastAPI StaticFiles mount at /onboarding/
// (catch-all for deep links is wired in Task 10).
export default function App() {
  return (
    <BrowserRouter basename="/onboarding">
      <Routes>
        {/* Landing = the sample report. The legacy auto-cycling 3-panel
            "GetStarted" intro was deleted — it shoved 12s of why-we-built-this
            in front of "what am I buying". Operators see the actual NEPOOL
            workbook layout first; the wizard takes over from there. */}
        <Route path="/" element={<DummyReport />} />
        <Route path="/demo" element={<Navigate to="/" replace />} />
        <Route path="/intro" element={<Navigate to="/" replace />} />
        {/* Wizard: Welcome → Info → ClientSetup (with checkout handoff) → Extension → Clients → Done */}
        <Route path="/welcome" element={<Welcome />} />
        <Route path="/info" element={<Info />} />
        <Route path="/client-setup" element={<ClientSetup />} />
        {/* Legacy /plan URL — redirect to /client-setup which now owns checkout */}
        <Route path="/plan" element={<Navigate to="/client-setup" replace />} />
        <Route path="/extension" element={<Extension />} />
        <Route path="/clients" element={<Clients />} />
        <Route path="/done" element={<Done />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
