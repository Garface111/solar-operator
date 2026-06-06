import { NavLink } from "react-router-dom";

export interface Tab {
  label: string;
  /** Router path (relative to the app basename), e.g. "/account". */
  to: string;
}

interface TabBarProps {
  tabs: Tab[];
  /** Tab paths that the user hasn't visited yet — render a small green dot
   *  next to each so first-time operators know they should look at each one. */
  unvisited?: Set<string>;
}

/**
 * Underline-style tab bar that sits below the top nav. Each tab is a real
 * route (deep-linkable). Active tab gets a 2px emerald (primary-500) underline
 * with zinc-900 / weight-600 text; inactive is zinc-500, hover zinc-700.
 *
 * On narrow screens the row scrolls horizontally rather than wrapping — the
 * scrollbar itself is hidden for a cleaner look.
 */
export function TabBar({ tabs, unvisited }: TabBarProps) {
  return (
    <nav className="sticky top-[57px] z-20 border-b border-cream-border bg-cream/90 backdrop-blur">
      <div className="mx-auto max-w-4xl px-4">
        <div className="flex w-full">
          {tabs.map((tab) => {
            const isUnvisited = unvisited?.has(tab.to) ?? false;
            return (
              <NavLink
                key={tab.to}
                to={tab.to}
                className={({ isActive }) =>
                  [
                    "relative -mb-px inline-flex flex-1 items-center justify-center gap-1.5 whitespace-nowrap border-b-2 py-4 text-base",
                    "transition-colors duration-150 ease-in-out",
                    isActive
                      ? "border-primary-500 font-semibold text-zinc-900"
                      : "border-transparent font-medium text-zinc-500 hover:text-zinc-700",
                  ].join(" ")
                }
              >
                {tab.label}
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
    </nav>
  );
}
