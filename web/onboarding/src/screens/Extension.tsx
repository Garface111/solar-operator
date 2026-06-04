import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { Modal } from "../ui/Modal";
import { useToast } from "../ui/Toast";
import {
  getToken,
  pingExtension,
  markExtensionInstalled,
  fetchStatus,
  reconcileCheckout,
} from "../lib/onboarding";

// Placeholder until the MV3 extension is published to the Chrome Web Store.
const CHROME_STORE_URL = "https://chromewebstore.google.com/detail/solar-operator-sync/ocohbimolfpnkjcjhiodopjjlhclinpl";

// Where to send the tenant to trigger their first capture.
const GMP_LOGIN_URL = "https://www.greenmountainpower.com/account/";

const POLL_MS = 3000;
// After this many consecutive ping failures we surface a "having trouble" hint
// and stop nagging the operator with toasts.
const FAIL_THRESHOLD = 5;
// After this many poll ticks with no capture (~30s at POLL_MS) we offer the
// troubleshooting modal. This counts *waiting*, not network failures — the
// common bounce is "reachable server, tenant never logged into GMP".
const HELP_THRESHOLD = 10;

export default function Extension() {
  const navigate = useNavigate();
  const toast = useToast();
  const [installed, setInstalled] = useState(false);
  const [advancing, setAdvancing] = useState(false);
  const [pollFailures, setPollFailures] = useState(0);
  const [waitTicks, setWaitTicks] = useState(0);
  const [helpOpen, setHelpOpen] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [activationCode, setActivationCode] = useState<string | null>(null);
  const [codeFailed, setCodeFailed] = useState(false);
  // Bumping this re-runs the activation-code retry loop (manual "Refresh").
  const [codeReload, setCodeReload] = useState(0);
  const [copied, setCopied] = useState(false);
  // Paid-but-inactive self-heal: when we return from Stripe with a session_id
  // but the webhook hasn't activated us yet, we reassure + reconcile rather
  // than letting the operator hit a 402 "pay again".
  const [paymentState, setPaymentState] =
    useState<"none" | "confirming" | "processing">("none");
  const tokenRef = useRef<string | null>(getToken());
  const sessionIdRef = useRef<string | null>(
    new URLSearchParams(window.location.search).get("session_id"),
  );

  // Fetch the activation code (tenant_key) the moment the tenant is active.
  // The user needs to paste this into the extension's options page so its
  // posts to /v1/sync are authenticated against their tenant.
  useEffect(() => {
    const token = tokenRef.current;
    if (!token) return;
    let cancelled = false;
    let retries = 0;
    const MAX_RETRIES = 20; // ~60s of retries at 3s intervals
    setCodeFailed(false);
    async function loadCode() {
      if (cancelled) return;
      try {
        const status = await fetchStatus(token!);
        if (cancelled) return;
        if (status.activation_code) {
          setActivationCode(status.activation_code);
          return; // got it — stop retrying
        }
      } catch {
        /* non-fatal — fall through to retry */
      }
      // No code yet (webhook hasn't fired or tenant not active) — try again
      retries += 1;
      if (retries < MAX_RETRIES && !cancelled) {
        window.setTimeout(loadCode, 3000);
      } else if (!cancelled) {
        // Retries exhausted — stop the silent permanent "Loading…" and offer
        // a real error + manual refresh instead.
        setCodeFailed(true);
      }
    }
    void loadCode();
    return () => {
      cancelled = true;
    };
  }, [codeReload]);

  // Paid-but-inactive self-heal. On mount, if we returned from Stripe with a
  // session_id but we're still pending_payment (webhook lag), verify the
  // Checkout session server-side and poll until active — up to ~30s — instead
  // of stranding a paying customer.
  useEffect(() => {
    const token = tokenRef.current;
    const sessionId = sessionIdRef.current;
    if (!token || !sessionId) return;

    let cancelled = false;
    let timer: number | undefined;
    let ticks = 0;
    const MAX_TICKS = 10; // ~30s at POLL_MS

    async function loop(first: boolean) {
      if (cancelled) return;
      try {
        let st = await fetchStatus(token!);
        if (cancelled) return;
        if (first) {
          if (st.active || st.stage !== "pending_payment") {
            return; // nothing to heal — webhook already landed
          }
          setPaymentState("confirming");
          // Kick the server-side self-heal once, then poll for the result.
          st = await reconcileCheckout(token!, sessionId!);
          if (cancelled) return;
        }
        if (st.active) {
          setPaymentState("none");
          return;
        }
      } catch {
        if (cancelled) return;
        // Network/Stripe hiccup — keep the reassuring state and keep polling.
        setPaymentState((s) => (s === "none" ? "confirming" : s));
      }
      ticks += 1;
      if (ticks >= MAX_TICKS) {
        if (!cancelled) setPaymentState("processing");
        return;
      }
      timer = window.setTimeout(() => void loop(false), POLL_MS);
    }

    void loop(true);
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  async function handleCopy() {
    if (!activationCode) return;
    try {
      await navigator.clipboard.writeText(activationCode);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Couldn't copy automatically — please select and copy manually.");
    }
  }

  // Poll extension-ping every 3s; auto-advance to /clients when installed.
  useEffect(() => {
    const token = tokenRef.current;
    if (!token) {
      setSessionError(
        "We couldn't find your onboarding session. Please restart from the welcome screen.",
      );
      return;
    }

    let cancelled = false;
    let failures = 0;
    let waits = 0;

    async function advance() {
      if (cancelled) return;
      setAdvancing(true);
      try {
        await markExtensionInstalled(token!);
      } catch {
        /* non-fatal — ping already proved a capture landed */
      }
      if (!cancelled) navigate("/clients");
    }

    async function tick() {
      try {
        const { installed: ok } = await pingExtension(token!);
        if (cancelled) return;
        failures = 0;
        setPollFailures(0);
        if (ok) {
          setInstalled(true);
          void advance();
          return;
        }
      } catch {
        if (cancelled) return;
        failures += 1;
        setPollFailures(failures);
        // One toast exactly when we cross the threshold — not on every tick.
        if (failures === FAIL_THRESHOLD) {
          toast.error(
            "We're having trouble reaching the server to detect your extension.",
          );
        }
      }
      // Reached only while still waiting (no capture yet, success or failure).
      // After ~30s we offer the troubleshooting modal.
      waits += 1;
      setWaitTicks(waits);
    }

    void tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [navigate, toast]);

  async function handleManual() {
    const token = tokenRef.current;
    if (!token || advancing) return;
    setAdvancing(true);
    try {
      await markExtensionInstalled(token, sessionIdRef.current);
      navigate("/clients");
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't continue. Please try again.",
      );
      setAdvancing(false);
    }
  }

  const storeUnpublished = CHROME_STORE_URL.startsWith("#");
  const havingTrouble = pollFailures >= FAIL_THRESHOLD;
  const showHelp = waitTicks >= HELP_THRESHOLD && !installed;

  return (
    <ScreenLayout current={2}>
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Install the Solar Operator Sync extension.
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          The Chrome extension securely captures your GMP bills so we can build
          your reports. Install it, then log into GMP once — we&apos;ll detect
          it automatically.
        </p>

        {paymentState === "confirming" && (
          <div className="mt-6 flex items-center gap-3 rounded-xl border border-primary-200 bg-primary-50 px-4 py-3">
            <Spinner />
            <div>
              <p className="text-sm font-medium text-zinc-900">
                Confirming your payment…
              </p>
              <p className="text-xs text-zinc-500">
                Hang tight — we&apos;re verifying your subscription with Stripe.
                This only takes a moment.
              </p>
            </div>
          </div>
        )}

        {paymentState === "processing" && (
          <div className="mt-6 rounded-xl border border-amber-300 bg-amber-50 px-4 py-4">
            <p className="text-sm font-medium text-amber-900">
              Your payment is processing
            </p>
            <p className="mt-1 text-xs leading-relaxed text-amber-800">
              This usually takes a minute. We&apos;ll email you the moment it
              clears, and you can pick up right where you left off — no need to
              pay again.
            </p>
            <a
              href="/onboarding/?recover=1"
              className="mt-3 inline-flex items-center justify-center gap-2 rounded-xl border border-amber-300 bg-white px-4 py-2 text-sm font-medium text-amber-800 transition-colors duration-150 hover:bg-amber-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/40 focus-visible:ring-offset-2"
            >
              I&apos;ll check back later
            </a>
          </div>
        )}

        <div className="mt-8 flex flex-col gap-3">
          <a
            href={CHROME_STORE_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center justify-center gap-2 rounded-xl bg-primary-500 px-5 py-3 text-sm font-medium text-white transition-colors duration-150 ease-in-out hover:bg-primary-600 active:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
          >
            Install Solar Operator Sync from the Chrome Web Store ↗
          </a>
          {storeUnpublished && (
            <p className="text-xs text-amber-600">
              Heads up: the Chrome Web Store listing is still pending publication
              — this link is a placeholder for now.
            </p>
          )}
        </div>

        {/* Activation code — paste into the extension's options page */}
        <div className="mt-6 rounded-xl border border-zinc-200 bg-white px-4 py-4">
          <div className="text-sm font-medium text-zinc-900">
            Step 2 — paste your activation code into the extension
          </div>
          <p className="mt-1 text-xs text-zinc-500">
            Once the extension is installed, click its icon in your Chrome
            toolbar → <strong>Options</strong>, paste this code into{" "}
            <strong>Activation code</strong>, and click Save. This links the
            extension to your account so we can find your bills.
          </p>
          {codeFailed && !activationCode ? (
            <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-3">
              <p className="text-xs leading-relaxed text-amber-800">
                We&apos;re still confirming your activation code — this can take
                up to a minute after payment.
              </p>
              <button
                type="button"
                onClick={() => {
                  setCodeFailed(false);
                  setCodeReload((n) => n + 1);
                }}
                className="mt-2 inline-flex items-center justify-center rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs font-medium text-amber-800 transition-colors duration-150 hover:bg-amber-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/40 focus-visible:ring-offset-2"
              >
                Refresh
              </button>
            </div>
          ) : (
            <div className="mt-3 flex items-stretch gap-2">
              <code className="flex-1 select-all rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 font-mono text-sm text-zinc-800 break-all">
                {activationCode ?? "Loading…"}
              </code>
              <button
                type="button"
                onClick={handleCopy}
                disabled={!activationCode}
                className="inline-flex items-center justify-center rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs font-medium text-zinc-700 transition-colors duration-150 hover:bg-zinc-50 active:bg-zinc-100 disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
                aria-label="Copy activation code"
              >
                {copied ? "Copied ✓" : "Copy"}
              </button>
            </div>
          )}
        </div>

        {/* Activation guidance — the #1 onboarding bounce point is the tenant
            not realizing they still have to log into GMP to trigger a capture. */}
        <div className="mt-8 rounded-xl border border-primary-200 bg-primary-50 px-5 py-5">
          <div className="text-base font-semibold tracking-tight text-zinc-900">
            Almost there — activate by logging into GMP
          </div>
          <ol className="mt-4 flex flex-col gap-4">
            {[
              "Install the extension above",
              "Paste your activation code into the extension's Options page",
              "Log into your Green Mountain Power account in any tab — we'll detect your bills automatically",
            ].map((step, i) => (
              <li key={i} className="flex items-start gap-3">
                <span
                  aria-hidden
                  className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary-500 text-xs font-semibold text-white"
                >
                  {i + 1}
                </span>
                <span className="text-sm leading-snug text-zinc-700">
                  {step}
                </span>
              </li>
            ))}
          </ol>
          <a
            href={GMP_LOGIN_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-5 inline-flex items-center justify-center gap-2 rounded-xl bg-primary-500 px-5 py-3 text-sm font-medium text-white transition-colors duration-150 ease-in-out hover:bg-primary-600 active:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
          >
            Open Green Mountain Power →
          </a>
          <p className="mt-3 text-xs text-zinc-500">
            You don&apos;t need to do anything else. We pick up your bills in the
            background.
          </p>
        </div>

        <div className="mt-8 rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-4">
          <div className="flex items-center gap-3">
            <span
              aria-hidden
              className={[
                "h-2.5 w-2.5 rounded-full",
                installed ? "bg-primary-500" : "animate-pulse bg-amber-400",
              ].join(" ")}
            />
            <span className="text-sm font-medium text-zinc-700" aria-live="polite">
              {installed
                ? "Capture received — taking you to the next step…"
                : "We're waiting for your first GMP capture…"}
            </span>
          </div>
          {!installed && !havingTrouble && (
            <p className="mt-2 pl-5 text-xs text-zinc-500">
              Checking every few seconds. Leave this tab open while you install
              the extension and sign into GMP.
            </p>
          )}
          {!installed && havingTrouble && (
            <p className="mt-2 pl-5 text-xs text-amber-700">
              Having trouble detecting the extension? Click &quot;I&apos;ve
              installed it&quot; below.
            </p>
          )}
        </div>

        {showHelp && (
          <div className="mt-4 flex justify-center">
            <button
              type="button"
              onClick={() => setHelpOpen(true)}
              className="inline-flex animate-pulse items-center justify-center gap-2 rounded-xl border border-amber-300 bg-amber-50 px-5 py-2.5 text-sm font-medium text-amber-800 transition-colors duration-150 hover:bg-amber-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/40 focus-visible:ring-offset-2"
            >
              Having trouble?
            </button>
          </div>
        )}

        {sessionError && (
          <div className="mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-sm text-red-700">{sessionError}</p>
            <Link
              to="/"
              className="mt-2 inline-flex items-center gap-1 text-sm font-medium text-red-700 underline underline-offset-2 hover:text-red-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/40 focus-visible:ring-offset-2"
            >
              Restart setup →
            </Link>
          </div>
        )}

        <div className="mt-8 flex flex-col items-end gap-1.5">
          <Button
            variant="secondary"
            onClick={handleManual}
            disabled={advancing || !!sessionError}
          >
            {advancing ? (
              <>
                <Spinner />
                Continuing…
              </>
            ) : (
              "I've installed it →"
            )}
          </Button>
          {sessionError && (
            <p className="text-xs text-zinc-400">
              Session expired — restart above to continue.
            </p>
          )}
        </div>
      </Card>

      <Modal
        open={helpOpen}
        onClose={() => setHelpOpen(false)}
        title="Not seeing a capture yet?"
      >
        <p className="text-sm text-zinc-600">
          Run through this checklist — one of these is almost always the reason:
        </p>
        <ul className="mt-4 flex flex-col gap-3">
          {[
            "You installed the extension from the Chrome Web Store (the Chrome toolbar should show its icon)",
            "You pasted the activation code into the extension's Options page and clicked Save",
            "You logged into greenmountainpower.com in any tab while the extension is active",
          ].map((item, i) => (
            <li key={i} className="flex items-start gap-3 text-sm text-zinc-700">
              <span aria-hidden className="mt-0.5 shrink-0 text-primary-600">
                ✓
              </span>
              <span className="leading-snug">{item}</span>
            </li>
          ))}
        </ul>
        <p className="mt-5 text-sm text-zinc-600">
          Still stuck? Email{" "}
          <a
            href="mailto:admin@solaroperator.org"
            className="font-medium text-primary-600 hover:text-primary-700"
          >
            admin@solaroperator.org
          </a>{" "}
          and we&apos;ll personally walk you through it.
        </p>
        <div className="mt-6 flex justify-end">
          <Button
            variant="secondary"
            onClick={() => {
              setHelpOpen(false);
              void handleManual();
            }}
            disabled={advancing}
          >
            {advancing ? (
              <>
                <Spinner />
                Continuing…
              </>
            ) : (
              "I've installed it manually — continue →"
            )}
          </Button>
        </div>
      </Modal>
    </ScreenLayout>
  );
}
