import type { ReactNode } from "react";
import { Stepper } from "./Stepper";

export const STEPS = ["Welcome", "Your info", "Install", "Clients", "Done"];

interface ScreenLayoutProps {
  /** Zero-based index of the active wizard step — drives the Stepper. */
  current: number;
  children: ReactNode;
}

/** Page chrome shared by every wizard screen: centered column + Stepper on top. */
export function ScreenLayout({ current, children }: ScreenLayoutProps) {
  return (
    <div className="mx-auto flex min-h-full max-w-2xl flex-col gap-8 px-4 py-12">
      <Stepper steps={STEPS} current={current} />
      {children}
    </div>
  );
}
