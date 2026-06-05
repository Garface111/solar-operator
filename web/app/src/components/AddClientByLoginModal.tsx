import { useEffect, useRef, useState } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { openPortalTab } from "../lib/openPortalTab";

type Provider = "gmp" | "vec";
type Phase = "choose" | "waiting" | "captured";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Fired when at least one new capture has been registered while this
   *  modal was open — used by the parent to reload the clients list. */
  onCaptured: () => void;
  /** Switch into the legacy form (manual name + login) for the rare case
   *  someone wants to add a placeholder upfront. */
  onSwitchToManual: () => void;
}

const PORTAL_URLS: Record<Provider, string> = {
  gmp: "https://greenmountainpower.com/account/login/",
  vec: "https://vermontelectric.smarthub.coop/Login.html",
};

const FRIENDLY: Record<Provider, string> = {
  gmp: "Green Mountain Power",
  vec: "Vermont Electric Cooperative",
};

/**
 * AddClientByLoginModal — the high-agency "just log in" flow.
 *
 * 1. Operator picks a portal.
 * 2. Extension opens the portal in a background tab.
 * 3. Operator signs in there.
 * 4. The extension captures and POSTs /v1/sync.
 * 5. Backend auto-creates a Client for the captured login.
 * 6. We get SO_CAPTURE_LANDED → flip to "captured" state with a
 *    "log into another" CTA so they can chain 50 logins in one sitting.
 */
export function AddClientByLoginModal({
  open,
  onClose,
  onCaptured,
  onSwitchToManual,
}: Props) {
  const toast = useToast();
  const [phase, setPhase] = useState<Phase>("choose");
  const [lastProvider, setLastProvider] = useState<Provider | null>(null);
  const [capturedClient, setCapturedClient] = useState<string | null>(null);
  const [openingTab, setOpeningTab] = useState(false);
  const capturesThisSession = useRef(0);

  // Reset whenever the modal opens.
  useEffect(() => {
    if (open) {
      setPhase("choose");
      setLastProvider(null);
      setCapturedClient(null);
      capturesThisSession.current = 0;
    }
  }, [open]);

  // Listen for SO_CAPTURE_LANDED while the modal is open.
  useEffect(() => {
    if (!open) return;
    function handler(e: MessageEvent) {
      if (e.source !== window) return;
      const data = e.data;
      if (!data || data.type !== "SO_CAPTURE_LANDED") return;
      capturesThisSession.current += 1;
      setCapturedClient(data.clientName ?? data.provider ?? "your account");
      setPhase("captured");
      onCaptured();
    }
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [open, onCaptured]);

  async function pick(provider: Provider) {
    setLastProvider(provider);
    setOpeningTab(true);
    const result = await openPortalTab(PORTAL_URLS[provider]);
    setOpeningTab(false);
    if (result === "extension") {
      setPhase("waiting");
    } else if (result === "blocked") {
      toast.error(
        "Pop-up was blocked. Click the link below to open the portal in a new tab.",
      );
      window.open(PORTAL_URLS[provider], "_blank");
      setPhase("waiting");
    } else {
      // No extension — open in a new tab and ask them to come back
      window.open(PORTAL_URLS[provider], "_blank");
      setPhase("waiting");
    }
  }

  function closeAndReset() {
    setPhase("choose");
    setLastProvider(null);
    setCapturedClient(null);
    onClose();
  }

  return (
    <Modal
      open={open}
      onClose={closeAndReset}
      title={
        phase === "captured"
          ? `Got it — ${capturedClient}`
          : phase === "waiting"
            ? `Sign in at ${lastProvider ? FRIENDLY[lastProvider] : "the portal"}`
            : "Add a client"
      }
      footer={
        phase === "captured" ? (
          <>
            <Button variant="ghost" onClick={closeAndReset}>
              Done for now
            </Button>
            <Button onClick={() => setPhase("choose")}>
              Add another
            </Button>
          </>
        ) : phase === "waiting" ? (
          <>
            <Button variant="ghost" onClick={() => setPhase("choose")}>
              Pick a different portal
            </Button>
            <Button variant="ghost" onClick={closeAndReset}>
              Close
            </Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={onSwitchToManual}>
              Add manually instead
            </Button>
            <Button variant="ghost" onClick={closeAndReset}>
              Cancel
            </Button>
          </>
        )
      }
    >
      {phase === "choose" && (
        <div className="space-y-4">
          <p className="text-sm text-zinc-600">
            Pick the portal your client signs into. We&apos;ll open it in a new
            tab — just sign in there as them, and their arrays appear here
            automatically.
          </p>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <PortalCard
              provider="gmp"
              label="Green Mountain Power"
              hint="Most VT solar clients"
              onClick={() => pick("gmp")}
              disabled={openingTab}
            />
            <PortalCard
              provider="vec"
              label="Vermont Electric Co-op"
              hint="Northeast Kingdom area"
              onClick={() => pick("vec")}
              disabled={openingTab}
            />
          </div>

          <p className="text-xs text-zinc-400">
            You can sign into as many clients as you want in one sitting —
            each capture creates its own client here.
          </p>
        </div>
      )}

      {phase === "waiting" && (
        <div className="space-y-4 py-2">
          <div className="flex items-center justify-center gap-3 text-emerald-600">
            <Spinner className="h-5 w-5" />
            <span className="text-sm font-medium">
              Waiting for sign-in&hellip;
            </span>
          </div>
          <div className="rounded-xl bg-zinc-50 px-4 py-3 text-sm text-zinc-600">
            <p className="font-medium text-zinc-800">
              In the tab that just opened:
            </p>
            <ol className="mt-2 ml-5 list-decimal space-y-1">
              <li>Sign in as your client.</li>
              <li>That&apos;s it — come back here.</li>
            </ol>
          </div>
          <p className="text-xs text-zinc-400">
            If you signed in but nothing happened, the extension may not be
            paired. Visit{" "}
            <button
              onClick={onSwitchToManual}
              className="text-primary-600 underline hover:text-primary-700"
            >
              add manually
            </button>
            .
          </p>
        </div>
      )}

      {phase === "captured" && (
        <div className="space-y-4 py-2">
          <div className="flex items-center justify-center gap-2 text-emerald-600">
            <svg
              className="h-6 w-6"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2.5}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
            </svg>
            <span className="text-base font-medium">
              {capturedClient} added
            </span>
          </div>
          <p className="text-center text-sm text-zinc-600">
            {capturesThisSession.current === 1
              ? "Their arrays are now on your dashboard."
              : `${capturesThisSession.current} clients captured this session.`}
          </p>
          <p className="text-center text-xs text-zinc-400">
            Have more to add? Pick another portal — we&apos;ll keep going.
          </p>
        </div>
      )}
    </Modal>
  );
}

function PortalCard({
  provider,
  label,
  hint,
  onClick,
  disabled,
}: {
  provider: Provider;
  label: string;
  hint: string;
  onClick: () => void;
  disabled: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="group flex flex-col items-start gap-1 rounded-xl border-2 border-zinc-200 bg-white p-4 text-left transition-all hover:border-emerald-400 hover:bg-emerald-50/50 disabled:opacity-50"
    >
      <span className="text-base font-semibold text-zinc-900 group-hover:text-emerald-800">
        {label}
      </span>
      <span className="text-xs text-zinc-500">{hint}</span>
      <span className="mt-2 text-xs font-medium text-emerald-600 group-hover:text-emerald-700">
        Open {provider.toUpperCase()} portal →
      </span>
    </button>
  );
}
