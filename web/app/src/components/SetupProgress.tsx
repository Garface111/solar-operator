import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  listClients,
  getNepoolStats,
  getReports,
  setAutoSendAll,
} from "../lib/api";
import { useToast } from "../ui/Toast";

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
  /** `to` navigates; `action` runs the step itself (the last step commits). */
  cta: { label: string; to?: string; action?: "auto_send_all" } | null;
  state: StepState;
}

const DISMISS_KEY = "so:genrep:setup-dismissed";

export function SetupProgress() {
  const navigate = useNavigate();
  const toast = useToast();
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [turningOn, setTurningOn] = useState(false);
  const [reload, setReload] = useState(0);

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

      const live = clientsR.status === "fulfilled"
        ? clientsR.value.filter((c) => c.active)
        : [];
      const activeClients = live.length;
      const missingNepool =
        statsR.status === "fulfilled" ? statsR.value.arrays_missing_nepool : 0;
      const anySent =
        reportsR.status === "fulfilled" &&
        reportsR.value.some((r) => r.status === "sent");
      // The commit state: is auto-send on for EVERY client (= account is live)?
      const onCount = live.filter((c) => c.auto_send).length;
      const allOn = activeClients > 0 && onCount === activeClients;
      // What turning it on will actually cost — the same arithmetic the confirm
      // shows, so the operator never sees a number they weren't quoted.
      const arrays = live.reduce((n, c) => n + (c.array_count || 0), 0);
      const quarterly = arrays * 15;

      const s1: StepState = activeClients > 0 ? "done" : "current";
      // Step 2 is "current" only once there's at least one client to attribute.
      const s2: StepState =
        activeClients === 0 ? "todo" : missingNepool === 0 ? "done" : "current";
      // Step 3 is the COMMIT: auto-send on for everyone. Sending already counts
      // as committed (a manual send is the same decision, one client at a time).
      const s3: StepState = allOn || anySent
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
          label: "Turn on auto-send",
          hint: allOn
            ? `Auto-send is on for all ${activeClients} client${activeClients === 1 ? "" : "s"} — reports ship automatically`
            : anySent
              ? "Reports are shipping"
              : s3 === "current"
                ? `Ship every client's report automatically. $15 per array, once a quarter — ${arrays} array${arrays === 1 ? "" : "s"} ≈ $${quarterly}/quarter.`
                : "Ship every client's report automatically each quarter",
          cta:
            s3 === "current"
              ? {
                  label: `Enable auto-send for all ${activeClients} client${activeClients === 1 ? "" : "s"}`,
                  action: "auto_send_all",
                }
              : null,
          state: s3,
        },
      ];
      setSteps(built);
    })();
    return () => {
      cancelled = true;
    };
  }, [reload]);

  /** The commit: auto-send for every client. This STARTS BILLING, so quote the
   *  exact cost and get an explicit yes first — never charge on a bare click. */
  const enableAll = useCallback(async () => {
    if (turningOn) return;
    let live: Awaited<ReturnType<typeof listClients>> = [];
    try {
      live = (await listClients()).filter((c) => c.active);
    } catch {
      /* fall through — the server response still reports the real numbers */
    }
    const n = live.length;
    const arrays = live.reduce((a, c) => a + (c.array_count || 0), 0);
    const ok = window.confirm(
      `Turn on auto-send for all ${n} client${n === 1 ? "" : "s"}?\n\n` +
        `Each client's report will ship automatically every period.\n\n` +
        `Billing: $15 per array, once per quarter — charged the first time an ` +
        `array is actually reported.\n` +
        `${arrays} array${arrays === 1 ? "" : "s"} ≈ $${arrays * 15} per quarter.\n\n` +
        `Building and previewing stay free. You can switch any client back off.`,
    );
    if (!ok) return;
    setTurningOn(true);
    try {
      const r = await setAutoSendAll(true);
      toast.success(
        `Auto-send on for ${r.clients} client${r.clients === 1 ? "" : "s"} — ` +
          `${r.arrays} array${r.arrays === 1 ? "" : "s"} ≈ $${Math.round(
            (r.estimated_quarterly_cents || 0) / 100,
          )}/quarter, billed as each is reported.`,
      );
      setReload((k) => k + 1);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't turn on auto-send");
    } finally {
      setTurningOn(false);
    }
  }, [turningOn, toast]);

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
                  disabled={s.cta.action === "auto_send_all" && turningOn}
                  onClick={() =>
                    s.cta!.action === "auto_send_all"
                      ? enableAll()
                      : navigate(s.cta!.to!)
                  }
                  className="rounded-lg bg-primary-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-primary-700 disabled:opacity-60"
                >
                  {s.cta.action === "auto_send_all" && turningOn
                    ? "Turning on…"
                    : s.cta.label}{" "}
                  →
                </button>
              </div>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
