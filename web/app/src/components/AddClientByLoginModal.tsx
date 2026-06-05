import { useEffect } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { useToast } from "../ui/Toast";
import {
  useExtensionStatus,
  type ExtensionStatus,
} from "../lib/useExtensionStatus";

type Provider = "gmp" | "vec";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Reload the clients list from the server. Called as a fire-and-forget
   *  hook so the parent can refresh — the modal itself no longer cares
   *  about confirmation (captures land ambiently via a global listener
   *  mounted in ClientsSection). */
  onCaptured: () => Promise<{ id: number; name: string }[]>;
  /** Switch into the legacy manual form for the rare case someone wants
   *  to add a placeholder without ever touching a portal. */
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
 * AddClientByLoginModal — the "click a portal, sign in, done" flow.
 *
 * Earlier versions of this modal sat operators on a "Watching for sign-in…"
 * babysitting page after picking a portal. That was its own friction
 * surface — Ford called it "the live capture page" — and it added a
 * cognitive layer the operator never needed.
 *
 * New flow:
 *   1. Operator picks GMP or VEC.
 *   2. We close the modal immediately and open the portal in a fresh
 *      foreground tab (extension wipes session cookies first).
 *   3. Operator signs in. The extension POSTs /v1/sync. Backend creates
 *      a Client. SO_CAPTURE_LANDED fires globally.
 *   4. A separate <CaptureListener> mounted in ClientsSection toasts
 *      "<Client name> added" and refreshes the list. The operator sees
 *      that toast from the dashboard tab whenever they come back.
 *   5. To add another, the operator clicks "+ Add client" again. Each
 *      click is a discrete decision; no chained-countdown decision tree.
 */
export function AddClientByLoginModal({
  open,
  onClose,
  onCaptured,
  onSwitchToManual,
}: Props) {
  const toast = useToast();
  const ext = useExtensionStatus(open);

  // Re-probe whenever the modal opens so a freshly-installed extension
  // is detected without a page reload.
  useEffect(() => {
    if (open) void ext.probe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Fire-and-forget cookie wipe via the extension. We don't await it —
  // window.open MUST be called synchronously inside the click handler
  // or Chrome's popup blocker treats it as programmatic and refuses
  // to open a foregrounded tab. Cookies usually finish wiping in <50ms,
  // which beats the portal page's auth-check round-trip on the new tab.
  function wipeCookiesAsync(domain: string) {
    try {
      window.postMessage(
        { type: "SO_WIPE_COOKIES", domain, reqId: `w-${Date.now()}` },
        "*",
      );
    } catch { /* ignore */ }
  }

  function pick(provider: Provider) {
    // Open the portal IMMEDIATELY — synchronous, user-initiated click,
    // foreground tab guaranteed. This is the entire user-facing job;
    // everything else is bookkeeping.
    const newTab = window.open(PORTAL_URLS[provider], "_blank", "noopener,noreferrer");
    if (!newTab) {
      toast.error(
        "Your browser blocked the new tab. Allow pop-ups for this site and try again.",
      );
      return;
    }

    // Cookie wipe runs in parallel — best-effort. If the extension is
    // installed it'll happen before the new tab finishes its first
    // network call; if not, the operator just lands wherever they were
    // last logged in (still functional, just less clean).
    const host =
      provider === "gmp" ? "greenmountainpower.com" : "smarthub.coop";
    wipeCookiesAsync(host);

    // Snapshot known clients + close modal. CaptureListener handles
    // success notification when the extension POSTs /v1/sync.
    (async () => {
      try {
        const before = await onCaptured();
        try {
          sessionStorage.setItem(
            "so_capture_pending",
            JSON.stringify({
              provider,
              startedAt: Date.now(),
              knownIds: before.map((c) => c.id),
            }),
          );
        } catch { /* non-fatal */ }
      } catch { /* parent surfaces its own errors */ }
    })();

    onClose();

    if (!extensionUsable) {
      toast.show(
        "Sign in at the portal, then add the client manually from the dashboard.",
        "info",
      );
    } else {
      toast.success(
        `Opened ${FRIENDLY[provider]}. Sign in as the new client — their arrays show up here automatically.`,
      );
    }
  }

  const extensionUsable = ext.status === "present-paired";
  const extensionUnpaired = ext.status === "present-unpaired";
  const extensionAbsent = ext.status === "absent";
  const extensionUnknown = ext.status === "unknown";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Add a client"
      footer={
        <>
          <Button
            variant={extensionUsable ? "ghost" : "primary"}
            onClick={onSwitchToManual}
          >
            {extensionUsable ? "Add manually instead" : "Add manually →"}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <ExtensionStatusBanner status={ext.status} version={ext.version} />

        {extensionUsable && (
          <p className="text-sm text-zinc-600">
            Pick the portal your client signs into. We&apos;ll open it in
            a fresh tab — sign in there as the new client, and their
            arrays appear in your dashboard automatically.
          </p>
        )}
        {extensionUnpaired && (
          <p className="text-sm text-zinc-600">
            Your extension is installed but not paired to this account yet.
            You can still pick a portal — after sign-in we&apos;ll fall back
            to a manual refresh.
          </p>
        )}
        {extensionAbsent && (
          <p className="text-sm text-zinc-600">
            Without the extension, auto-capture can&apos;t finish.{" "}
            <b className="text-zinc-900">Add manually instead</b> — it&apos;s
            quick, and you can re-enable auto-populate after installing the
            extension.
          </p>
        )}
        {extensionUnknown && (
          <p className="text-sm text-zinc-500">
            Checking your extension&hellip;
          </p>
        )}

        <div
          className={[
            "grid grid-cols-1 gap-3 sm:grid-cols-2",
            extensionAbsent ? "opacity-50" : "",
          ].join(" ")}
        >
          <PortalCard
            provider="gmp"
            label="Green Mountain Power"
            hint="Most VT solar clients"
            onClick={() => pick("gmp")}
            disabled={extensionUnknown}
          />
          <PortalCard
            provider="vec"
            label="Vermont Electric Co-op"
            hint="Northeast Kingdom area"
            onClick={() => pick("vec")}
            disabled={extensionUnknown}
          />
        </div>

        <p className="text-xs text-zinc-400">
          Tip: to add another client, just click <b>+ Add client</b> again
          after the first one lands.
        </p>
      </div>
    </Modal>
  );
}

function ExtensionStatusBanner({
  status,
  version,
}: {
  status: ExtensionStatus;
  version: string | null;
}) {
  if (status === "unknown") return null;
  if (status === "present-paired") {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
        <span aria-hidden>✓</span>
        <span>
          Extension paired{version ? ` (v${version})` : ""} — auto-capture is on.
        </span>
      </div>
    );
  }
  if (status === "present-unpaired") {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
        <span aria-hidden>⚠</span>
        <span>
          Extension installed{version ? ` (v${version})` : ""} but not paired
          yet. Auto-capture may not fire.
        </span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-900">
      <span aria-hidden>✗</span>
      <span>
        Extension not detected. Install it from{" "}
        <a
          href="/onboarding/#/extension"
          target="_blank"
          rel="noopener noreferrer"
          className="font-semibold underline hover:text-red-700"
        >
          Setup
        </a>{" "}
        to enable auto-capture, or add this client manually below.
      </span>
    </div>
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
      className="group flex flex-col items-start gap-1 rounded-xl border-2 border-zinc-200 bg-white p-4 text-left transition-all hover:border-emerald-400 hover:bg-emerald-50/50 disabled:cursor-not-allowed disabled:opacity-50"
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
