import { Card } from "./ui/Card";
import { Stepper } from "./ui/Stepper";

const STEPS = ["Welcome", "Your info", "Install", "Clients", "Done"];

export default function App() {
  return (
    <div className="mx-auto flex min-h-full max-w-2xl flex-col gap-8 px-4 py-12">
      <Stepper steps={STEPS} current={0} />
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Quarterly solar reports, on autopilot.
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          Onboarding scaffold is up. Screens land in subsequent tasks.
        </p>
      </Card>
    </div>
  );
}
