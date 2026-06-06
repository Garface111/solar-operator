import { useEffect } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { useToast } from "../ui/Toast";
import {
  useExtensionStatus,
  type ExtensionStatus,
} from "../lib/useExtensionStatus";
import { wipeCookiesAndWait, gmpPortalUrl } from "../lib/openPortalTab";

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
  gmp: "https://greenmountainpower.com/account/",
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

  function pick(provider: Provider) {
    const url = provider === "gmp" ? gmpPortalUrl(ext.version) : PORTAL_URLS[provider];
    const host =
      provider === "gmp" ? "greenmountainpower.com" : "smarthub.coop";

    // Pattern A: open about:blank SYNCHRONOUSLY (foreground tab guaranteed —
    // no await between click and window.open). We omit noopener so the
    // returned reference is non-null and we can set location.href after the
    // wipe. about:blank has no meaningful cross-origin security concern.
    const t0 = Date.now();
    console.log(`[SO ${t0}] add-login: open about:blank for ${provider}`);
    const newTab = window.open("about:blank", "_blank");
    if (!newTab) {
      // Popup blocker — user-initiated click should never hit this in practice.
      return;
    }

    // Await the cookie wipe ack, THEN navigate. This prevents the race where
    // the portal loads with stale session cookies and lands on the previous
    // customer's dashboard.
    (async () => {
      console.log(`[SO ${Date.now()}] add-login: wipe-start for ${host}`);
      await wipeCookiesAndWait(host);
      console.log(`[SO ${Date.now()}] add-login: wipe-done (+${Date.now() - t0}ms), navigating tab`);
      try {
        newTab.location.href = url;
        console.log(`[SO ${Date.now()}] add-login: tab.location.href set → ${url}`);
      } catch (e) {
        // Should not happen while tab is on about:blank but log if it does.
        console.error("[SO] could not navigate new tab:", e);
      }
    })();

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
          try {
            window.dispatchEvent(
              new CustomEvent("so:capture-pending", { detail: { provider } }),
            );
          } catch { /* ignore */ }
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
            I'm done
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

        <p className="text-xs text-zinc-500">
          <b className="text-zinc-700">Adding multiple clients?</b> Stay in the
          portal tab and sign out, then sign into the next client's account.
          Each sign-in captures another client automatically — you don't need
          to come back here between them.
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
