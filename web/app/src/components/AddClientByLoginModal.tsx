import { useEffect, useRef, useState } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { openPortalTab } from "../lib/openPortalTab";
import {
  useExtensionStatus,
  type ExtensionStatus,
} from "../lib/useExtensionStatus";

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
  // Open the login page directly. The extension wipes portal session
  // cookies before opening the tab (background.js OPEN_UTILITY_PORTAL
  // handler), so the operator always lands on a fresh login screen
  // even if they were signed in as a different client a moment ago.
  gmp: "https://greenmountainpower.com/account/login/",
  vec: "https://vermontelectric.smarthub.coop/Login.html",
};

const FRIENDLY: Record<Provider, string> = {
  gmp: "Green Mountain Power",
  vec: "Vermont Electric Cooperative",
};

// After this long with no SO_CAPTURE_LANDED, surface a "still waiting?"
// nudge so a hung modal can't trap the operator.
const STILL_WAITING_MS = 30_000;

/**
 * AddClientByLoginModal — the high-agency "just log in" flow.
 *
 * Intelligence upgrades (June 2026):
 *   - Probes extension status BEFORE the operator picks a portal, so a
 *     flow that can't complete is never offered as the default.
 *   - When the extension is absent or unpaired, prominently steers the
 *     operator to the manual-entry flow instead of letting them fall
 *     into a frozen "waiting for capture" state.
 *   - "I signed in already — check now" escape hatch still appears as a
 *     safety net for slow networks / missed events.
 *   - Title falls back gracefully when SO_CAPTURE_LANDED carries no
 *     clientName/provider (was rendering "Got it — null").
 *   - Probes are cached at module level by useExtensionStatus, so
 *     re-opening the modal is instant.
 *
 * Flow:
 *   1. Operator opens modal → we probe extension status passively + actively.
 *   2. If extension absent/unpaired, top-of-modal banner says so AND the
 *      manual-entry CTA is promoted to primary.
 *   3. If extension is paired, the portal-picker is the primary path.
 *   4. Pick portal → background tab opens → user signs in → extension
 *      POSTs /v1/sync → backend creates Client → SO_CAPTURE_LANDED fires
 *      → modal flips to captured state.
 *   5. Safety nets (30s nudge + manual refresh + extension-absent banner)
 *      cover the "extension didn't fire" failure mode.
 */
export function AddClientByLoginModal({
  open,
  onClose,
  onCaptured,
  onSwitchToManual,
}: Props) {
  const toast = useToast();
  const ext = useExtensionStatus(open);
  const [phase, setPhase] = useState<Phase>("choose");
  const [lastProvider, setLastProvider] = useState<Provider | null>(null);
  const [capturedClient, setCapturedClient] = useState<string | null>(null);
  const [openingTab, setOpeningTab] = useState(false);
  const [stillWaiting, setStillWaiting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  // Countdown to auto-loop back to the portal picker after a successful
  // capture. Chaining 5-50 clients in one sitting becomes muscle memory
  // — operator never has to think about clicking "Add another".
  const [autoLoopSecondsLeft, setAutoLoopSecondsLeft] = useState(0);
  const capturesThisSession = useRef(0);

  // Reset whenever the modal opens; re-probe to get a fresh status.
  useEffect(() => {
    if (open) {
      setPhase("choose");
      setLastProvider(null);
      setCapturedClient(null);
      setStillWaiting(false);
      setAutoLoopSecondsLeft(0);
      capturesThisSession.current = 0;
      void ext.probe();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Listen for SO_CAPTURE_LANDED while the modal is open.
  useEffect(() => {
    if (!open) return;
    function handler(e: MessageEvent) {
      if (e.source !== window) return;
      const data = e.data;
      if (!data || data.type !== "SO_CAPTURE_LANDED") return;

      // Failed captures (network error, 4xx from /v1/sync, etc.) should
      // NOT flip the modal to the success state. Surface the error and
      // keep the user in waiting so they can retry or use manual.
      if (data.ok === false) {
        toast.error(
          typeof data.error === "string"
            ? `Capture failed: ${data.error}`
            : "Capture failed — try again or add this client manually.",
        );
        return;
      }

      capturesThisSession.current += 1;
      const label =
        (typeof data.clientName === "string" && data.clientName.trim()) ||
        (typeof data.provider === "string" && FRIENDLY[data.provider as Provider]) ||
        "your account";
      setCapturedClient(label);
      setPhase("captured");
      // Kick off the auto-loop countdown (3s) — the operator never
      // has to click "Add another"; we just go back to the picker.
      setAutoLoopSecondsLeft(3);
      onCaptured();
    }
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [open, onCaptured, toast]);

  // "Still waiting?" nudge timer in waiting phase.
  useEffect(() => {
    if (phase !== "waiting") {
      setStillWaiting(false);
      return;
    }
    const t = window.setTimeout(() => setStillWaiting(true), STILL_WAITING_MS);
    return () => window.clearTimeout(t);
  }, [phase]);

  // Auto-loop countdown ticker. When a capture lands, autoLoopSecondsLeft
  // is set to 3; we tick down every second and on hit-zero auto-reset
  // to the picker. Operator gets a quiet visual confirmation, then we
  // hand them the next "GMP / VEC" choice without a click.
  useEffect(() => {
    if (autoLoopSecondsLeft <= 0) return;
    if (autoLoopSecondsLeft === 1) {
      // Last tick → reset back to choose state.
      const t = window.setTimeout(() => {
        setAutoLoopSecondsLeft(0);
        setCapturedClient(null);
        setLastProvider(null);
        setPhase("choose");
      }, 1000);
      return () => window.clearTimeout(t);
    }
    const t = window.setTimeout(
      () => setAutoLoopSecondsLeft((n) => n - 1),
      1000,
    );
    return () => window.clearTimeout(t);
  }, [autoLoopSecondsLeft]);

  function cancelAutoLoop() {
    setAutoLoopSecondsLeft(0);
  }

  async function pick(provider: Provider) {
    setLastProvider(provider);
    setOpeningTab(true);
    const result = await openPortalTab(PORTAL_URLS[provider]);
    setOpeningTab(false);
    if (result === "blocked") {
      toast.error(
        "Pop-up was blocked. Click the link in the next panel to open the portal.",
      );
      window.open(PORTAL_URLS[provider], "_blank");
    } else if (result !== "extension") {
      // No extension → open in a new tab; user finishes flow via the
      // manual "I signed in already" refresh button.
      window.open(PORTAL_URLS[provider], "_blank");
    }
    setPhase("waiting");
  }

  function closeAndReset() {
    setPhase("choose");
    setLastProvider(null);
    setCapturedClient(null);
    setStillWaiting(false);
    setAutoLoopSecondsLeft(0);
    onClose();
  }

  async function handleManualRefresh() {
    setRefreshing(true);
    try {
      onCaptured(); // parent reloads /v1/account/clients
      toast.success(
        "Refreshed — if your client showed up, you'll see them below.",
      );
      closeAndReset();
    } finally {
      setRefreshing(false);
    }
  }

  const extensionUsable = ext.status === "present-paired";
  const extensionPresentButUnpaired = ext.status === "present-unpaired";
  const extensionAbsent = ext.status === "absent";
  const extensionUnknown = ext.status === "unknown";

  return (
    <Modal
      open={open}
      onClose={closeAndReset}
      title={
        phase === "captured"
          ? `Got it${capturedClient ? ` — ${capturedClient}` : ""}`
          : phase === "waiting"
            ? `Sign in at ${lastProvider ? FRIENDLY[lastProvider] : "the portal"}`
            : "Add a client"
      }
      footer={
        phase === "captured" ? (
          <>
            <Button variant="ghost" onClick={closeAndReset}>
              Done
            </Button>
            <Button
              onClick={() => {
                cancelAutoLoop();
                setCapturedClient(null);
                setLastProvider(null);
                setPhase("choose");
              }}
            >
              {autoLoopSecondsLeft > 0 ? "Next now →" : "Add another"}
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
            <Button
              variant={extensionUsable ? "ghost" : "primary"}
              onClick={onSwitchToManual}
            >
              {extensionUsable ? "Add manually instead" : "Add manually →"}
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
          <ExtensionStatusBanner status={ext.status} version={ext.version} />

          {extensionUsable && (
            <p className="text-sm text-zinc-600">
              Pick the portal your client signs into. We&apos;ll open it in a
              background tab — sign in there as them, and their arrays appear
              here automatically.
            </p>
          )}
          {extensionPresentButUnpaired && (
            <p className="text-sm text-zinc-600">
              Your extension is installed but not paired to this account
              yet. You can still pick a portal — after sign-in we&apos;ll fall
              back to a manual refresh.
            </p>
          )}
          {extensionAbsent && (
            <p className="text-sm text-zinc-600">
              Without the extension, the auto-capture flow can&apos;t finish.
              <b className="text-zinc-900"> Add manually instead</b> — it&apos;s
              fast, and you can turn on auto-populate after the extension is
              installed.
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
              disabled={openingTab || extensionUnknown}
            />
            <PortalCard
              provider="vec"
              label="Vermont Electric Co-op"
              hint="Northeast Kingdom area"
              onClick={() => pick("vec")}
              disabled={openingTab || extensionUnknown}
            />
          </div>

          {extensionUsable && (
            <p className="text-xs text-zinc-400">
              Tip: sign into as many clients as you want in one sitting — each
              capture creates its own client here.
            </p>
          )}
        </div>
      )}

      {phase === "waiting" && (
        <div className="space-y-4 py-2">
          <div className="flex items-center justify-center gap-3 text-emerald-600">
            <Spinner className="h-5 w-5" />
            <span className="text-sm font-medium">
              {extensionUsable
                ? capturesThisSession.current > 0
                  ? "Watching for your next sign-in…"
                  : "Watching for sign-in…"
                : "Sign in at the portal, then come back"}
            </span>
          </div>
          {/* First-time-only instruction. After the first capture, the
              operator knows the drill — we hide the steps and let the
              spinner do the talking. */}
          {capturesThisSession.current === 0 && (
            <div className="rounded-xl bg-zinc-50 px-4 py-3 text-sm text-zinc-600">
              <p className="font-medium text-zinc-800">
                In the tab that just opened:
              </p>
              <ol className="mt-2 ml-5 list-decimal space-y-1">
                <li>You&apos;ll be signed out of any previous session.</li>
                <li>Sign in as the <b>new</b> client.</li>
                <li>That&apos;s it — come back here.</li>
              </ol>
            </div>
          )}

          {!extensionUsable && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
              <p className="font-medium">
                {extensionAbsent
                  ? "Extension not detected"
                  : "Extension not paired"}
                .
              </p>
              <p className="mt-1 text-xs">
                Your client won&apos;t auto-add. Once you&apos;ve signed in,
                tap{" "}
                <span className="font-semibold">
                  &ldquo;I signed in already&rdquo;
                </span>{" "}
                below to refresh — or close this and use{" "}
                <button
                  type="button"
                  onClick={onSwitchToManual}
                  className="font-semibold underline hover:text-amber-700"
                >
                  add manually
                </button>{" "}
                instead.
              </p>
            </div>
          )}

          {/* Escape hatches — always visible in waiting state. The
              "I signed in already" button forces a clients reload from
              the server in case SO_CAPTURE_LANDED never fires, and the
              quieter underline link drops the user straight into the
              manual form if auto-capture isn't going to happen. */}
          <div className="flex flex-col items-center gap-2">
              <Button
                onClick={handleManualRefresh}
                disabled={refreshing}
                className="w-full sm:w-auto"
              >
                {refreshing ? (
                  <>
                    <Spinner />
                    Refreshing…
                  </>
                ) : (
                  "I signed in already — add now"
                )}
              </Button>
              <button
                type="button"
                onClick={onSwitchToManual}
                className="text-xs font-medium text-zinc-500 underline-offset-2 hover:text-zinc-700 hover:underline focus:outline-none"
              >
                or add this client manually
              </button>
              {stillWaiting && extensionUsable && (
                <p className="mt-1 text-center text-xs text-amber-700">
                  Capture is taking longer than expected — click above to
                  refresh.
                </p>
              )}
            </div>

          <p className="text-xs text-zinc-400">
            If you signed in but nothing happened, the extension may not be
            paired. Try{" "}
            <button
              onClick={onSwitchToManual}
              className="text-primary-600 underline hover:text-primary-700"
            >
              adding manually
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
              {capturedClient || "Client"} added
            </span>
          </div>
          <p className="text-center text-sm text-zinc-600">
            {capturesThisSession.current === 1
              ? "Their arrays are now on your dashboard."
              : `${capturesThisSession.current} clients captured this session.`}
          </p>
          {autoLoopSecondsLeft > 0 ? (
            <div className="flex flex-col items-center gap-2">
              <p className="text-center text-xs text-zinc-500">
                Ready for the next one in {autoLoopSecondsLeft}s…
              </p>
              <div className="h-1 w-32 overflow-hidden rounded-full bg-zinc-200">
                <div
                  className="h-full bg-emerald-500 transition-all duration-1000 ease-linear"
                  style={{ width: `${(autoLoopSecondsLeft / 3) * 100}%` }}
                />
              </div>
            </div>
          ) : (
            <p className="text-center text-xs text-zinc-400">
              Have more to add? Pick another portal — we&apos;ll keep going.
            </p>
          )}
        </div>
      )}
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
  // absent
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
