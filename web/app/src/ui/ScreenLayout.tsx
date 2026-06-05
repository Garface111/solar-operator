import type { ReactNode } from "react";

interface ScreenLayoutProps {
  children: ReactNode;
  className?: string;
}

/** Consistent vertical rhythm wrapper for each dashboard tab.
 *  DashboardLayout owns the outer shell (nav, tabs, max-w-4xl padding);
 *  this just normalises spacing between the cards inside each tab. */
export function ScreenLayout({ children, className = "" }: ScreenLayoutProps) {
  return (
    <div className={["space-y-6", className].filter(Boolean).join(" ")}>
      {children}
    </div>
  );
}
