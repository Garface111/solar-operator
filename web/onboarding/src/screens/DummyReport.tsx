import { useNavigate } from "react-router-dom";
import { Button } from "../ui/Button";

const BASE = import.meta.env.BASE_URL;

const ARRAY_NAME = "Maple Ridge South";
const NEPOOL_ID = "53984";

// Mirrors what api/writers/gmcs_writer.py actually produces: one sheet per
// array, rolling 6 quarters of monthly MWh + REC counts (REC = int(MWh)),
// with the verbatim FOOTNOTE_TEXT from the writer pinned below the data.
// No credit/dollar column — real reports don't compute rate-schedule money.
const QUARTERS = [
  {
    label: "Q3 2025 (Jul – Sep)",
    months: [
      { month: "July 2025",      mwh: 28.541, recs: 28 },
      { month: "August 2025",    mwh: 31.82,  recs: 31 },
      { month: "September 2025", mwh: 24.193, recs: 24 },
    ],
  },
  {
    label: "Q4 2025 (Oct – Dec)",
    months: [
      { month: "October 2025",  mwh: 16.72, recs: 16 },
      { month: "November 2025", mwh: 9.34,  recs: 9  },
      { month: "December 2025", mwh: 7.081, recs: 7  },
    ],
  },
  {
    label: "Q1 2026 (Jan – Mar)",
    months: [
      { month: "January 2026",  mwh: 8.912,  recs: 8  },
      { month: "February 2026", mwh: 11.46,  recs: 11 },
      { month: "March 2026",    mwh: 18.775, recs: 18 },
    ],
  },
];

function fmt(n: number) {
  // Mirror Excel "General" format: trims trailing zeros (25.720 → 25.72)
  return n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 3 });
}

export default function DummyReport() {
  const navigate = useNavigate();

  return (
    <div className="mx-auto min-h-dvh max-w-2xl px-4 py-10 sm:py-14">
      {/* Header */}
      <div className="mb-6">
        <span className="inline-flex items-center rounded-full border border-primary-200 bg-primary-50 px-3 py-1 text-xs font-medium text-primary-700">
          Sample — not your real data
        </span>
        <h1 className="mt-3 text-3xl font-semibold tracking-tight text-zinc-900">
          Here&apos;s what a finished quarterly report looks like.
        </h1>
        <p className="mt-2 text-base leading-relaxed text-zinc-500">
          This NEPOOL-format Excel report is what your clients receive every
          quarter. We build it automatically from your utility bills (Green
          Mountain Power, Vermont Electric Coop, and more) — no spreadsheets,
          no manual data entry.
        </p>
      </div>

      {/* Spreadsheet mock */}
      <div className="overflow-x-auto rounded-2xl border border-zinc-200 bg-white shadow-sm">
        {/* Title row mimics A1:C1 merged header in the real GMCS.xlsx:
            "<Array Name> (<NEPOOL-GIS ID>)" */}
        <div className="border-b border-zinc-200 bg-zinc-50 px-5 py-3">
          <span className="text-sm font-semibold text-zinc-800">
            {ARRAY_NAME} ({NEPOOL_ID})
          </span>
        </div>

        <table className="w-full min-w-[420px] text-sm">
          <thead>
            <tr className="border-b border-zinc-200 bg-zinc-50">
              <th className="px-5 py-3 text-left text-[13px] font-bold text-zinc-700 w-56">Month</th>
              <th className="px-5 py-3 text-right text-[13px] font-bold text-zinc-700">MWh</th>
              <th className="px-5 py-3 text-right text-[13px] font-bold text-zinc-700">RECs</th>
            </tr>
          </thead>
          <tbody>
            {QUARTERS.map((q, qi) => (
              <>
                <tr key={`q${qi}`}>
                  <td
                    colSpan={3}
                    className="border-t-2 border-primary-100 bg-primary-50 px-5 py-1.5 text-xs font-semibold text-primary-700"
                  >
                    {q.label}
                  </td>
                </tr>
                {q.months.map((m, mi) => (
                  <tr
                    key={`${qi}-${mi}`}
                    className={[
                      "border-t border-zinc-100",
                      mi % 2 === 1 ? "bg-zinc-50/60" : "bg-white",
                    ].join(" ")}
                  >
                    <td className="px-5 py-2.5 text-zinc-700">{m.month}</td>
                    <td className="px-5 py-2.5 text-right font-mono text-zinc-700">{fmt(m.mwh)}</td>
                    <td className="px-5 py-2.5 text-right font-mono text-zinc-700">{m.recs}</td>
                  </tr>
                ))}
              </>
            ))}
          </tbody>
        </table>

        {/* Footnote row — VERBATIM from api/writers/gmcs_writer.py FOOTNOTE_TEXT.
            Bruce flagged that fabricated "credit estimates" misrepresent what
            real reports contain; this matches the real writer's output. */}
        <div className="border-t border-zinc-200 bg-zinc-50/60 px-5 py-3 text-[11px] italic leading-relaxed text-zinc-500">
          † NEPOOL-GIS will award 1 REC for every MWH reported. Additionally,
          NEPOOL-GIS will keep track of the decimal MWHs and award an
          additional REC when the total exceeds 1 MWH.
        </div>
      </div>

      {/* Download + CTA */}
      <div className="mt-8 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <a
          href={`${BASE}sample.xlsx`}
          download
          className="text-sm font-medium text-primary-600 underline underline-offset-2 hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
        >
          Download sample Excel file →
        </a>
        <Button onClick={() => navigate("/welcome")}>
          Start my free setup →
        </Button>
      </div>

      <p className="mt-6 text-center text-xs text-zinc-400">
        $250 one-time setup &middot; $45 / array / month &middot; cancel anytime
      </p>

      {/* Back */}
      <div className="mt-4 flex justify-center">
        <button
          type="button"
          onClick={() => navigate("/")}
          className="text-xs text-zinc-400 underline underline-offset-2 hover:text-zinc-600 focus:outline-none"
        >
          ← Back to intro
        </button>
      </div>
    </div>
  );
}
