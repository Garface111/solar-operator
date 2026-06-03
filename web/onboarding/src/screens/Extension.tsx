import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import {
  getToken,
  pingExtension,
  markExtensionInstalled,
} from "../lib/onboarding";

// Placeholder until the MV3 extension is published to the Chrome Web Store.
const CHROME_STORE_URL = "#TODO-publish-pending";

const POLL_MS = 3000;

export default function Extension() {
  const navigate = useNavigate();
  const [installed, setInstalled] = useState(false);
  const [advancing, setAdvancing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tokenRef = useRef<string | null>(getToken());

  // Poll extension-ping every 3s; auto-advance to /clients when installed.
  useEffect(() => {
    const token = tokenRef.current;
    if (!token) {
      setError(
        "We couldn't find your onboarding session. Please restart from the welcome screen.",
      );
      return;
    }

    let cancelled = false;

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
        if (ok) {
          setInstalled(true);
          void advance();
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Connection problem");
        }
      }
    }

    void tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [navigate]);

  async function handleManual() {
    const token = tokenRef.current;
    if (!token || advancing) return;
    setAdvancing(true);
    try {
      await markExtensionInstalled(token);
      navigate("/clients");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't continue");
      setAdvancing(false);
    }
  }

  const storeUnpublished = CHROME_STORE_URL.startsWith("#");

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
            className="inline-flex items-center justify-center gap-2 rounded-xl bg-primary-500 px-5 py-3 text-sm font-medium text-white transition-colors hover:bg-primary-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-2"
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
                installed
                  ? "bg-primary-500"
                  : "animate-pulse bg-amber-400",
              ].join(" ")}
            />
            <span className="text-sm font-medium text-zinc-700">
              {installed
                ? "Capture received — taking you to the next step…"
                : "We're waiting for your first GMP capture…"}
            </span>
          </div>
          {!installed && (
            <p className="mt-2 pl-5 text-xs text-zinc-500">
              Checking every few seconds. Leave this tab open while you install
              the extension and sign into GMP.
            </p>
          )}
        </div>

        {error && <p className="mt-4 text-sm text-red-600">{error}</p>}

        <div className="mt-8 flex justify-end">
          <Button
            variant="secondary"
            onClick={handleManual}
            disabled={advancing}
          >
            I&apos;ve installed it →
          </Button>
        </div>
      </Card>
    </ScreenLayout>
  );
}
