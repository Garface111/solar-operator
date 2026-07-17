import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  listClients,
  getNepoolStats,
  getReports,
} from "../lib/api";

/**
 * SetupProgress — the onboarding spine for Generation reports (Ford 2026-07-16).
 *
 * A new operator lands with the pieces (add-client, NEPOOL-ID nudge, delivery
 * settings) but no sense of the sequence. This slim, data-driven strip shows
 * where they are: Add clients → Assign NEPOOL IDs → Review & send. It computes
 * each step from real state and REMOVES ITSELF the moment setup is complete (or
 * the operator dismisses it), so an established operator never sees it. No
 * heavy wizard — just a spine that answers "what's next?".
 */
type StepState = "done" | "current" | "todo";

interface Step {
  key: string;
  label: string;
  hint: string;
  cta: { label: string; to: string } | null;
  state: StepState;
}

const DISMISS_KEY = "so:genrep:setup-dismissed";

export function SetupProgress() {
  const navigate = useNavigate();
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    try {
      if (localStorage.getItem(DISMISS_KEY) === "1") setDismissed(true);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [clientsR, statsR, reportsR] = await Promise.allSettled([
        listClients(),
        getNepoolStats(),
        getReports(4),
      ]);
      if (cancelled) return;

      const activeClients =
        clientsR.status === "fulfilled"
          ? clientsR.value.filter((c) => c.active).length
          : 0;
      const missingNepool =
        statsR.status === "fulfilled" ? statsR.value.arrays_missing_nepool : 0;
      const anySent =
        reportsR.status === "fulfilled" &&
        reportsR.value.some((r) => r.status === "sent");

      const s1: StepState = activeClients > 0 ? "done" : "current";
      // Step 2 is "current" only once there's at least one client to attribute.
      const s2: StepState =
        activeClients === 0 ? "todo" : missingNepool === 0 ? "done" : "current";
      const s3: StepState = anySent
        ? "done"
        : s1 === "done" && s2 === "done"
          ? "current"
          : "todo";

      const built: Step[] = [
        {
          key: "clients",
          label: "Add your clients",
          hint:
            activeClients > 0
              ? `${activeClients} client${activeClients === 1 ? "" : "s"} connected`
              : "Connect a utility login for each client",
          cta: s1 !== "done" ? { label: "Add a client", to: "/clients" } : null,
          state: s1,
        },
        {
          key: "nepool",
          label: "Assign NEPOOL-GIS IDs",
          hint:
            s2 === "done"
              ? "Every array is attributed"
              : s2 === "current"
                ? `${missingNepool} array${missingNepool === 1 ? "" : "s"} still need an ID`
                : "Each array needs its REC-market ID",
          cta:
            s2 === "current" ? { label: "Assign IDs", to: "/clients" } : null,
          state: s2,
        },
        {
          key: "send",
          label: "Review & send",
          hint: anySent
            ? "Reports are shipping"
            : "Set cadence & recipients, then send a test to yourself",
          cta:
            s3 === "current"
              ? { label: "Open Reports", to: "/reports" }
              : null,
          state: s3,
        },
      ];
      setSteps(built);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Hide until we know the state, once everything is done, or on dismiss.
  if (dismissed || steps === null) return null;
  const allDone = steps.every((s) => s.state === "done");
  if (allDone) return null;

  const doneCount = steps.filter((s) => s.state === "done").length;

  function dismiss() {
    setDismissed(true);
    try {
      localStorage.setItem(DISMISS_KEY, "1");
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="mb-4 rounded-2xl border border-cream-border bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div>
          <p className="text-sm font-semibold text-zinc-900">
            Set up generation reports
          </p>
          <p className="text-xs text-zinc-500">
            {doneCount} of {steps.length} done — quarterly NEPOOL-GIS workbooks
            and a report email to each client.
          </p>
        </div>
        <button
          type="button"
          onClick={dismiss}
          className="shrink-0 rounded-md px-2 py-1 text-xs font-medium text-zinc-400 hover:bg-zinc-100 hover:text-zinc-600"
        >
          Dismiss
        </button>
      </div>

      <ol className="grid gap-2 sm:grid-cols-3">
        {steps.map((s, i) => (
          <li
            key={s.key}
            className={[
              "flex flex-col gap-1 rounded-xl border p-3",
              s.state === "current"
                ? "border-primary-300 bg-primary-50"
                : "border-cream-border bg-white",
            ].join(" ")}
          >
            <div className="flex items-center gap-2">
              <span
                className={[
                  "flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold",
                  s.state === "done"
                    ? "bg-primary-600 text-white"
                    : s.state === "current"
                      ? "bg-primary-100 text-primary-700"
                      : "bg-zinc-100 text-zinc-400",
                ].join(" ")}
              >
                {s.state === "done" ? "✓" : i + 1}
              </span>
              <span
                className={[
                  "text-sm font-semibold",
                  s.state === "todo" ? "text-zinc-400" : "text-zinc-900",
                ].join(" ")}
              >
                {s.label}
              </span>
            </div>
            <p className="pl-7 text-xs text-zinc-500">{s.hint}</p>
            {s.cta && (
              <div className="pl-7 pt-0.5">
                <button
                  type="button"
                  onClick={() => navigate(s.cta!.to)}
                  className="rounded-lg bg-primary-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-primary-700"
                >
                  {s.cta.label} →
                </button>
              </div>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
