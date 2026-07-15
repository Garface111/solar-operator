import type { ReactNode } from "react";
import { Stepper } from "./Stepper";

// Flow: Welcome(0) → Info(1) → Clients(2) → Connect(3) → Done(4)
// Connect is the dual-path fork (cloud logins or extension install).
export const STEPS = ["Welcome", "Your info", "Clients", "Connect", "Done"];

interface ScreenLayoutProps {
  /** Zero-based index of the active wizard step — drives the Stepper. */
  current: number;
  children: ReactNode;
  /** Optional path-specific labels (e.g. cloud path "Your logins"). */
  stepLabelsOverride?: string[];
}

/** Page chrome shared by every wizard screen: centered column + Stepper on top. */
export function ScreenLayout({ current, children, stepLabelsOverride }: ScreenLayoutProps) {
  const steps = stepLabelsOverride ?? STEPS;
  return (
    <div className="mx-auto flex min-h-full max-w-2xl flex-col gap-8 px-4 py-8 sm:py-12">
      <nav aria-label="Onboarding progress">
        <Stepper steps={steps} current={current} />
      </nav>
      <main>{children}</main>
    </div>
  );
}
