import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../ui/Button";
import UtilitySearch from "../components/UtilitySearch";

/* ─── Slide visuals ─────────────────────────────────────────────────────────
   Each slide is a self-contained <Slide /> component so the layout per slide
   stays opinionated and tight — visuals on the left, text on the right at
   sm+, stacked on mobile. Ford Jun 8'26: "big clean visuals, slide show, sample
   first." Slide 1 = the artifact (calibrates trust). Slide 2 = the problem.
   Slide 3 = the mechanism. Slide 4 = Cloud Capture. Slide 5 = CTA.
   ─────────────────────────────────────────────────────────────────────────── */

/** A miniature, faithful preview of the GMCS workbook one sheet — mirrors
 *  api/writers/gmcs_writer.py output: A1:C1 merged title, quarter-blocks of
 *  three month rows. Stripped to ~6 rows for a quick read. */
function SampleReportVisual() {
  // Monthly rows mirror api/writers/gmcs_writer.py output: each quarter is
  // three month rows (Jul/Aug/Sep, Oct/Nov/Dec, ...) with MWh and floor(MWh)
  // RECs.  Showing month on every row keeps all three columns visibly
  // populated — the previous merge-cell convention (quarter label only on
  // the first row of each block) read as "missing data."
  const rows = [
    { period: "Jul 2025", mwh: 28.541, recs: 28 },
    { period: "Aug 2025", mwh: 31.82, recs: 31 },
    { period: "Sep 2025", mwh: 24.193, recs: 24 },
    { period: "Oct 2025", mwh: 16.72, recs: 16 },
    { period: "Nov 2025", mwh: 9.34, recs: 9 },
    { period: "Dec 2025", mwh: 7.081, recs: 7 },
  ];
  return (
    <div className="rounded-2xl border border-zinc-200 bg-white shadow-[0_10px_30px_-12px_rgba(0,0,0,0.18)] overflow-hidden">
      <div className="border-b border-zinc-200 bg-gradient-to-b from-zinc-50 to-white px-4 py-3">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-primary-700">
          Sample · NEPOOL-GIS workbook
        </p>
        <p className="mt-1 text-sm font-semibold text-zinc-900">
          Maple Ridge South (53984)
        </p>
      </div>
      <table className="w-full text-xs">
        <thead className="bg-zinc-50 text-[10px] uppercase tracking-wide text-zinc-500">
          <tr>
            <th className="px-3 py-2 text-left font-medium">Month</th>
            <th className="px-3 py-2 text-right font-medium">MWh</th>
            <th className="px-3 py-2 text-right font-medium">RECs</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr
              key={i}
              className={`border-t border-zinc-100 ${
                i === 3 ? "border-t-2 border-t-zinc-200" : ""
              }`}
            >
              <td className="px-3 py-2 font-medium text-zinc-800">{r.period}</td>
              <td className="px-3 py-2 text-right font-mono tabular-nums text-zinc-700">
                {r.mwh.toLocaleString("en-US", { maximumFractionDigits: 3 })}
              </td>
              <td className="px-3 py-2 text-right font-mono tabular-nums text-zinc-700">
                {r.recs}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="border-t border-zinc-100 bg-zinc-50 px-4 py-2 text-[10px] text-zinc-400">
        Rolling 6 quarters · one sheet per array · auto-emailed each quarter
      </div>
    </div>
  );
}

/** A "before" visual — a chaotic stack of receipt-looking shards with
 *  calculator + clock to convey "you currently piece this together by hand." */
function TimeSinkVisual() {
  return (
    <div className="relative rounded-2xl border border-amber-200 bg-amber-50 p-6 shadow-[0_10px_30px_-12px_rgba(0,0,0,0.18)]">
      <div className="absolute right-4 top-4 flex items-center gap-1.5 rounded-full bg-white px-3 py-1 text-[11px] font-semibold text-amber-700 shadow-sm">
        <span aria-hidden>⏱</span> 3–5 hrs / client
      </div>
      <div className="space-y-3">
        {/* Stack of "bills" */}
        {[
          { label: "GMP bill — Mar", offset: 0 },
          { label: "GMP bill — Apr", offset: 8 },
          { label: "GMP bill — May", offset: 16 },
        ].map((b) => (
          <div
            key={b.label}
            className="rounded-lg border border-amber-300 bg-white px-4 py-3 shadow-sm"
            style={{ marginLeft: b.offset }}
          >
            <div className="flex items-center justify-between text-xs">
              <span className="font-medium text-zinc-700">{b.label}</span>
              <span className="font-mono text-zinc-400">_ _ _ . _ _ _ kWh</span>
            </div>
            <div className="mt-1 h-2 w-2/3 rounded-full bg-amber-100" />
            <div className="mt-1 h-2 w-1/3 rounded-full bg-amber-100" />
          </div>
        ))}
        <div className="flex items-center gap-2 pl-4 text-xs text-amber-800">
          <span aria-hidden className="text-base">➜</span>
          <span className="rounded border border-zinc-300 bg-white px-2 py-1 font-mono">
            Excel.xlsx
          </span>
          <span aria-hidden>+</span>
          <span className="rounded border border-zinc-300 bg-white px-2 py-1 font-mono">
            🧮
          </span>
          <span aria-hidden>=</span>
          <span className="rounded border border-zinc-300 bg-white px-2 py-1 font-mono">
            🤯
          </span>
        </div>
      </div>
    </div>
  );
}

/** Pipeline visual — utility portal → cloud refresh → Excel. */
function PipelineVisual() {
  const Node = ({
    label,
    sub,
    icon,
    tone,
  }: {
    label: string;
    sub: string;
    icon: string;
    tone: "neutral" | "accent";
  }) => (
    <div
      className={`flex-1 rounded-xl border p-4 text-center ${
        tone === "accent"
          ? "border-primary-300 bg-primary-50"
          : "border-zinc-200 bg-white"
      }`}
    >
      <div className="text-2xl" aria-hidden>
        {icon}
      </div>
      <p className="mt-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
        {sub}
      </p>
      <p className="mt-0.5 text-sm font-semibold text-zinc-900">{label}</p>
    </div>
  );
  return (
    <div className="rounded-2xl border border-zinc-200 bg-white p-5 shadow-[0_10px_30px_-12px_rgba(0,0,0,0.18)]">
      <div className="flex items-center gap-2 sm:gap-3">
        <Node label="Your utility" sub="Hundreds, US-wide" icon="⚡" tone="neutral" />
        <span className="shrink-0 text-2xl text-zinc-300" aria-hidden>→</span>
        <Node label="Cloud Capture" sub="Bills 24/7" icon="☁️" tone="accent" />
        <span className="shrink-0 text-2xl text-zinc-300" aria-hidden>→</span>
        <Node label="NEPOOL workbook" sub="Auto-built" icon="📊" tone="neutral" />
      </div>
      <div className="mt-4 rounded-lg bg-zinc-50 px-4 py-3 text-center text-xs text-zinc-500">
        <span className="font-semibold text-zinc-700">Connect once.</span>{" "}
        Nothing to keep open. Reports go out every quarter on autopilot.
      </div>
    </div>
  );
}

/** Cloud Capture visual — store a login, we refresh. */
function CloudCaptureVisual() {
  return (
    <div className="rounded-2xl border border-zinc-200 bg-white p-6 shadow-[0_10px_30px_-12px_rgba(0,0,0,0.18)]">
      <div className="flex items-center gap-4">
        <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-primary-50 to-primary-100 text-3xl">
          ☁️
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-primary-700">
            Cloud Capture
          </p>
          <p className="mt-1 text-sm font-semibold text-zinc-900">
            Store a utility login once
          </p>
          <p className="mt-0.5 text-xs text-zinc-500">
            Encrypted on our servers · remove anytime
          </p>
        </div>
      </div>
      <div className="mt-5 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
        <span className="rounded-full bg-zinc-100 px-2 py-1 font-medium text-zinc-700">
          1. Add login
        </span>
        <span aria-hidden>→</span>
        <span className="rounded-full bg-zinc-100 px-2 py-1 font-medium text-zinc-700">
          2. We pull bills
        </span>
        <span aria-hidden>→</span>
        <span className="rounded-full bg-primary-50 px-2 py-1 font-medium text-primary-700">
          3. Reports build
        </span>
      </div>
    </div>
  );
}

/** Final-slide CTA visual: a clean stack of three "client cards" being marked complete. */
function HappyOperatorVisual() {
  return (
    <div className="rounded-2xl border border-primary-200 bg-gradient-to-br from-primary-50 to-white p-6 shadow-[0_10px_30px_-12px_rgba(0,0,0,0.18)]">
      <div className="space-y-2">
        {["Catamount Solar", "Maple Ridge South", "Tannery Brook"].map((c) => (
          <div
            key={c}
            className="flex items-center gap-3 rounded-xl border border-primary-200 bg-white px-4 py-3"
          >
            <span
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary-500 text-xs font-bold text-white"
              aria-hidden
            >
              ✓
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-semibold text-zinc-900">{c}</p>
              <p className="text-[11px] text-primary-700">Sent · Q1 2026</p>
            </div>
          </div>
        ))}
      </div>
      <p className="mt-4 text-center text-xs font-medium text-primary-700">
        Every quarter — without you lifting a finger.
      </p>
    </div>
  );
}

/* ─── Slide content ─────────────────────────────────────────────────────────── */

interface SlideDef {
  kicker: string;
  headline: string;
  body?: string;
  bullets?: { icon: string; text: string }[];
  visual: React.ReactNode;
  cta?: { label: string; href?: string; route?: string };
}

const slides: SlideDef[] = [
  // 1 — Overview. Sample report (the artifact) + a tight bullet list that
  // names the whole story in one screen. Subsequent slides drill in.
  // Ford Jun 8'26: "few bullets on the first page, granularity on the next."
  {
    kicker: "What you do as a NEPOOL agent",
    headline: "Quarterly reports, on autopilot.",
    bullets: [
      {
        icon: "📋",
        text: "You deliver a NEPOOL-GIS workbook every quarter — one sheet per array.",
      },
      {
        icon: "⏱",
        text: "By hand, that's 3–5 hours per client. A workweek for ten clients.",
      },
      {
        icon: "⚡",
        text: "NEPOOL Operator pulls every utility bill and builds the workbook for you — hundreds of utilities supported, coast to coast.",
      },
      {
        icon: "☁️",
        text: "Connect a utility login once. We keep bills fresh — nothing to keep open.",
      },
    ],
    visual: <SampleReportVisual />,
  },
  // 2 — Granularity on the problem.
  {
    kicker: "The problem, in detail",
    headline: "Quarterly reports eat your week.",
    body:
      "Every quarter you log into each utility portal, download three months of bills per client, transcribe kWh into a spreadsheet, calculate net-metering credits, and hand-format a NEPOOL-shaped workbook. With ten clients that's a full workweek — and the work produces nothing new for your business.",
    visual: <TimeSinkVisual />,
  },
  // 3 — Granularity on the mechanism.
  {
    kicker: "How NEPOOL Operator solves it",
    headline: "Connect once. Reports build themselves.",
    body:
      "NEPOOL Operator connects to the utility portals your clients already use — hundreds supported coast to coast — pulls each bill on a schedule, and renders the NEPOOL-GIS workbook on autopilot every quarter, formatted exactly the way ISO-NE expects.",
    visual: <PipelineVisual />,
  },
  // 4 — Cloud Capture (primary path).
  {
    kicker: "What you do, exactly once",
    headline: "We keep your bills fresh.",
    body:
      "Hand us a utility portal login once (encrypted on our servers) and Cloud Capture signs in and pulls bills around the clock — no install, no tab to keep open. Prefer passwords only on your computer? You can use the free browser extension instead.",
    visual: <CloudCaptureVisual />,
  },
  // 5 — Future state + primary CTA.
  {
    kicker: "What changes after setup",
    headline: "Every client. Every quarter. Without you.",
    body:
      "Once you're set up, reports go out on their own. You stay focused on growing your book — NEPOOL Operator handles the part that used to take a week.",
    visual: <HappyOperatorVisual />,
    cta: { label: "Start my free setup", route: "/welcome" },
  },
];

/* ─── Slide carousel ───────────────────────────────────────────────────────── */

export default function GetStarted() {
  const navigate = useNavigate();
  const [idx, setIdx] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  const total = slides.length;
  const slide = slides[idx];

  // Keyboard navigation
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "ArrowRight") setIdx((i) => Math.min(total - 1, i + 1));
      if (e.key === "ArrowLeft") setIdx((i) => Math.max(0, i - 1));
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [total]);

  return (
    <div className="mx-auto min-h-dvh max-w-5xl px-4 py-10 sm:py-14">
      {/* Progress + step counter */}
      <div className="mb-6 flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wider text-zinc-500">
          {String(idx + 1).padStart(2, "0")} / {String(total).padStart(2, "0")}
        </span>
        <div className="flex gap-1.5">
          {slides.map((_, i) => (
            <button
              key={i}
              type="button"
              onClick={() => setIdx(i)}
              aria-label={`Go to slide ${i + 1}`}
              className={`h-1.5 rounded-full transition-all duration-200 ${
                i === idx
                  ? "w-8 bg-primary-500"
                  : "w-1.5 bg-zinc-300 hover:bg-zinc-400"
              }`}
            />
          ))}
        </div>
      </div>

      {/* Slide */}
      <div
        ref={containerRef}
        key={idx}
        className="grid animate-fade-up grid-cols-1 gap-8 rounded-3xl border border-zinc-200 bg-white p-6 shadow-[0_30px_80px_-30px_rgba(0,0,0,0.18)] sm:p-10 lg:grid-cols-2 lg:items-center lg:gap-12"
        style={{ minHeight: 460 }}
      >
        {/* Visual side */}
        <div className="order-2 lg:order-1">{slide.visual}</div>

        {/* Text side */}
        <div className="order-1 flex flex-col gap-4 lg:order-2">
          <p className="text-xs font-semibold uppercase tracking-wider text-primary-700">
            {slide.kicker}
          </p>
          <h1 className="text-3xl font-semibold leading-tight tracking-tight text-zinc-900 sm:text-4xl">
            {slide.headline}
          </h1>
          {slide.body && (
            <p className="text-base leading-relaxed text-zinc-600 sm:text-lg">
              {slide.body}
            </p>
          )}
          {slide.bullets && (
            <ul className="mt-1 space-y-3">
              {slide.bullets.map((b, i) => (
                <li key={i} className="flex items-start gap-3">
                  <span
                    aria-hidden
                    className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary-50 text-base"
                  >
                    {b.icon}
                  </span>
                  <span className="text-base leading-relaxed text-zinc-700">
                    {b.text}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {slide.cta?.href && (
            <a
              href={slide.cta.href}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-1 inline-flex items-center gap-1 text-sm font-medium text-primary-600 underline underline-offset-2 hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 self-start"
            >
              {slide.cta.label} ↗
            </a>
          )}
          {slide.cta?.route && (
            <div className="mt-2">
              <Button onClick={() => navigate(slide.cta!.route!)}>
                {slide.cta.label} →
              </Button>
            </div>
          )}
        </div>
      </div>

      {/* Slide controls */}
      <div className="mt-6 flex items-center justify-between">
        <button
          type="button"
          onClick={() => setIdx((i) => Math.max(0, i - 1))}
          disabled={idx === 0}
          className="inline-flex items-center gap-2 rounded-xl border border-zinc-200 bg-white px-4 py-2 text-sm font-medium text-zinc-700 shadow-sm transition-colors hover:bg-zinc-50 disabled:opacity-40 disabled:hover:bg-white"
        >
          ← Back
        </button>

        {idx < total - 1 ? (
          <button
            type="button"
            onClick={() => setIdx((i) => Math.min(total - 1, i + 1))}
            className="inline-flex items-center gap-2 rounded-xl bg-primary-500 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-primary-600"
          >
            Next →
          </button>
        ) : (
          <Button onClick={() => navigate("/welcome")}>
            Start my free setup →
          </Button>
        )}
      </div>

      {/* Skip + price footer */}
      <div className="mt-8 flex flex-col items-center gap-1.5">
        <button
          type="button"
          onClick={() => navigate("/welcome")}
          className="text-sm font-medium text-zinc-500 underline-offset-4 hover:text-zinc-800 hover:underline"
        >
          Skip the tour — start setup
        </button>
        <p className="text-xs text-zinc-400">
          Start free — 14-day trial · $15/array/month (volume discounts past 50) · $250 one-time setup · cancel anytime
        </p>
        <p className="mt-1 flex items-center gap-1.5 text-xs font-medium text-primary-700">
          <span aria-hidden>🌄</span>
          Born in the Green Mountains · now serving solar operators coast to coast
        </p>
      </div>

      {/* Coverage self-check — let a prospect confirm their utility before they
          commit to setup. Honest 3-state answer straight from /v1/providers. */}
      <UtilitySearch />
    </div>
  );
}
