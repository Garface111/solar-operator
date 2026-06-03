import { Button } from "../ui/Button";

interface TopNavProps {
  email: string | null;
  onSignOut: () => void;
}

export function TopNav({ email, onSignOut }: TopNavProps) {
  return (
    <header className="sticky top-0 z-30 border-b border-zinc-200 bg-white/80 backdrop-blur">
      <div className="mx-auto flex max-w-4xl items-center justify-between px-4 py-3.5">
        <div className="text-base font-semibold tracking-tight text-zinc-900">
          <span className="text-primary-600">Solar</span> Operator
        </div>
        <div className="flex items-center gap-3">
          {email && (
            <span className="hidden text-sm text-zinc-500 sm:inline">
              {email}
            </span>
          )}
          <Button variant="secondary" onClick={onSignOut} className="px-3 py-1.5">
            Sign out
          </Button>
        </div>
      </div>
    </header>
  );
}
