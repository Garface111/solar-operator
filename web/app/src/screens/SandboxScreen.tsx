import { ReactFlowProvider } from '@xyflow/react';
import SandboxCanvas from '../components/sandbox/SandboxCanvas';

// Full-bleed canvas: fixed below the topnav (57px) + tabbar (~44px) = 101px.
// The outer div escapes DashboardLayout's max-width constraint entirely.
export default function SandboxScreen() {
  return (
    <div
      style={{ position: 'fixed', inset: 0, top: 101, zIndex: 5 }}
      className="bg-zinc-50"
    >
      <ReactFlowProvider>
        <SandboxCanvas />
      </ReactFlowProvider>
    </div>
  );
}
