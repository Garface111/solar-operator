import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { Modal } from "../ui/Modal";
import { useToast } from "../ui/Toast";
import { openPortalTab, gmpPortalUrl } from "../lib/openPortalTab";
import {
  getToken,
  fetchStatus,
  reconcileCheckout,
  pingExtension,
} from "../lib/onboarding";

const CHROME_STORE_URL =
  "https://chromewebstore.google.com/detail/solar-operator-sync/ocohbimolfpnkjcjhiodopjjlhclinpl";

type Provider = "gmp" | "vec";

const PORTALS: { id: Provider; name: string; short: string; url: string }[] = [
  {
    id: "gmp",
    name: "Green Mountain Power",
    short: "GMP",
    url: "https://www.greenmountainpower.com/account/",
  },
  {
    id: "vec",
    name: "Vermont Electric Co-op",
    short: "VEC",
    url: "https://vermontelectric.smarthub.coop/",
  },
];

// How long to wait (silently) before surfacing the "Having trouble?" affordance.
const HELP_TIMEOUT_MS = 45_000;

// How often to retry fetching the activation_code from /v1/onboarding/status
// while the Stripe webhook is still in flight. The happy path resolves on the
// first try; we keep trying quietly behind the scenes for slower webhooks.
const STATUS_RETRY_MS = 3_000;
const STATUS_MAX_RETRIES = 20;

const PING_INTERVAL_MS = 3_000;
const PING_MAX_ATTEMPTS = 60;

function uuid(): string {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const c: any = typeof crypto !== "undefined" ? crypto : null;
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
  } catch {
    /* fall through */
  }
  return `r-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

interface LoginState {
  provider: Provider;
  state: "login_required" | "signed_in" | "unknown";
  at: string;
}

export default function Extension() {
  const navigate = useNavigate();
  const toast = useToast();

  const tokenRef = useRef<string | null>(getToken());
  const sessionIdRef = useRef<string | null>(
    new URLSearchParams(window.location.search).get("session_id"),
  );

  const [sessionError, setSessionError] = useState<string | null>(null);
  const [paymentActive, setPaymentActive] = useState(false);
  const [paymentState, setPaymentState] =
    useState<"none" | "confirming" | "processing">("none");

  // Bridge state
  const [extensionPresent, setExtensionPresent] = useState(false);
  const [extVersion, setExtVersion] = useState<string | null>(null);
  const [paired, setPaired] = useState(false);
  const [activationCode, setActivationCode] = useState<string | null>(null);
  const pairAttemptedRef = useRef(false);

  // The portal the user most recently opened from this screen.
  const [openingProvider, setOpeningProvider] = useState<Provider | null>(null);
  const [activeProvider, setActiveProvider] = useState<Provider | null>(null);
  const [loginState, setLoginState] = useState<LoginState | null>(null);

  // Capture / advance
  const [landed, setLanded] = useState<{ accountCount: number; provider: Provider } | null>(null);
  const navigatingRef = useRef(false);

  // Safety net — surface troubleshooting only after a long silent wait.
  const [showHelpLink, setShowHelpLink] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);

  // -------- 1. Listen to the bridge --------
  useEffect(() => {
    function onMessage(event: MessageEvent) {
      if (event.source !== window) return;
      const data = event.data;
      if (!data || typeof data !== "object") return;

      if (data.type === "SO_EXTENSION_PRESENT") {
        setExtensionPresent(true);
        if (typeof data.version === "string") setExtVersion(data.version);
        return;
      }

      if (data.type === "SO_LOGIN_STATE") {
        const p = data.provider as Provider;
        setLoginState({ provider: p, state: data.state, at: data.at });
        return;
      }

      if (data.type === "SO_CAPTURE_LANDED") {
        if (!data.ok) return;
        if (navigatingRef.current) return;
        navigatingRef.current = true;
        setLanded({
          accountCount: Number(data.accountCount ?? 0),
          provider: data.provider as Provider,
        });
        window.setTimeout(() => navigate("/done"), 1100);
        return;
      }

      if (data.type === "SO_PAIR_ACK" && data.ok) {
        setPaired(true);
        return;
      }

      if (data.type === "SO_STATUS_ACK" && data.ok) {
        // SO_STATUS_ACK is itself proof the bridge is alive — the SO_EXTENSION_PRESENT
        // broadcast fires at document_start which is BEFORE this React effect mounts,
        // so we'd otherwise miss it and never auto-pair. Treat a successful status
        // response as equivalent to an extension-present signal.
        setExtensionPresent(true);
        if (typeof data.version === "string") setExtVersion((v) => v ?? data.version);
        if (data.tenantKeySet) setPaired(true);
        if (data.loginState) {
          setLoginState({
            provider: data.loginState.provider,
            state: data.loginState.state,
            at: data.loginState.at,
          });
        }
        return;
      }
    }
    window.addEventListener("message", onMessage);

    // Ask the bridge for current state in case SO_EXTENSION_PRESENT already fired
    // before this listener mounted (race on fast page loads).
    window.postMessage({ type: "SO_STATUS_REQUEST", reqId: uuid() }, "*");

    return () => window.removeEventListener("message", onMessage);
  }, [navigate]);

  // -------- 2. Fetch activation_code quietly --------
  useEffect(() => {
    const token = tokenRef.current;
    if (!token) {
      setSessionError(
        "We couldn't find your onboarding session. Please restart from the welcome screen.",
      );
      return;
    }
    let cancelled = false;
    let retries = 0;
    async function loop() {
      if (cancelled) return;
      try {
        const status = await fetchStatus(token!);
        if (cancelled) return;
        if (status.active) setPaymentActive(true);
        if (status.activation_code) {
          setActivationCode(status.activation_code);
          return;
        }
      } catch {
        /* non-fatal; retry */
      }
      retries += 1;
      if (retries < STATUS_MAX_RETRIES && !cancelled) {
        window.setTimeout(loop, STATUS_RETRY_MS);
      }
    }
    void loop();
    return () => {
      cancelled = true;
    };
  }, []);

  // -------- 3. Paid-but-inactive self-heal (unchanged behavior) --------
  useEffect(() => {
    const token = tokenRef.current;
    const sessionId = sessionIdRef.current;
    if (!token || !sessionId) return;

    let cancelled = false;
    let timer: number | undefined;
    let ticks = 0;
    const MAX_TICKS = 10;
    // After MAX_TICKS exhausted we slow the poll cadence but DON'T stop —
    // Stripe webhooks sometimes lag minutes when their infra is slow, and
    // the operator should never be stranded with no recovery path.
    const SLOW_POLL_MS = 10_000;

    async function step(first: boolean) {
      if (cancelled) return;
      try {
        let st = await fetchStatus(token!);
        if (cancelled) return;
        if (first) {
          if (st.active || st.stage !== "pending_payment") return;
          setPaymentState("confirming");
          st = await reconcileCheckout(token!, sessionId!);
          if (cancelled) return;
        }
        if (st.active) {
          setPaymentActive(true);
          setPaymentState("none");
          return;
        }
      } catch {
        if (cancelled) return;
        setPaymentState((s) => (s === "none" ? "confirming" : s));
      }
      ticks += 1;
      if (ticks >= MAX_TICKS) {
        if (!cancelled) setPaymentState("processing");
        // Keep polling in the background at a slower cadence so a delayed
        // webhook still flips us to active. The operator also has the
        // "Continue anyway" affordance on the processing card.
        timer = window.setTimeout(() => void step(false), SLOW_POLL_MS);
        return;
      }
      timer = window.setTimeout(() => void step(false), 3000);
    }

    void step(true);
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  // -------- 4. AUTO-PAIR --------
  useEffect(() => {
    if (pairAttemptedRef.current) return;
    if (!extensionPresent || !activationCode) return;
    pairAttemptedRef.current = true;
    const endpoint = `${window.location.origin}/v1/sync`;
    window.postMessage(
      {
        type: "SO_PAIR",
        tenantKey: activationCode,
        endpoint,
        reqId: uuid(),
      },
      "*",
    );
  }, [extensionPresent, activationCode]);

  // -------- 5. Help-link safety net --------
  useEffect(() => {
    const id = window.setTimeout(() => setShowHelpLink(true), HELP_TIMEOUT_MS);
    return () => window.clearTimeout(id);
  }, []);

  // -------- 6. Capture-ping fallback poll --------
  // Guards against the SO_CAPTURE_LANDED postMessage never arriving (cross-tab
  // timing race, old extension version, content script not injected here).
  // First-to-fire wins via navigatingRef; both paths are safe to run concurrently.
  useEffect(() => {
    const token = tokenRef.current;
    if (!token) return;
    if (landed !== null) return;
    if (navigatingRef.current) return;
    const inFlight = loginState?.state === "signed_in" || openingProvider !== null;
    if (!inFlight) return;

    let attempts = 0;
    const id = window.setInterval(() => {
      if (navigatingRef.current) {
        window.clearInterval(id);
        return;
      }
      attempts += 1;
      if (attempts > PING_MAX_ATTEMPTS) {
        window.clearInterval(id);
        return;
      }
      void pingExtension(token)
        .then((ping) => {
          if (ping.installed && !navigatingRef.current) {
            navigatingRef.current = true;
            setLanded({ accountCount: 0, provider: openingProvider ?? "gmp" });
            window.setTimeout(() => navigate("/done"), 1100);
          }
        })
        .catch(() => { /* swallow — keep polling */ });
    }, PING_INTERVAL_MS);

    return () => window.clearInterval(id);
  }, [loginState?.state, openingProvider, landed, navigate]);

  // -------- Status line copy --------
  function statusLine(): { text: string; tone: "waiting" | "active" | "success" } {
    if (landed) {
      if (landed.accountCount === 0) {
        return { text: "Your first client landed ✓", tone: "success" };
      }
      return {
        text: `Your first client landed: ${landed.accountCount} account${landed.accountCount === 1 ? "" : "s"} captured ✓`,
        tone: "success",
      };
    }
    // Prefer the live broadcast if it matches the portal the user just opened.
    const ls = loginState;
    if (ls) {
      const portal = PORTALS.find((p) => p.id === ls.provider);
      const name = portal?.name ?? "your utility";
      if (ls.state === "signed_in") {
        return { text: "Signed in — capturing your data…", tone: "active" };
      }
      if (ls.state === "login_required") {
        // Only narrate this for a portal we know the user opened from here;
        // otherwise fall back to the gentler "waiting" copy.
        if (activeProvider === ls.provider || openingProvider === ls.provider) {
          return { text: `Sign in at ${name}`, tone: "active" };
        }
      }
    }
    if (openingProvider) {
      const portal = PORTALS.find((p) => p.id === openingProvider);
      return {
        text: `Opening ${portal?.name ?? "your utility"}…`,
        tone: "active",
      };
    }
    return {
      text: "Waiting for you to open a utility portal…",
      tone: "waiting",
    };
  }

  async function openPortal(p: Provider, url: string) {
    const resolvedUrl = p === "gmp" ? gmpPortalUrl(extVersion) : url;
    setOpeningProvider(p);
    try {
      await openPortalTab(resolvedUrl);
      setActiveProvider(p);
    } catch {
      toast.error("Couldn't open that portal — try clicking again.");
    } finally {
      // Clear "Opening…" once the call settles; live state takes over from here.
      setOpeningProvider((cur) => (cur === p ? null : cur));
    }
  }

  async function handleManualContinue() {
    if (navigatingRef.current) return;
    navigatingRef.current = true;
    navigate("/done");
  }

  const status = statusLine();

  return (
    <ScreenLayout current={3}>
      <Card active>
        {paymentActive && (
          <div className="mb-5 inline-flex items-center gap-2 rounded-lg border border-primary-200 bg-primary-50 px-3 py-1.5 text-sm font-medium text-primary-700">
            <span aria-hidden>✓</span>
            Trial started — your account is active.
          </div>
        )}

        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Connect your utility.
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          Open the portal you use, sign in once, and we&apos;ll do the rest from
          here. No codes to copy, no settings to fiddle with.
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
              This usually takes a minute. We&apos;ll keep watching and flip
              you to active the moment Stripe confirms — you don&apos;t need
              to pay again. Feel free to keep going below; we&apos;ll catch up.
            </p>
            <button
              type="button"
              onClick={() => setPaymentActive(true)}
              className="mt-3 inline-flex items-center justify-center rounded-lg border border-amber-400 bg-white px-3 py-1.5 text-xs font-medium text-amber-900 transition-colors hover:bg-amber-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/40"
            >
              Continue anyway →
            </button>
          </div>
        )}

        {/* Pair badge (tiny, calm). Only after the extension is paired. */}
        {paired && (
          <div className="mt-6 inline-flex items-center gap-2 rounded-full border border-primary-200 bg-primary-50 px-3 py-1 text-xs font-medium text-primary-700">
            <span aria-hidden>✓</span>
            Extension paired
          </div>
        )}

        {/* If the extension isn't here yet, point them at the store — quietly. */}
        {!extensionPresent && (
          <div className="mt-6 rounded-xl border border-zinc-200 bg-white px-4 py-4">
            <p className="text-sm text-zinc-700">
              First, add Solar Operator Sync to Chrome — it&apos;s the little
              helper that watches your utility tab for bills.
            </p>
            <a
              href={CHROME_STORE_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-3 inline-flex items-center justify-center gap-2 rounded-xl bg-primary-500 px-5 py-3 text-sm font-medium text-white transition-colors duration-150 ease-in-out hover:bg-primary-600 active:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
            >
              Add Solar Operator Sync to Chrome ↗
            </a>
            <p className="mt-2 text-xs text-zinc-500">
              We&apos;ll notice it the moment it&apos;s installed — no need to
              refresh.
            </p>
          </div>
        )}

        {/* Portal choice + live status — the heart of the screen. */}
        <div className="mt-8 rounded-2xl border border-primary-200 bg-primary-50/40 px-5 py-5">
          <div className="text-sm font-semibold text-zinc-900">
            Which utility does this client use?
          </div>
          <div className="mt-4 flex flex-wrap gap-3">
            {PORTALS.map((p) => {
              const isOpening = openingProvider === p.id;
              const isActive = activeProvider === p.id;
              const baseStyle = isActive
                ? "border border-primary-300 bg-white text-primary-700 hover:bg-primary-50"
                : "bg-primary-500 text-white hover:bg-primary-600 active:bg-primary-700";
              return (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => void openPortal(p.id, p.url)}
                  disabled={!!landed}
                  className={`inline-flex items-center justify-center gap-2 rounded-xl px-5 py-3 text-sm font-medium transition-colors duration-150 ease-in-out focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2 disabled:opacity-60 ${baseStyle}`}
                >
                  {isActive && <span aria-hidden>✓</span>}
                  {isOpening ? `Opening ${p.short}…` : `Open ${p.name} →`}
                </button>
              );
            })}
          </div>

          {/* Live status line */}
          <div className="mt-5 flex items-center gap-3" aria-live="polite">
            <span
              aria-hidden
              className={[
                "h-2.5 w-2.5 shrink-0 rounded-full",
                status.tone === "success"
                  ? "bg-primary-500"
                  : status.tone === "active"
                    ? "animate-pulse bg-primary-500"
                    : "animate-pulse bg-amber-400",
              ].join(" ")}
            />
            <span className="text-sm font-medium text-zinc-700">
              {status.text}
            </span>
          </div>
          <p className="mt-3 text-xs text-zinc-500">
            Manage clients on both GMP and VEC? Sign into each — we&apos;ll
            stitch them together for you.
          </p>
        </div>

        {sessionError && (
          <div className="mt-6 rounded-xl border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-sm text-red-700">{sessionError}</p>
            <Link
              to="/"
              className="mt-2 inline-flex items-center gap-1 text-sm font-medium text-red-700 underline underline-offset-2 hover:text-red-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/40 focus-visible:ring-offset-2"
            >
              Restart setup →
            </Link>
          </div>
        )}

        {/* Footer: troubleshooting fallback + manual continue. Quiet by design. */}
        <div className="mt-8 flex items-center justify-between gap-4">
          <div className="text-xs text-zinc-500">
            {showHelpLink ? (
              <button
                type="button"
                onClick={() => setHelpOpen(true)}
                className="font-medium text-primary-700 underline underline-offset-2 hover:text-primary-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
              >
                Having trouble?
              </button>
            ) : (
              <span>&nbsp;</span>
            )}
          </div>
          <Button
            variant="secondary"
            onClick={() => void handleManualContinue()}
            disabled={!!sessionError}
          >
            Continue →
          </Button>
        </div>
      </Card>

      <Modal
        open={helpOpen}
        onClose={() => setHelpOpen(false)}
        title="Not seeing anything yet?"
      >
        <p className="text-sm text-zinc-600">
          Run through this quick list — one of these is almost always it:
        </p>
        <ul className="mt-4 flex flex-col gap-3">
          {[
            "Solar Operator Sync is installed in Chrome (puzzle-piece icon → pinned)",
            "You opened your utility portal from one of the buttons above (not a bookmark in a different browser)",
            "You actually signed in — capture only fires once your account page loads",
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
          and we&apos;ll walk you through it ourselves.
        </p>
        <div className="mt-6 flex justify-end">
          <Button
            variant="secondary"
            onClick={() => {
              setHelpOpen(false);
              void handleManualContinue();
            }}
          >
            Continue anyway →
          </Button>
        </div>
      </Modal>
    </ScreenLayout>
  );
}
