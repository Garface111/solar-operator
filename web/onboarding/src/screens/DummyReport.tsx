import { useNavigate } from "react-router-dom";
import { Button } from "../ui/Button";

const BASE = import.meta.env.BASE_URL;

const ARRAY_NAME = "Maple Ridge South";
const NEPOOL_ID = "53984";

const QUARTERS = [
  {
    label: "Q3 2025 (Jul – Sep)",
    months: [
      { month: "July 2025",      mwh: 28.541, recs: 28, credit: 2427.49 },
      { month: "August 2025",    mwh: 31.82,  recs: 31, credit: 2704.70 },
      { month: "September 2025", mwh: 24.193, recs: 24, credit: 2056.41 },
    ],
  },
  {
    label: "Q4 2025 (Oct – Dec)",
    months: [
      { month: "October 2025",  mwh: 16.72, recs: 16, credit: 1421.20 },
      { month: "November 2025", mwh: 9.34,  recs: 9,  credit: 794.05  },
      { month: "December 2025", mwh: 7.081, recs: 7,  credit: 601.89  },
    ],
  },
  {
    label: "Q1 2026 (Jan – Mar)",
    months: [
      { month: "January 2026",  mwh: 8.912,  recs: 8,  credit: 757.52  },
      { month: "February 2026", mwh: 11.46,  recs: 11, credit: 974.10  },
      { month: "March 2026",    mwh: 18.775, recs: 18, credit: 1595.89 },
    ],
  },
];

function fmt(n: number) {
  return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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
          This NEPOOL-format Excel report is what your clients receive every quarter.
          We build it automatically from your utility bills (Green Mountain
          Power, Vermont Electric Coop, and more) — no
          spreadsheets, no manual data entry.
        </p>
      </div>

      {/* Spreadsheet mock */}
      <div className="overflow-x-auto rounded-2xl border border-zinc-200 bg-white shadow-sm">
        {/* Title row mimics A1:C1 merged header in the real GMCS.xlsx */}
        <div className="border-b border-zinc-200 bg-zinc-50 px-5 py-3">
          <span className="text-sm font-semibold text-zinc-800">
            {ARRAY_NAME} ({NEPOOL_ID})
          </span>
          <span className="ml-3 text-xs text-zinc-400">GMCS Net-Metering Credit Report</span>
        </div>

        <table className="w-full min-w-[520px] text-sm">
          <thead>
            <tr className="border-b border-zinc-200 bg-zinc-50">
              <th className="px-5 py-3 text-left text-[13px] font-bold text-zinc-700 w-44">Month</th>
              <th className="px-5 py-3 text-right text-[13px] font-bold text-zinc-700">MWh</th>
              <th className="px-5 py-3 text-right text-[13px] font-bold text-zinc-700">RECs</th>
              <th className="px-5 py-3 text-right text-[13px] font-bold text-zinc-700">Credit (est.)</th>
            </tr>
          </thead>
          <tbody>
            {QUARTERS.map((q, qi) => (
              <>
                <tr key={`q${qi}`}>
                  <td
                    colSpan={4}
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
                    <td className="px-5 py-2.5 text-right font-mono text-zinc-700">{m.mwh}</td>
                    <td className="px-5 py-2.5 text-right font-mono text-zinc-700">{m.recs}</td>
                    <td className="px-5 py-2.5 text-right font-mono font-medium text-primary-700">
                      ${fmt(m.credit)}
                    </td>
                  </tr>
                ))}
              </>
            ))}
          </tbody>
        </table>

        {/* Footnote row mimics the GMCS footnote pinned below data */}
        <div className="border-t border-zinc-200 bg-zinc-50/60 px-5 py-3 text-[11px] leading-relaxed text-zinc-400">
          Net-metering credits are estimates calculated per VT PSB Rule 5.100 and GMP Schedule NM-2.
          Data auto-pulled from your utility (Green Mountain Power, Vermont Electric Coop) and cross-checked against NEPOOL-GIS.
          RECs = integer floor of monthly MWh generation.
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
