import { NavLink } from "react-router-dom";

function ClientsIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}

function ReportsIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
      <polyline points="10 9 9 9 8 9" />
    </svg>
  );
}

function AccountIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" />
    </svg>
  );
}

interface Props {
  /** Desktop brand labels; defaults are NEPOOL Operator (this SPA). */
  clientsLabel?: string;
  reportsLabel?: string;
  accountLabel?: string;
}

/**
 * Mobile-only sticky bottom navigation.
 * Defaults force NEPOOL vocabulary so offtaker/Billing labels never leak in.
 */
export function BottomTabBar({
  clientsLabel = "Clients",
  reportsLabel = "Automatic Reports",
  accountLabel = "Account",
}: Props) {
  // Short mobile label for the long "Automatic Reports" string.
  const reportsShort =
    reportsLabel === "Automatic Reports" ? "Reports" : reportsLabel;

  const tabs = [
    { label: clientsLabel, short: clientsLabel, to: "/clients", Icon: ClientsIcon, testId: "clients" },
    { label: reportsLabel, short: reportsShort, to: "/reports", Icon: ReportsIcon, testId: "reports" },
    { label: accountLabel, short: accountLabel, to: "/account", Icon: AccountIcon, testId: "account" },
  ];

  return (
    <nav
      aria-label="Main navigation"
      data-testid="bottom-tab-bar"
      className="sm:hidden fixed inset-x-0 bottom-0 z-30 border-t border-[#C8A24A]/25 bg-cream/95 backdrop-blur-sm"
      style={{ paddingBottom: "env(safe-area-inset-bottom, 0px)" }}
    >
      <div className="flex h-14">
        {tabs.map(({ label, short, to, Icon, testId }) => (
          <NavLink
            key={to}
            to={to}
            aria-label={label}
            data-testid={`bottom-tab-${testId}`}
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
            <span className="max-w-[4.5rem] truncate text-[10px] font-medium leading-tight">
              {short}
            </span>
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
