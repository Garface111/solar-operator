import { useEffect, useMemo, useState } from "react";
import { Spinner } from "../../ui/Spinner";
import { Button } from "../../ui/Button";
import { timeAgo } from "./utils";
import {
  getCloudCaptureStatus,
  getProviders,
  setCloudCredential,
  deleteCloudCredential,
  refreshCloudCapture,
  type CloudCredential,
  type ProviderEntry,
} from "../../lib/api";

/**
 * Cloud Capture Credential Vault (NEPOOL) — store a utility login; the
 * server-side harvester signs in and pulls bills around the clock. No
 * extension or open tab required. Passwords write-only, encrypted at rest.
 */
export function CloudCaptureCard({ compact }: { compact?: boolean }) {
  const [loading, setLoading] = useState(true);
  const [creds, setCreds] = useState<CloudCredential[]>([]);
  const [providers, setProviders] = useState<ProviderEntry[]>([]);
  const [provider, setProvider] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const s = await getCloudCaptureStatus();
      setCreds(s.credentials ?? []);
    } catch {
      /* list empty; form still usable */
    }
    setLoading(false);
  };

  useEffect(() => {
    void load();
    getProviders()
      .then((p) => setProviders(p.filter((x) => x.scrape_status === "live")))
      .catch(() => {});
  }, []);

  const selected = useMemo(
    () => providers.find((p) => p.code === provider),
    [providers, provider],
  );

  const save = async () => {
    setMsg(null);
    if (!provider || !username.trim() || !password) {
      setMsg({ kind: "err", text: "Pick a utility and enter the username + password." });
      return;
    }
    if (!consent) {
      setMsg({ kind: "err", text: "Please tick the box to store the password server-side." });
      return;
    }
    setBusy(true);
    try {
      await setCloudCredential({
        provider,
        username: username.trim(),
        password,
        login_host: selected?.smarthub_host || null,
        enable: true,
        consent: true,
      });
      setMsg({ kind: "ok", text: "Saved — our servers will start refreshing this login." });
      setUsername("");
      setPassword("");
      setConsent(false);
      await load();
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : "Couldn't save that login." });
    }
    setBusy(false);
  };

  const remove = async (c: CloudCredential) => {
    setBusy(true);
    try {
      await deleteCloudCredential(c.provider, c.username);
      await load();
    } catch {
      /* reload either way */
    }
    setBusy(false);
  };

  const doRefresh = async () => {
    setRefreshing(true);
    try {
      const r = await refreshCloudCapture();
      setMsg({
        kind: "ok",
        text:
          r.queued > 0
            ? `Queued ${r.queued} login${r.queued === 1 ? "" : "s"} for a fresh pull.`
            : "No enabled logins to refresh yet.",
      });
      await load();
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : "Couldn't refresh." });
    }
    setRefreshing(false);
  };

  /** Honest status: only login_failed blames the password (AO lesson). */
  const statusLine = (c: CloudCredential): { text: string; cls: string } => {
    const fails = c.harvest_fails || 0;
    const st = (c.last_harvest_status || "").toLowerCase();
    if (fails >= 3)
      return { text: "Paused — re-enter the password to retry", cls: "text-amber-700" };
    if (st === "login_failed")
      return { text: "Couldn't sign in — check the password", cls: "text-amber-700" };
    if (st === "scrape_failed")
      return {
        text: "Signed in, data pull hit a snag — retrying",
        cls: "text-amber-700",
      };
    if (c.last_harvest_at && c.last_harvest_ok === false && !st)
      return { text: "Last pull had a snag — retrying", cls: "text-amber-700" };
    if (c.last_harvest_at && c.last_harvest_ok !== false)
      return {
        text: `Connected — refreshed ${timeAgo(new Date(c.last_harvest_at))}`,
        cls: "text-emerald-700",
      };
    if (c.last_harvest_at)
      return {
        text: `Last attempt ${timeAgo(new Date(c.last_harvest_at))}`,
        cls: "text-zinc-500",
      };
    return { text: "Saved — first refresh starting…", cls: "text-zinc-500" };
  };

  const labelFor = (code: string) =>
    providers.find((p) => p.code === code)?.label || code.toUpperCase();

  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-semibold text-zinc-900">Cloud Capture</h2>
          <span className="rounded-full bg-primary-100 px-2 py-0.5 text-[11px] font-semibold text-primary-700">
            Recommended
          </span>
        </div>
        {creds.length > 0 && (
          <button
            type="button"
            onClick={() => void doRefresh()}
            disabled={refreshing || busy}
            className="text-xs font-semibold text-primary-700 hover:underline disabled:opacity-50"
          >
            {refreshing ? "Queuing…" : "Refresh now"}
          </button>
        )}
      </div>
      {!compact && (
        <p className="mb-4 text-sm text-zinc-600">
          Store a utility login and our servers sign in and pull the bills around the
          clock — no browser tab, no extension. Passwords are encrypted at rest and
          never shown again; remove any login anytime.
        </p>
      )}

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-zinc-500">
          <Spinner /> Loading…
        </div>
      ) : (
        <>
          {creds.length > 0 && (
            <ul className="mb-4 divide-y divide-zinc-100 rounded-lg border border-zinc-100">
              {creds.map((c) => {
                const st = statusLine(c);
                return (
                  <li
                    key={`${c.provider}:${c.username}`}
                    className="flex items-center justify-between gap-3 px-3 py-2.5"
                  >
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium text-zinc-900">
                        {labelFor(c.provider)}
                        <span className="ml-2 font-normal text-zinc-500">{c.username}</span>
                      </div>
                      <div className={`text-xs ${st.cls}`}>{st.text}</div>
                    </div>
                    <button
                      type="button"
                      onClick={() => void remove(c)}
                      disabled={busy}
                      className="shrink-0 text-xs font-semibold text-zinc-500 hover:text-red-600 disabled:opacity-50"
                    >
                      Remove
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="space-y-2 rounded-lg bg-zinc-50 p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
              Add a utility login
            </div>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm"
            >
              <option value="">Choose a utility…</option>
              {providers.map((p) => (
                <option key={p.code} value={p.code}>
                  {p.label}
                  {p.state ? ` · ${p.state}` : ""}
                </option>
              ))}
            </select>
            <input
              type="text"
              autoComplete="off"
              placeholder="Portal username / email"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm"
            />
            <input
              type="password"
              autoComplete="new-password"
              placeholder="Portal password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm"
            />
            <label className="flex items-start gap-2 text-xs text-zinc-600">
              <input
                type="checkbox"
                checked={consent}
                onChange={(e) => setConsent(e.target.checked)}
                className="mt-0.5"
              />
              I authorize storing this password, encrypted, on the server so it can refresh my
              utility bills automatically. I can remove it anytime.
            </label>
            {msg && (
              <div className={`text-xs ${msg.kind === "ok" ? "text-emerald-700" : "text-red-600"}`}>
                {msg.text}
              </div>
            )}
            <Button variant="primary" onClick={() => void save()} disabled={busy}>
              {busy ? "Saving…" : "Save login"}
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
