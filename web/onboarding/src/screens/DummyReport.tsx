import { useNavigate } from "react-router-dom";
import { Button } from "../ui/Button";

const BASE = import.meta.env.BASE_URL;

const ARRAY_NAME = "Maple Ridge South";
const NEPOOL_ID = "53984";

// Mirrors what api/writers/gmcs_writer.py actually produces:
//   Row 5 header:  Quarter | Generation (MWh) | Reporting Amount | RECs†
//   Quarter label appears once per 3-month block (subsequent rows blank in col A).
//   "Reporting Amount" duplicates MWh — this is Bruce's GMCS.xlsx convention,
//   NOT a credit/$$ field. Real reports never compute rate-schedule money.
//   RECs = floor(MWh).
// Rolling 6 quarters in the real file; show 3 here for a clean preview.
const QUARTERS = [
  {
    label: "Q3 2025",
    rows: [
      { mwh: 28.541 },
      { mwh: 31.82 },
      { mwh: 24.193 },
    ],
  },
  {
    label: "Q4 2025",
    rows: [
      { mwh: 16.72 },
      { mwh: 9.34 },
      { mwh: 7.081 },
    ],
  },
  {
    label: "Q1 2026",
    rows: [
      { mwh: 8.912 },
      { mwh: 11.46 },
      { mwh: 18.775 },
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
          Mountain Power, Vermont Electric Co-op, and more) — no spreadsheets,
          no manual data entry.
        </p>
      </div>

      {/* Spreadsheet mock — matches gmcs_writer.py output exactly */}
      <div className="overflow-x-auto rounded-2xl border border-zinc-200 bg-white shadow-sm">
        {/* Title row mimics A1:C1 merged header in the real GMCS.xlsx:
            "<Array Name> (<NEPOOL-GIS ID>)" */}
        <div className="border-b border-zinc-200 bg-zinc-50 px-5 py-3">
          <span className="text-sm font-semibold text-zinc-800">
            {ARRAY_NAME} ({NEPOOL_ID})
          </span>
        </div>

        <table className="w-full min-w-[480px] text-sm">
          <thead>
            <tr className="border-b border-zinc-200 bg-zinc-50">
              <th className="px-5 py-3 text-left text-[13px] font-bold text-zinc-700 w-40">Quarter</th>
              <th className="px-5 py-3 text-right text-[13px] font-bold text-zinc-700">Generation (MWh)</th>
              <th className="px-5 py-3 text-right text-[13px] font-bold text-zinc-700">Reporting Amount</th>
              <th className="px-5 py-3 text-right text-[13px] font-bold text-zinc-700">RECs†</th>
            </tr>
          </thead>
          <tbody>
            {QUARTERS.map((q, qi) => (
              <>
                {/* Gap row between quarter blocks, like the real writer */}
                {qi > 0 && (
                  <tr key={`gap${qi}`} aria-hidden>
                    <td colSpan={4} className="h-2 bg-white" />
                  </tr>
                )}
                {q.rows.map((m, mi) => {
                  const recs = Math.floor(m.mwh);
                  return (
                    <tr
                      key={`${qi}-${mi}`}
                      className={[
                        "border-t border-zinc-100",
                        mi % 2 === 1 ? "bg-zinc-50/60" : "bg-white",
                      ].join(" ")}
                    >
                      <td className="px-5 py-2.5 font-semibold text-zinc-800">
                        {mi === 0 ? q.label : ""}
                      </td>
                      <td className="px-5 py-2.5 text-right font-mono text-zinc-700">{fmt(m.mwh)}</td>
                      <td className="px-5 py-2.5 text-right font-mono text-zinc-700">{fmt(m.mwh)}</td>
                      <td className="px-5 py-2.5 text-right font-mono text-zinc-700">{recs}</td>
                    </tr>
                  );
                })}
              </>
            ))}
          </tbody>
        </table>

        {/* Footnote row — VERBATIM from api/writers/gmcs_writer.py FOOTNOTE_TEXT. */}
        <div className="border-t border-zinc-200 bg-zinc-50/60 px-5 py-3 text-[11px] italic leading-relaxed text-zinc-500">
          † NEPOOL-GIS will award 1 REC for every MWH reported. Additionally,
          NEPOOL-GIS will keep track of the decimal MWHs and award an
          additional REC when the total exceeds 1 MWH.
        </div>
      </div>

      <p className="mt-3 text-xs text-zinc-400">
        Real reports show a rolling 6 quarters, one sheet per array.
      </p>

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
        $15/array/month &middot; $250 one-time setup &middot; 14-day free trial &middot; cancel anytime
      </p>
      <p className="mt-3 text-center text-xs">
        <button
          type="button"
          onClick={() => navigate("/intro")}
          className="text-zinc-400 underline underline-offset-2 transition-colors hover:text-zinc-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
        >
          What you&apos;ll get →
        </button>
      </p>
    </div>
  );
}
