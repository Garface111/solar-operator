import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import DummyReport from "./screens/DummyReport";
import GetStarted from "./screens/GetStarted";
import Welcome from "./screens/Welcome";
import Info from "./screens/Info";
import ClientSetup from "./screens/ClientSetup";
import Connect from "./screens/Connect";
import CloudConnect from "./screens/CloudConnect";
import Extension from "./screens/Extension";
import Clients from "./screens/Clients";
import Done from "./screens/Done";

// basename matches Vite's `base` and the FastAPI StaticFiles mount at /onboarding/
// (catch-all for deep links is wired in Task 10).
export default function App() {
  return (
    <BrowserRouter basename="/onboarding">
      <Routes>
        {/* Landing = the intro animation (3 fade-up panels — what operators
            gain from the software). Followed by a "See the sample report →"
            CTA that goes to /sample (the NEPOOL workbook mock). The wizard
            entry point ("Start my free setup →") is the primary action.
            Ford Jun 6: intro animation comes before the sample spreadsheet. */}
        <Route path="/" element={<GetStarted />} />
        <Route path="/sample" element={<DummyReport />} />
        {/* Legacy /demo + /intro point at the new homes so any old links
            (extension popup, marketing, bookmarks) still land somewhere sane. */}
        <Route path="/demo" element={<Navigate to="/sample" replace />} />
        <Route path="/intro" element={<Navigate to="/" replace />} />
        {/* Wizard: Welcome → Info → ClientSetup (with checkout handoff) → Extension → Clients → Done */}
        <Route path="/welcome" element={<Welcome />} />
        <Route path="/info" element={<Info />} />
        <Route path="/client-setup" element={<ClientSetup />} />
        {/* Legacy /plan URL — redirect to /client-setup which now owns checkout */}
        <Route path="/plan" element={<Navigate to="/client-setup" replace />} />
        {/* Cloud-Capture fork (dark-shipped behind so:flag:cloud-capture-ui):
            ClientSetup → /connect → either /cloud (store-with-us) or /extension. */}
        <Route path="/connect" element={<Connect />} />
        <Route path="/cloud" element={<CloudConnect />} />
        <Route path="/extension" element={<Extension />} />
        <Route path="/clients" element={<Clients />} />
        <Route path="/done" element={<Done />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
