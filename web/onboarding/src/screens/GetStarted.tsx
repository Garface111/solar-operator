import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../ui/Button";

const CHROME_STORE_URL =
  "https://chromewebstore.google.com/detail/solar-operator-sync/ocohbimolfpnkjcjhiodopjjlhclinpl";

const PANELS = [
  {
    icon: "📋",
    heading: "Quarterly reports used to mean hours every three months.",
    body: "VT community solar operators spend 3–5 hours per client pulling utility bills, calculating net-metering credits, and formatting NEPOOL Excel reports. With 10 clients, that's a full day gone — every quarter.",
  },
  {
    icon: "⚡",
    heading: "Tell us how many arrays you manage. We handle the rest.",
    body: "Solar Operator connects to your utility account (Green Mountain Power, Vermont Electric Co-op, and more), pulls your bills automatically, and generates NEPOOL-format reports for every client — every quarter, without you touching a spreadsheet.",
  },
  {
    icon: "🌐",
    heading: "One requirement: Google Chrome.",
    body: "We use a lightweight Chrome extension to securely capture your utility bill data. It takes about two minutes to install and you only do it once.",
    cta: "Preview the Chrome extension →",
    ctaHref: CHROME_STORE_URL,
  },
];

const PANEL_MS = 4000;

export default function GetStarted() {
  const navigate = useNavigate();
  const [idx, setIdx] = useState(0);
  const [paused, setPaused] = useState(false);
  const isLast = idx === PANELS.length - 1;

  useEffect(() => {
    if (paused || isLast) return;
    const id = window.setTimeout(() => setIdx((n) => n + 1), PANEL_MS);
    return () => window.clearTimeout(id);
  }, [idx, paused, isLast]);

  function advance() {
    if (!isLast) {
      setPaused(true);
      setIdx((n) => n + 1);
    } else {
      navigate("/demo");
    }
  }

  const panel = PANELS[idx];

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center bg-gradient-to-b from-primary-50 to-white px-4 py-12">
      {/* Progress pills */}
      <div className="mb-10 flex items-center gap-2" role="tablist" aria-label="Intro progress">
        {PANELS.map((_, i) => (
          <button
            key={i}
            type="button"
            role="tab"
            aria-selected={i === idx}
            aria-label={`Panel ${i + 1}`}
            onClick={() => { setPaused(true); setIdx(i); }}
            className={[
              "h-2 rounded-full transition-all duration-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40",
              i === idx ? "w-7 bg-primary-500" : "w-2 bg-primary-200 hover:bg-primary-300",
            ].join(" ")}
          />
        ))}
      </div>

      {/* Panel card — key forces remount/animation on each slide */}
      <div
        key={idx}
        className="animate-panel-in w-full max-w-lg rounded-2xl border border-zinc-200 bg-white p-8 shadow-lg"
      >
        <div className="mb-5 text-5xl" aria-hidden="true">{panel.icon}</div>
        <h1 className="text-2xl font-semibold leading-snug tracking-tight text-zinc-900">
          {panel.heading}
        </h1>
        <p className="mt-4 text-base leading-relaxed text-zinc-500">{panel.body}</p>
        {panel.cta && panel.ctaHref && (
          <a
            href={panel.ctaHref}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-5 inline-flex items-center gap-1 text-sm font-medium text-primary-600 underline underline-offset-2 hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
          >
            {panel.cta} ↗
          </a>
        )}
      </div>

      {/* Controls */}
      <div className="mt-8 flex w-full max-w-lg items-center justify-between">
        <button
          type="button"
          onClick={() => navigate("/demo")}
          className="rounded px-3 py-2 text-sm text-zinc-400 transition-colors duration-150 hover:text-zinc-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
        >
          Skip intro
        </button>
        <Button onClick={advance}>
          {isLast ? "See a sample report →" : "Next →"}
        </Button>
      </div>

      {/* Auto-progress bar */}
      {!paused && !isLast && (
        <div className="mt-6 h-0.5 w-full max-w-lg overflow-hidden rounded-full bg-primary-100">
          <div
            key={`bar-${idx}`}
            className="h-full bg-primary-400"
            style={{
              width: "100%",
              animation: `grow-bar ${PANEL_MS}ms linear forwards`,
            }}
          />
        </div>
      )}
    </div>
  );
}
