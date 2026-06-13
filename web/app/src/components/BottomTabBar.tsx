import { NavLink } from "react-router-dom";

function ClientsIcon() {
  return (
    <svg
      width="22"
      height="22"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}

function ReportsIcon() {
  return (
    <svg
      width="22"
      height="22"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
    </svg>
  );
}

function AccountIcon() {
  return (
    <svg
      width="22"
      height="22"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  );
}

interface BottomTabDef {
  label: string;
  to: string;
  Icon: React.ComponentType;
}

const BOTTOM_TABS: BottomTabDef[] = [
  { label: "Clients", to: "/clients", Icon: ClientsIcon },
  { label: "Reports", to: "/reports", Icon: ReportsIcon },
  { label: "Account", to: "/account", Icon: AccountIcon },
];

/**
 * Mobile-only sticky bottom navigation bar — replaces the top TabBar tabs
 * on viewports narrower than the sm breakpoint (640px).
 *
 * 56px tall (h-14), cream/translucent bg, gold hairline top border.
 * Active tab: primary-600 emerald. Inactive: zinc-500.
 * Safe-area-inset-bottom respected for notch/home-indicator devices.
 */
export function BottomTabBar() {
  return (
    <nav
      aria-label="Main navigation"
      data-testid="bottom-tab-bar"
      className="sm:hidden fixed inset-x-0 bottom-0 z-30 border-t border-[#C8A24A]/25 bg-cream/95 backdrop-blur-sm"
      style={{ paddingBottom: "env(safe-area-inset-bottom, 0px)" }}
    >
      <div className="flex h-14">
        {BOTTOM_TABS.map(({ label, to, Icon }) => (
          <NavLink
            key={to}
            to={to}
            aria-label={label}
            data-testid={`bottom-tab-${label.toLowerCase()}`}
            className={({ isActive }) =>
              [
                "flex flex-1 flex-col items-center justify-center gap-0.5 transition-colors duration-150",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary-500/40",
                isActive
                  ? "text-primary-600"
                  : "text-zinc-500 hover:text-zinc-700",
              ].join(" ")
            }
          >
            <Icon />
            <span className="text-[11px] font-medium leading-none">{label}</span>
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
