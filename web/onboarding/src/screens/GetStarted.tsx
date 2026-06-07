import { useEffect, useRef } from "react";
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

function FadeUpPanel({ children, delay = 0 }: { children: React.ReactNode; delay?: number }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          el.classList.add("animate-fade-up");
          obs.disconnect();
        }
      },
      { threshold: 0.1 }
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  return (
    <div ref={ref} style={{ opacity: 0, animationDelay: `${delay}ms` }}>
      {children}
    </div>
  );
}

export default function GetStarted() {
  const navigate = useNavigate();

  return (
    <div className="mx-auto min-h-dvh max-w-2xl px-4 py-10 sm:py-14">
      {/* Hero */}
      <div className="mb-10">
        <h1 className="text-3xl font-semibold tracking-tight text-zinc-900">
          What you&apos;ll get
        </h1>
        <p className="mt-2 text-base text-zinc-500">
          Why we built this, and what changes once you&apos;re set up.
        </p>
      </div>

      {/* Stacked panels — each fades up as it scrolls into view */}
      <div className="flex flex-col gap-6">
        {PANELS.map((panel, i) => (
          <FadeUpPanel key={i} delay={i * 120}>
            <div className="rounded-2xl border border-zinc-200 bg-white p-8 shadow-sm">
              <div className="mb-4 text-4xl" aria-hidden="true">{panel.icon}</div>
              <h2 className="text-xl font-semibold leading-snug tracking-tight text-zinc-900">
                {panel.heading}
              </h2>
              <p className="mt-3 text-base leading-relaxed text-zinc-500">{panel.body}</p>
              {panel.cta && panel.ctaHref && (
                <a
                  href={panel.ctaHref}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-4 inline-flex items-center gap-1 text-sm font-medium text-primary-600 underline underline-offset-2 hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
                >
                  {panel.cta} ↗
                </a>
              )}
            </div>
          </FadeUpPanel>
        ))}
      </div>

      {/* Footer CTA — single primary action.
          Flow: GetStarted (intro animation) → /sample (workbook preview)
          → /welcome (Terms + checkout). Ford Jun 7'26: previously the
          primary jumped straight to /welcome, skipping the sample entirely
          and burying it behind a tiny secondary link nobody clicked.
          DummyReport carries its own "Start my free setup →" CTA so users
          who like what they see can continue from there. */}
      <div className="mt-10 flex flex-col items-center gap-3">
        <Button onClick={() => navigate("/sample")}>
          See the sample report →
        </Button>
      </div>

      <p className="mt-3 text-center text-sm text-zinc-500">
        Start free — 14-day trial, cancel anytime before your card is charged
      </p>

      <p className="mt-2 text-center text-xs text-zinc-400">
        $15/array/month &middot; $250 one-time setup &middot; cancel anytime
      </p>
    </div>
  );
}
