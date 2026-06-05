import { Button } from "../ui/Button";

interface TopNavProps {
  email: string | null;
  onSignOut: () => void;
  onShowWalkthrough?: () => void;
}

export function TopNav({ email, onSignOut, onShowWalkthrough }: TopNavProps) {
  return (
    <header className="sticky top-0 z-30 border-b border-cream-border bg-cream/90 backdrop-blur">
      <div className="mx-auto flex max-w-4xl items-center justify-between px-4 py-3.5">
        <div
          className="text-base font-semibold tracking-tight text-zinc-900"
          style={{ fontFamily: "'Georgia', ui-serif, serif" }}
        >
          <span className="text-primary-600">Solar</span> Operator
        </div>
        <div className="flex items-center gap-3">
          {email && (
            <span className="hidden text-sm text-zinc-500 sm:inline">
              {email}
            </span>
          )}
          {onShowWalkthrough && (
            <button
              type="button"
              onClick={onShowWalkthrough}
              className="hidden text-xs text-zinc-400 transition-colors hover:text-zinc-600 sm:inline"
            >
              Show walkthrough
            </button>
          )}
          <Button variant="secondary" onClick={onSignOut} className="px-3 py-1.5">
            Sign out
          </Button>
        </div>
      </div>
    </header>
  );
}
