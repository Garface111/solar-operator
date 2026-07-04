import { useEffect, useState } from "react";
import { Spinner } from "../../ui/Spinner";
import { Button } from "../../ui/Button";
import {
  type PortalAccess,
  type PortalAccessRow,
  getPortalAccess,
  updateClient,
} from "../../lib/api";
import { vaultStashLogin, type VaultSaveResult } from "../../lib/vaultBridge";
import { useExtensionStatus } from "../../lib/useExtensionStatus";
import { timeAgo } from "./utils";

const EXTENSION_INSTALL_URL =
  "https://chromewebstore.google.com/detail/solar-operator-sync/ocohbimolfpnkjcjhiodopjjlhclinpl";

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

// The utilities an operator can enter a fresh login for from the dashboard.
// Each maps a vault CODE (the credential slot key the extension rotates on) to
// the Client identity column that lets the roster match the login to a client.
// GMP has its own column; every SmartHub co-op shares the vec_* pair.
const ENTRY_PROVIDERS: {
  code: string;
  label: string;
  identityField: "gmp_email" | "vec_email";
}[] = [
  { code: "gmp", label: "Green Mountain Power", identityField: "gmp_email" },
  { code: "vec", label: "Vermont Electric Co-op (SmartHub)", identityField: "vec_email" },
  { code: "wec", label: "Washington Electric Co-op (SmartHub)", identityField: "vec_email" },
];

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

const PROVIDER_LABEL: Record<string, string> = {
  gmp: "GMP",
  smarthub: "Co-op",
};

// The vault code to save a credential under for a row that already has a
// provider identity. The roster reports "gmp" or the specific co-op code as the
// provider; a bare "smarthub" (identity present, no saved login yet) defaults
// to vec — the operator can still pick a different co-op via the fresh-entry
// path when there's no identity at all.
function codeForRow(row: PortalAccessRow): string {
  if (row.provider && row.provider !== "smarthub") return row.provider;
  return "vec";
}

/**
 * Inline "save this client's portal login" form. The password is handed to the
 * extension's client-side vault via the bridge and NEVER sent to our backend;
 * when the client has no portal identity on file yet, the typed username is
 * also written to the client record (server-side, no password) so the roster
 * can match the saved login to this client.
 */
function LoginEntry({
  row,
  extensionAbsent,
  onSaved,
}: {
  row: PortalAccessRow;
  extensionAbsent: boolean;
  onSaved: () => void;
}) {
  const hasIdentity = row.status !== "no_portal_identity" && !!row.login_username;
  const [open, setOpen] = useState(false);
  const [providerCode, setProviderCode] = useState(codeForRow(row));
  const [username, setUsername] = useState(row.login_username ?? "");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<VaultSaveResult | "error" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const cta =
    row.status === "failing"
      ? "Update password"
      : hasIdentity
        ? "Save login"
        : "Add login";

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="text-xs font-medium text-sky-600 hover:text-sky-700"
      >
        {cta} →
      </button>
    );
  }

  async function save() {
    const user = username.trim();
    if (!user || !password) {
      setError("Enter the portal username and password.");
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      // Resolve which vault code + client identity column this login belongs to.
      const provider = hasIdentity
        ? { code: codeForRow(row), identityField: (row.provider === "gmp" ? "gmp_email" : "vec_email") as "gmp_email" | "vec_email" }
        : ENTRY_PROVIDERS.find((p) => p.code === providerCode) ?? ENTRY_PROVIDERS[0];
      // If the client has no portal identity yet, record the typed username on
      // the client (server-side, NO password) so the roster links the login.
      if (!hasIdentity) {
        await updateClient(
          row.client_id,
          provider.identityField === "gmp_email"
            ? { gmp_email: user }
            : { vec_email: user },
        );
      }
      const r = await vaultStashLogin(provider.code, user, password);
      setResult(r);
      setPassword("");
      if (r !== "unavailable") {
        // Give the roster a beat to reflect the new identity, then refresh.
        setTimeout(onSaved, 600);
      }
    } catch (e) {
      setResult("error");
      setError(e instanceof Error ? e.message : "Couldn't save the login.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-2 rounded-lg border border-zinc-200 bg-zinc-50 p-3">
      {extensionAbsent && (
        <p className="mb-2 text-xs text-amber-700">
          Install the{" "}
          <a href={EXTENSION_INSTALL_URL} target="_blank" rel="noreferrer" className="underline">
            EnergyAgent extension
          </a>{" "}
          first — logins are stored in it, on your machine, never on our servers.
        </p>
      )}
      <div className="flex flex-col gap-2">
        {!hasIdentity && (
          <select
            value={providerCode}
            onChange={(e) => setProviderCode(e.target.value)}
            className="rounded border border-zinc-300 bg-white px-2 py-1.5 text-sm"
            aria-label="Utility"
          >
            {ENTRY_PROVIDERS.map((p) => (
              <option key={p.code} value={p.code}>{p.label}</option>
            ))}
          </select>
        )}
        <input
          type="text"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          readOnly={hasIdentity}
          placeholder="Portal username / email"
          aria-label="Portal username or email"
          className={
            "rounded border border-zinc-300 px-2 py-1.5 text-sm " +
            (hasIdentity ? "bg-zinc-100 text-zinc-500" : "bg-white")
          }
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Portal password"
          aria-label="Portal password"
          autoComplete="off"
          className="rounded border border-zinc-300 bg-white px-2 py-1.5 text-sm"
        />
        <div className="flex items-center gap-2">
          <Button variant="primary" onClick={save} disabled={busy}>
            {busy ? "Saving…" : "Save to extension"}
          </Button>
          <button
            type="button"
            onClick={() => { setOpen(false); setResult(null); setError(null); setPassword(""); }}
            className="text-xs text-zinc-500 hover:text-zinc-700"
          >
            Cancel
          </button>
        </div>
      </div>
      {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
      {result === "pending" && (
        <p className="mt-2 text-xs text-emerald-700">
          Almost there — open the EnergyAgent extension (puzzle-piece icon → EnergyAgent)
          and click <b>Save</b> to finish. The password stays in your browser.
        </p>
      )}
      {result === "saved" && (
        <p className="mt-2 text-xs text-emerald-700">Saved. This client is now automated.</p>
      )}
      {result === "unavailable" && (
        <p className="mt-2 text-xs text-red-600">
          Couldn&apos;t reach the extension. Make sure EnergyAgent is installed and you&apos;re
          on this site, then try again.
        </p>
      )}
    </div>
  );
}

/**
 * Per-client portal automation roster ("Portal access") for the Master account
 * tab. Shows, for EACH client, whether their utility login is saved in the
 * operator's extension vault (hands-off), failing (password changed), or still
 * to be collected — and lets the operator enter a missing login inline. The
 * password is handed straight to the extension's encrypted client-side vault;
 * it never reaches our servers.
 */
export function PortalAccessCard() {
  const [data, setData] = useState<PortalAccess | null>(null);
  const [failed, setFailed] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const ext = useExtensionStatus();

  useEffect(() => {
    let alive = true;
    getPortalAccess()
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [reloadKey]);

  if (failed) return null; // non-critical card — never block the account page

  const extensionAbsent = ext.status === "absent";
  const reload = () => setReloadKey((k) => k + 1);

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
            Each client&apos;s utility login, saved once for hands-off bill pulls.
            Passwords are stored encrypted in your browser extension — never on our servers.
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
          {extensionAbsent && (
            <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
              The EnergyAgent capture extension isn&apos;t detected in this browser.{" "}
              <a href={EXTENSION_INSTALL_URL} target="_blank" rel="noreferrer" className="underline">
                Install it
              </a>{" "}
              to save client logins and automate bill pulls.
            </p>
          )}
          {!extensionAbsent && !data.extension_alive && (
            <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
              The capture extension hasn&apos;t checked in
              {data.extension_last_seen
                ? ` since ${timeAgo(new Date(data.extension_last_seen))}`
                : " yet"}
              . Statuses below may be stale — open Chrome on the machine that runs it.
            </p>
          )}
          {rows.length === 0 ? (
            <p className="mt-4 text-sm text-zinc-500">
              No clients yet — add clients (Clients tab) to save their portal
              logins here.
            </p>
          ) : (
            <ul className="mt-4 divide-y divide-zinc-100">
              {rows.map((r) => {
                const chip = statusChip(r.status);
                const canEnter = r.status !== "automated";
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
                    {r.last_ok_at && (
                      <div className="mt-0.5 text-xs text-zinc-400">
                        last pull {timeAgo(new Date(r.last_ok_at))}
                      </div>
                    )}
                    {canEnter && (
                      <div className="mt-1">
                        <LoginEntry row={r} extensionAbsent={extensionAbsent} onSaved={reload} />
                      </div>
                    )}
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
