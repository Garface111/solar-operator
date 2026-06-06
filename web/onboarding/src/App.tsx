import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import GetStarted from "./screens/GetStarted";
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
        {/* Pre-wizard explainer screens (no stepper).
            Landing drops straight onto the sample report — the auto-cycling
            3-panel intro felt like "watch this autoscroll then click Next"
            instead of "see what you're buying". /intro keeps it reachable. */}
        <Route path="/" element={<Navigate to="/demo" replace />} />
        <Route path="/intro" element={<GetStarted />} />
        <Route path="/demo" element={<DummyReport />} />
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
