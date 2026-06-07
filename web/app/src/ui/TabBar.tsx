import { NavLink } from "react-router-dom";

export interface Tab {
  label: string;
  /** Shorter label shown on mobile viewports (< 640px) where space is limited. */
  shortLabel?: string;
  /** Router path (relative to the app basename), e.g. "/account". */
  to: string;
}

interface TabBarProps {
  tabs: Tab[];
  /** Tab paths that the user hasn't visited yet — render a small green dot
   *  next to each so first-time operators know they should look at each one. */
  unvisited?: Set<string>;
  /** Wordmark + auth chrome — collapsed into the tab row Jun 6'26 so the
   *  top of the page doesn't waste a whole band on company name + email. */
  email?: string | null;
  onSignOut?: () => void;
}

/**
 * Single sticky bar that consolidates wordmark + tabs + auth chrome:
 *   [ Solar Operator ]   [ Master account | Clients | Automatic reports ]   [ email · Sign out ]
 *
 * Active tab gets a 2px emerald (primary-500) underline with zinc-900 /
 * weight-600 text; inactive is zinc-500, hover zinc-700.
 *
 * Mobile (<640px): wordmark hidden to free up tab space; shortLabel shown
 * instead of label; py reduced; sign-out shrinks to compact form.
 */
export function TabBar({ tabs, unvisited, email, onSignOut }: TabBarProps) {
  return (
    <nav className="sticky top-0 z-30 border-b border-cream-border bg-cream/90 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center gap-2 px-4 sm:gap-4">
        {/* Left: wordmark — hidden on mobile to give tabs more room */}
        <div
          className="hidden shrink-0 text-base font-semibold tracking-tight text-zinc-900 sm:block"
          style={{ fontFamily: "'Georgia', ui-serif, serif" }}
        >
          <span className="text-primary-600">Solar</span> Operator
        </div>

        {/* Center: tabs — flex-1 so the row gets the leftover width and each
            tab is roughly 1/3 of the tabs region. */}
        <div className="flex flex-1 justify-center">
          <div className="flex w-full max-w-xl">
            {tabs.map((tab) => {
              const isUnvisited = unvisited?.has(tab.to) ?? false;
              return (
                <NavLink
                  key={tab.to}
                  to={tab.to}
                  className={({ isActive }) =>
                    [
                      "relative -mb-px inline-flex flex-1 items-center justify-center gap-1 border-b-2 py-3 text-sm sm:gap-1.5 sm:py-4 sm:text-base",
                      "transition-colors duration-150 ease-in-out",
                      isActive
                        ? "border-primary-500 font-semibold text-zinc-900"
                        : "border-transparent font-medium text-zinc-500 hover:text-zinc-700",
                    ].join(" ")
                  }
                >
                  {/* Short label on mobile, full label on sm+ */}
                  <span className="sm:hidden">{tab.shortLabel ?? tab.label}</span>
                  <span className="hidden sm:inline">{tab.label}</span>
                  {isUnvisited && (
                    <span
                      aria-label="Not yet visited"
                      title="You haven't opened this tab yet"
                      className="h-2 w-2 shrink-0 rounded-full bg-primary-500"
                    />
                  )}
                </NavLink>
              );
            })}
          </div>
        </div>

        {/* Right: email + sign out */}
        <div className="flex shrink-0 items-center gap-2 sm:gap-3">
          {email && (
            <span className="hidden text-sm text-zinc-500 sm:inline">{email}</span>
          )}
          {onSignOut && (
            <button
              type="button"
              onClick={onSignOut}
              className="rounded-lg border border-cream-border bg-white px-2.5 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 sm:px-3 sm:text-sm"
            >
              Sign out
            </button>
          )}
        </div>
      </div>
    </nav>
  );
}
