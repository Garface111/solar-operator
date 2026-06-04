import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Welcome from "./screens/Welcome";
import Info from "./screens/Info";
import Plan from "./screens/Plan";
import Extension from "./screens/Extension";
import Clients from "./screens/Clients";
import Done from "./screens/Done";

// basename matches Vite's `base` and the FastAPI StaticFiles mount at /onboarding/
// (catch-all for deep links is wired in Task 10).
export default function App() {
  return (
    <BrowserRouter basename="/onboarding">
      <Routes>
        <Route path="/" element={<Welcome />} />
        <Route path="/info" element={<Info />} />
        <Route path="/plan" element={<Plan />} />
        <Route path="/extension" element={<Extension />} />
        <Route path="/clients" element={<Clients />} />
        <Route path="/done" element={<Done />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
