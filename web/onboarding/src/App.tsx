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
export default function App() {
  return (
    <BrowserRouter basename="/onboarding">
      <Routes>
        {/* Landing = intro → sample report; wizard starts at /welcome. */}
        <Route path="/" element={<GetStarted />} />
        <Route path="/sample" element={<DummyReport />} />
        <Route path="/demo" element={<Navigate to="/sample" replace />} />
        <Route path="/intro" element={<Navigate to="/" replace />} />
        {/* Wizard: Welcome → Info → ClientSetup → Connect fork →
            cloud (/cloud) or device (/extension) → Done */}
        <Route path="/welcome" element={<Welcome />} />
        <Route path="/info" element={<Info />} />
        <Route path="/client-setup" element={<ClientSetup />} />
        <Route path="/plan" element={<Navigate to="/client-setup" replace />} />
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
