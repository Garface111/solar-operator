import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  getToken,
  pingExtension,
  markExtensionInstalled,
} from "../lib/onboarding";

// Placeholder until the MV3 extension is published to the Chrome Web Store.
const CHROME_STORE_URL = "#TODO-publish-pending";

const POLL_MS = 3000;
// After this many consecutive ping failures we surface a "having trouble" hint
// and stop nagging the operator with toasts.
const FAIL_THRESHOLD = 5;

export default function Extension() {
  const navigate = useNavigate();
  const toast = useToast();
  const [installed, setInstalled] = useState(false);
  const [advancing, setAdvancing] = useState(false);
  const [pollFailures, setPollFailures] = useState(0);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const tokenRef = useRef<string | null>(getToken());

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
      await markExtensionInstalled(token);
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

        {sessionError && (
          <p className="mt-4 text-sm text-red-600">{sessionError}</p>
        )}

        <div className="mt-8 flex justify-end">
          <Button
            variant="secondary"
            onClick={handleManual}
            disabled={advancing}
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
        </div>
      </Card>
    </ScreenLayout>
  );
}
