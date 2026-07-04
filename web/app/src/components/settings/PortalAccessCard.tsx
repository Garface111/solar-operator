import { useEffect, useState } from "react";
import { Spinner } from "../../ui/Spinner";
import {
  type PortalAccess,
  type PortalAccessRow,
  getPortalAccess,
} from "../../lib/api";
import { timeAgo } from "./utils";

// Roster states, ordered most-actionable first so the operator's TODO floats
// to the top of the list.
const STATUS_ORDER: Record<string, number> = {
  failing: 0,
  no_portal_identity: 1,
  login_missing: 2,
  disabled: 3,
  saved_pending: 4,
  automated: 5,
};

function statusChip(status: string): { label: string; cls: string } {
  switch (status) {
    case "automated":
      return { label: "● automated", cls: "bg-emerald-50 text-emerald-600" };
    case "saved_pending":
      return { label: "saved — first pull pending", cls: "bg-sky-50 text-sky-700" };
    case "failing":
      return { label: "failing — password changed?", cls: "bg-red-50 text-red-600" };
    case "disabled":
      return { label: "auto-login off", cls: "bg-zinc-100 text-zinc-500" };
    case "login_missing":
      return { label: "login not saved", cls: "bg-amber-50 text-amber-700" };
    case "no_portal_identity":
      return { label: "no portal login on file", cls: "bg-amber-50 text-amber-700" };
    default:
      return { label: status, cls: "bg-zinc-100 text-zinc-500" };
  }
}

// What the operator should DO about a row — the card is a checklist, not a
// status wall. Only non-green rows get an action line.
function actionFor(row: PortalAccessRow): string | null {
  switch (row.status) {
    case "failing":
      return "Re-save this login's password in the extension (EnergyAgent → Utility logins).";
    case "login_missing":
      return `Save ${row.login_username}'s portal password in the extension to automate this client.`;
    case "no_portal_identity":
      return "Add this client's portal email/username (Clients tab) so captures can match them.";
    case "disabled":
      return "Auto-login is switched off for this login in the extension popup.";
    default:
      return null;
  }
}

const PROVIDER_LABEL: Record<string, string> = {
  gmp: "GMP",
  smarthub: "Co-op",
};

/**
 * Per-client portal automation roster ("Portal access") for the Master account
 * tab. Answers, for EACH client: is their utility login saved in the operator's
 * extension vault (fully hands-off), failing (password changed), or still to be
 * collected? Passwords never appear here — they live encrypted in the extension
 * on the operator's machine; this card reads status metadata the extension
 * heartbeat reports (usernames + health only).
 */
export function PortalAccessCard() {
  const [data, setData] = useState<PortalAccess | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    getPortalAccess()
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, []);

  if (failed) return null; // non-critical card — never block the account page

  const rows = (data?.clients ?? [])
    .slice()
    .sort(
      (a, b) =>
        (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) ||
        a.client.localeCompare(b.client),
    );
  const todo = rows.filter((r) => r.status !== "automated" && r.status !== "saved_pending").length;

  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm">
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-zinc-900">Portal access</h2>
          <p className="mt-0.5 text-sm text-zinc-500">
            Which client portal logins are automated. Passwords stay encrypted in
            your browser extension — never on our servers.
          </p>
        </div>
        {data && (
          <span
            className={
              "shrink-0 rounded-full px-2 py-0.5 text-xs font-medium " +
              (todo === 0
                ? "bg-emerald-50 text-emerald-600"
                : "bg-amber-50 text-amber-700")
            }
          >
            {todo === 0 ? "all set" : `${todo} to fix`}
          </span>
        )}
      </div>

      {!data ? (
        <div className="flex justify-center py-8">
          <Spinner className="h-5 w-5" />
        </div>
      ) : (
        <>
          {!data.extension_alive && (
            <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
              The capture extension hasn&apos;t checked in
              {data.extension_last_seen
                ? ` since ${timeAgo(new Date(data.extension_last_seen))}`
                : " yet"}
              . Statuses below may be stale — open Chrome on the machine that
              runs it.
            </p>
          )}
          {rows.length === 0 ? (
            <p className="mt-4 text-sm text-zinc-500">
              No clients yet — add clients and their portal logins to see
              automation status here.
            </p>
          ) : (
            <ul className="mt-4 divide-y divide-zinc-100">
              {rows.map((r) => {
                const chip = statusChip(r.status);
                const action = actionFor(r);
                return (
                  <li key={`${r.client_id}-${r.provider}-${r.login_username}`} className="py-2.5">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className="text-sm font-medium text-zinc-900">{r.client}</span>
                      {r.provider && (
                        <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[11px] font-medium text-zinc-500">
                          {PROVIDER_LABEL[r.provider] ?? r.provider.toUpperCase()}
                        </span>
                      )}
                      {r.login_username && (
                        <span className="text-xs text-zinc-500">{r.login_username}</span>
                      )}
                      <span className={`ml-auto rounded-full px-2 py-0.5 text-xs font-medium ${chip.cls}`}>
                        {chip.label}
                      </span>
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-xs text-zinc-400">
                      {r.last_ok_at && <span>last pull {timeAgo(new Date(r.last_ok_at))}</span>}
                      {action && <span className="text-amber-700">{action}</span>}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
          {data.unassigned_logins.length > 0 && (
            <div className="mt-4 rounded-lg bg-zinc-50 px-3 py-2">
              <p className="text-xs font-medium text-zinc-600">
                Saved logins not linked to a client
              </p>
              <p className="mt-0.5 text-xs text-zinc-500">
                {data.unassigned_logins
                  .map((u) => `${u.username} (${PROVIDER_LABEL[u.provider] ?? u.provider.toUpperCase()})`)
                  .join(", ")}{" "}
                — set the matching client&apos;s portal email/username so captures
                file under them.
              </p>
            </div>
          )}
        </>
      )}
    </section>
  );
}
