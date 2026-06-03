import { NavLink } from "react-router-dom";

export interface Tab {
  label: string;
  /** Router path (relative to the app basename), e.g. "/account". */
  to: string;
}

interface TabBarProps {
  tabs: Tab[];
}

/**
 * Underline-style tab bar that sits below the top nav. Each tab is a real
 * route (deep-linkable). Active tab gets a 2px emerald (primary-500) underline
 * with zinc-900 / weight-600 text; inactive is zinc-500, hover zinc-700.
 *
 * On narrow screens the row scrolls horizontally rather than wrapping — the
 * scrollbar itself is hidden for a cleaner look.
 */
export function TabBar({ tabs }: TabBarProps) {
  return (
    <nav className="sticky top-[57px] z-20 border-b border-zinc-200 bg-white/80 backdrop-blur">
      <div className="mx-auto max-w-4xl px-4">
        <div className="flex gap-6 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {tabs.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={({ isActive }) =>
                [
                  "relative -mb-px whitespace-nowrap border-b-2 py-3 text-sm",
                  "transition-colors duration-150 ease-in-out",
                  isActive
                    ? "border-primary-500 font-semibold text-zinc-900"
                    : "border-transparent font-medium text-zinc-500 hover:text-zinc-700",
                ].join(" ")
              }
            >
              {tab.label}
            </NavLink>
          ))}
        </div>
      </div>
    </nav>
  );
}
