import type { ReactNode } from "react";
import { Stepper } from "./Stepper";

// Flow: Welcome(0) → Info(1) → Clients(2, w/ Stripe handoff) → Install(3) → Done(4)
export const STEPS = ["Welcome", "Your info", "Clients", "Install", "Done"];

interface ScreenLayoutProps {
  /** Zero-based index of the active wizard step — drives the Stepper. */
  current: number;
  children: ReactNode;
}

/** Page chrome shared by every wizard screen: centered column + Stepper on top. */
export function ScreenLayout({ current, children }: ScreenLayoutProps) {
  return (
    <div className="mx-auto flex min-h-full max-w-2xl flex-col gap-8 px-4 py-8 sm:py-12">
      <nav aria-label="Onboarding progress">
        <Stepper steps={STEPS} current={current} />
      </nav>
      <main>{children}</main>
    </div>
  );
}
