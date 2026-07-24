import { useEffect, useState, useMemo, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { useToast } from "../ui/Toast";
import {
  requestUtilityAddition,
  getProviders,
  getPortalAccess,
  listDiscoveryCandidates,
  getInverterVendors,
  createClient,
  setCloudCredential,
  discoverLocus,
  connectLocusAccount,
  discoverAlsoEnergy,
  connectAlsoEnergyAccount,
  discoverSolarEdge,
  connectSolarEdgeAccount,
  type InverterVendorEntry,
  type PortalAccessUnassigned,
} from "../lib/api";
import {
  useExtensionStatus,
  type ExtensionStatus,
} from "../lib/useExtensionStatus";
import { wipeCookiesAndWait, gmpPortalUrl } from "../lib/openPortalTab";
import { milesToState } from "../lib/stateGeo";

// A pickable SmartHub portal, derived at runtime from the provider catalog
// (GET /v1/providers → rows with a smarthub_host). The hardcoded list is gone:
// new utilities added to api/data/providers/*.csv appear here automatically.
interface SmartHubEntry {
  provider: string;
  name: string;
  hint: string;
  /** Two-letter state code, used to rank by distance when the operator
   *  shares their location. Empty for utilities with no state on file. */
  stateCode: string;
  url: string;
  /** SmartHub subdomain host — the login_host Cloud Capture needs to open the
   *  right co-op portal. Empty for GMP (fixed login URL in the harvester). */
  host: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  /** Reload the clients list from the server. Called as a fire-and-forget
   *  hook so the parent can refresh — the modal itself no longer cares
   *  about confirmation (captures land ambiently via a global listener
   *  mounted in ClientsSection). */
  onCaptured: () => Promise<{ id: number; name: string }[]>;
  /** Switch into the legacy manual form for the rare case someone wants
   *  to add a placeholder without ever touching a portal. */
  onSwitchToManual: () => void;
  /** When true (Store it with us / cloud), hide extension install nags —
   *  bills come from Auto-refresh, not the Chrome helper. */
  cloudMode?: boolean;
}

const GMP_FRIENDLY = "Green Mountain Power";

// Utility logins we can turn straight into a client (createClient carries a
// {gmp,vec}_username + autopopulate that attaches the login's captured bills to
// the new client). Inverter logins (fronius/chint) and co-op logins aren't
// utility-bill sources for a NEPOOL client, so they're not offered here —
// they're managed in the credential vault.
const ATTACHABLE: Record<string, { label: string; field: "gmp" | "vec" }> = {
  gmp: { label: "Green Mountain Power", field: "gmp" },
  vec: { label: "Vermont Electric Coop", field: "vec" },
};

/** Prettify a login into a default client name (four.general → "Four General"). */
function nameFromLogin(login: string): string {
  const s = (login || "").trim();
  const local = s.includes("@") ? s.split("@")[0] : s;
  const cleaned = local.replace(/[._-]+/g, " ").trim();
  return (
    cleaned.replace(/\b\w/g, (ch) => ch.toUpperCase()).slice(0, 120) ||
    s.slice(0, 120) ||
    "New client"
  );
}

// ─── Section 1: the logins already feeding generation reports ───────────────

/**
 * One row in the top section. Merged from two sources that overlap:
 *   • GET /v1/account/discovery/candidates → every saved login we actively
 *     read sites from, with its health and its curation counts.
 *   • GET /v1/portal-access → unassigned_logins, i.e. saved utility logins no
 *     client has claimed yet (these may not be in the pool at all).
 * A login in both sources collapses into one row (provider+username, case
 * insensitive), keeping the pool's counts AND the unassigned action.
 */
interface ConnectedLogin {
  key: string;
  provider: string;
  label: string;
  username: string;
  kind: "vendor" | "utility";
  lastError: string | null;
  counts: { new: number; imported: number; ignored: number } | null;
  /** Set when this is a saved utility login no client claims yet — the one
   *  row that can become a client without re-entering anything. */
  unassigned: PortalAccessUnassigned | null;
}

function mergeKey(provider: string, username: string): string {
  return `${provider.trim().toLowerCase()}:${username.trim().toLowerCase()}`;
}

/**
 * ConnectedLoginsList — "Logins feeding your generation reports". Every login
 * we already hold, utility and monitoring alike, in one list with a plain
 * status line. This replaces the old LinkedLoginsPicker, which only showed
 * unassigned utility logins and hid the monitoring ones entirely.
 */
function ConnectedLoginsList({
  onCreated,
  onReview,
}: {
  onCreated: () => void;
  onReview: () => void;
}) {
  const toast = useToast();
  const [rows, setRows] = useState<ConnectedLogin[] | null>(null);
  const [openFor, setOpenFor] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Either source failing is non-fatal — show whatever we could load.
      const [pool, access] = await Promise.all([
        listDiscoveryCandidates().catch(() => null),
        getPortalAccess().catch(() => null),
      ]);
      if (cancelled) return;

      const byKey = new Map<string, ConnectedLogin>();
      for (const g of pool?.logins ?? []) {
        byKey.set(mergeKey(g.provider, g.login), {
          key: mergeKey(g.provider, g.login),
          provider: g.provider,
          label: g.provider_label,
          username: g.login,
          kind: g.source_kind,
          lastError: g.last_error,
          counts: g.counts,
          unassigned: null,
        });
      }
      for (const l of access?.unassigned_logins ?? []) {
        const k = mergeKey(l.provider, l.username);
        const existing = byKey.get(k);
        if (existing) {
          existing.unassigned = l;
        } else {
          byKey.set(k, {
            key: k,
            provider: l.provider,
            label: ATTACHABLE[l.provider]?.label || l.provider.toUpperCase(),
            username: l.username,
            kind: "utility",
            lastError: null,
            counts: null,
            unassigned: l,
          });
        }
      }
      setRows([...byKey.values()]);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function create(row: ConnectedLogin) {
    const login = row.unassigned;
    if (!login) return;
    const nm = name.trim();
    if (!nm) {
      toast.show("Name the client first.", "info");
      return;
    }
    setCreating(true);
    try {
      const input =
        login.provider === "gmp"
          ? { name: nm, gmp_username: login.username, gmp_autopopulate: true }
          : { name: nm, vec_username: login.username, vec_autopopulate: true };
      const created = await createClient(input);
      // Honest copy (Ford 2026-07-16): creating a client from an already-linked
      // login does NOT synchronously pull bills — the arrays attach when that
      // login next syncs (the /v1/sync autopop matches this client by its
      // username and fills it in). Only claim data has arrived if the server
      // actually returned arrays; otherwise say plainly what happens next.
      const n = created?.array_count || 0;
      toast.success(
        n > 0
          ? `Added ${nm} — ${n} array${n === 1 ? "" : "s"} attached.`
          : `Added ${nm}. Its arrays will fill in automatically the next time ${ATTACHABLE[login.provider].label} syncs this login — no need to re-enter anything.`,
      );
      setOpenFor(null);
      setName("");
      onCreated();
    } catch (e) {
      toast.show(
        e instanceof Error ? e.message : "Couldn't create that client.",
        "error",
      );
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-3">
      <div>
        <p className="text-sm font-semibold text-zinc-900">
          Logins feeding your generation reports
        </p>
        <p className="mt-0.5 text-xs text-zinc-500">
          Every utility and monitoring login we hold, and what it&apos;s
          bringing in.
        </p>
      </div>

      {rows === null ? (
        <div className="rounded-xl border border-cream-border px-4 py-3 text-sm text-zinc-500">
          Loading your logins&hellip;
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-xl border border-dashed border-cream-border px-4 py-3 text-sm text-zinc-600">
          No logins connected yet — add one below.
        </div>
      ) : (
        <ul className="space-y-2">
          {rows.map((r) => {
            const attachable = !!r.unassigned && !!ATTACHABLE[r.provider];
            const isOpen = openFor === r.key;
            const newCount = r.counts?.new ?? 0;
            return (
              <li
                key={r.key}
                className="rounded-xl border border-cream-border bg-white p-3 shadow-sm"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-zinc-900">
                      {r.label}
                    </p>
                    <p className="truncate text-xs text-zinc-500">
                      {r.username}
                    </p>
                    <LoginStatusLine row={r} />
                  </div>
                  {attachable && !isOpen && (
                    <Button
                      onClick={() => {
                        setOpenFor(r.key);
                        setName(nameFromLogin(r.username));
                      }}
                      className="shrink-0 px-3 py-1.5 text-xs"
                    >
                      Use this login →
                    </Button>
                  )}
                </div>

                {newCount > 0 && (
                  <button
                    type="button"
                    onClick={onReview}
                    className="mt-2 text-xs font-medium text-primary-600 hover:underline"
                  >
                    Review {newCount} site{newCount === 1 ? "" : "s"} →
                  </button>
                )}

                {isOpen && (
                  <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                    <input
                      autoFocus
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void create(r);
                      }}
                      placeholder="Client name (e.g. Town of Glover)"
                      className="min-w-0 flex-1 rounded-lg border border-cream-border px-3 py-2 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-primary-400 focus:outline-none focus:ring-2 focus:ring-primary-500/30"
                    />
                    <div className="flex gap-2">
                      <Button
                        onClick={() => void create(r)}
                        disabled={creating || !name.trim()}
                        className="px-3 py-2 text-sm"
                      >
                        {creating ? "Creating…" : "Create client"}
                      </Button>
                      <Button
                        variant="ghost"
                        onClick={() => setOpenFor(null)}
                        className="px-3 py-2 text-sm"
                      >
                        Cancel
                      </Button>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** The one plain sentence under a login: what it's actually done for you.
 *  A last_error wins the row's attention — that's how a stale password
 *  surfaces without needing its own banner. */
function LoginStatusLine({ row }: { row: ConnectedLogin }) {
  if (row.lastError) {
    return (
      <p className="mt-0.5 text-xs font-medium text-amber-700">
        Needs attention — {row.lastError}
      </p>
    );
  }
  const imported = row.counts?.imported ?? 0;
  const fresh = row.counts?.new ?? 0;
  if (imported > 0) {
    return (
      <p className="mt-0.5 text-xs text-zinc-500">
        {imported} array{imported === 1 ? "" : "s"} in your system
        {fresh > 0 ? ` · ${fresh} new to review` : ""}
      </p>
    );
  }
  if (fresh > 0) {
    return (
      <p className="mt-0.5 text-xs text-zinc-500">
        {fresh} site{fresh === 1 ? "" : "s"} found — none added yet
      </p>
    );
  }
  if (row.unassigned) {
    return (
      <p className="mt-0.5 text-xs text-zinc-500">Not linked to a client yet</p>
    );
  }
  return null;
}

// ─── Section 2: one search, then one form ───────────────────────────────────

/**
 * One searchable thing you can log into — a utility portal or a monitoring
 * vendor. The two catalogs (GET /v1/providers and
 * GET /v1/array-owners/inverter-vendors) merge into this single shape so the
 * operator searches once instead of choosing a category first.
 */
interface CatalogEntry {
  key: string;
  kind: "utility" | "monitoring";
  code: string;
  label: string;
  /** Small trailing note in the row — state for a utility, blank otherwise. */
  hint: string;
  /** Utility only. */
  utility?: SmartHubEntry;
  /** Monitoring only. */
  vendor?: InverterVendorEntry;
  /** Distance to the operator, when they've shared their location. */
  miles?: number | null;
}

/** Which connect surface a monitoring vendor gets here. Vendors with no
 *  account-level connect path in this modal fall through to "unsupported" —
 *  they're connected from an array's monitoring panel instead, and we say so
 *  rather than showing a form that can't finish. */
type MonitorMode =
  | "login-discover"
  | "key-account"
  | "unsupported";

function monitorMode(v: InverterVendorEntry): MonitorMode {
  if (!v.available) return "unsupported";
  if (v.connect_mode === "consent") return "unsupported";
  // Both login-based monitors preview their sites first — never connect a whole
  // account blind. A partner login can span several operators' sites, which is
  // exactly what the pick step exists to catch.
  if (v.code === "locus" || v.code === "alsoenergy") return "login-discover";
  if (v.code === "solaredge") return "key-account";
  return "unsupported";
}

/** A site checkbox row — Locus and SolarEdge discover results share this shape. */
interface PickSite {
  site_id: number;
  name: string;
  peak_power_kw?: number | null;
}

/**
 * MonitorConnectForm — the one form for a monitoring vendor, driven by its
 * connect mode. Generalized from the old Locus-only form:
 *   • locus       → discover → pick sites → create client → connect-account
 *   • alsoenergy  → NO discover route on the backend, so we connect the whole
 *                   account in one call (it enumerates sites server-side)
 *   • solaredge   → API key → discover → pick sites → connect-account. That
 *                   endpoint takes no client_id, so we don't claim a client;
 *                   the sites land in Discover for the operator to file.
 */
function MonitorConnectForm({
  vendor,
  onCreated,
  onReview,
}: {
  vendor: InverterVendorEntry;
  onCreated: () => void;
  onReview: () => void;
}) {
  const toast = useToast();
  const mode = monitorMode(vendor);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [clientName, setClientName] = useState("");
  const [sites, setSites] = useState<PickSite[] | null>(null);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<"discover" | "create" | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function discover() {
    setBusy("discover");
    setErr(null);
    try {
      if (mode === "key-account") {
        const key = apiKey.trim();
        if (!key) {
          toast.show("Paste your SolarEdge API key.", "info");
          return;
        }
        const r = await discoverSolarEdge(key);
        setSites(r.sites);
        setPicked(new Set(r.sites.map((s) => s.site_id))); // all pre-checked
        if (!r.sites.length) setErr(r.message || "No sites found on that key.");
        return;
      }
      const user = username.trim();
      if (!user) {
        toast.show(`Enter your ${vendor.label} username.`, "info");
        return;
      }
      if (!password) {
        toast.show(`Enter your ${vendor.label} password.`, "info");
        return;
      }
      if (!clientName.trim()) setClientName(nameFromLogin(user));
      const r =
        vendor.code === "alsoenergy"
          ? await discoverAlsoEnergy(user, password)
          : await discoverLocus(user, password);
      setSites(r.sites);
      setPicked(new Set(r.sites.map((s) => s.site_id))); // all pre-checked
      if (!r.sites.length)
        setErr(r.message || "No sites found under this login.");
    } catch (e) {
      setErr(
        e instanceof Error
          ? e.message
          : `Couldn't reach ${vendor.label} with that login.`,
      );
    } finally {
      setBusy(null);
    }
  }

  function toggle(siteId: number) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(siteId)) next.delete(siteId);
      else next.add(siteId);
      return next;
    });
  }

  async function create() {
    setBusy("create");
    setErr(null);
    try {
      if (mode === "key-account") {
        // This endpoint has no client_id — connect the sites, then point the
        // operator at Discover to file them under a client.
        const res = await connectSolarEdgeAccount(apiKey.trim(), [...picked]);
        toast.success(
          `${res.connected.length} SolarEdge array${res.connected.length === 1 ? "" : "s"} connected. ` +
            `File them under a client in Discover.`,
        );
        onReview();
        return;
      }
      const name = clientName.trim() || nameFromLogin(username);
      const client = await createClient({ name });
      const res =
        vendor.code === "alsoenergy"
          ? await connectAlsoEnergyAccount(username.trim(), password, {
              siteIds: [...picked],
              clientId: client.id,
            })
          : await connectLocusAccount(username.trim(), password, {
              siteIds: [...picked],
              clientId: client.id,
            });
      toast.success(
        `${client.name}: ${res.connected.length} array${res.connected.length === 1 ? "" : "s"} connected. ` +
          `We're pulling their generation now — the report will be ready shortly.`,
      );
      onCreated();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Couldn't create the client.");
    } finally {
      setBusy(null);
    }
  }

  if (mode === "unsupported") {
    return (
      <div className="space-y-2">
        <p className="text-sm font-semibold text-zinc-900">{vendor.label}</p>
        {vendor.note && <p className="text-xs text-zinc-600">{vendor.note}</p>}
        <p className="text-xs text-zinc-500">
          We can&apos;t connect this one from here yet — open the array in your
          dashboard and connect it from its monitoring panel.
        </p>
      </div>
    );
  }

  // Every monitor previews its sites, so there is always something to check.
  const canCreate = picked.size > 0;

  return (
    <div className="space-y-3">
      <p className="text-sm font-semibold text-zinc-900">
        Connect {vendor.label}
      </p>
      <p className="text-xs text-zinc-500">
        {mode === "key-account"
          ? "Paste an account-level API key and we'll show you every site it can read before anything is saved."
          : "We'll find every site under this login. The login becomes a client and the sites you keep become its arrays — ready for generation reports."}
      </p>

      {mode === "key-account" ? (
        <input
          type="password"
          autoComplete="off"
          placeholder="SolarEdge API key"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          className="w-full rounded-lg border border-zinc-200 px-3 py-2 text-sm focus:border-primary-400 focus:outline-none"
        />
      ) : (
        <>
          <input
            type="text"
            autoComplete="off"
            placeholder={`${vendor.label} username`}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="w-full rounded-lg border border-zinc-200 px-3 py-2 text-sm focus:border-primary-400 focus:outline-none"
          />
          <input
            type="password"
            autoComplete="off"
            placeholder={`${vendor.label} password`}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded-lg border border-zinc-200 px-3 py-2 text-sm focus:border-primary-400 focus:outline-none"
          />
        </>
      )}

      {sites === null ? (
        <Button
          disabled={busy !== null}
          onClick={discover}
          className="w-full py-2 text-sm"
        >
          {busy === "discover" ? "Finding sites…" : "Find my sites"}
        </Button>
      ) : (
        <>
          {mode !== "key-account" && (
            <label className="block text-xs font-medium text-zinc-600">
              Client name
              <input
                type="text"
                value={clientName}
                onChange={(e) => setClientName(e.target.value)}
                className="mt-1 w-full rounded-lg border border-zinc-200 px-3 py-2 text-sm text-zinc-900 focus:border-primary-400 focus:outline-none"
              />
            </label>
          )}
          {sites.length > 0 && (
            <div className="rounded-lg border border-zinc-200 bg-white">
              <div className="flex items-center justify-between border-b border-zinc-100 px-3 py-2">
                <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                  Sites found ({picked.size}/{sites.length})
                </span>
                <button
                  type="button"
                  className="text-xs font-medium text-primary-600 hover:underline"
                  onClick={() =>
                    setPicked(
                      picked.size === sites.length
                        ? new Set()
                        : new Set(sites.map((s) => s.site_id)),
                    )
                  }
                >
                  {picked.size === sites.length ? "Clear all" : "Select all"}
                </button>
              </div>
              <ul className="max-h-52 overflow-y-auto">
                {sites.map((s) => (
                  <li key={s.site_id}>
                    <label className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm hover:bg-zinc-50">
                      <input
                        type="checkbox"
                        checked={picked.has(s.site_id)}
                        onChange={() => toggle(s.site_id)}
                        className="h-4 w-4"
                      />
                      <span className="truncate text-zinc-800">{s.name}</span>
                      {s.peak_power_kw ? (
                        <span className="ml-auto shrink-0 text-xs text-zinc-400">
                          {s.peak_power_kw} kW
                        </span>
                      ) : null}
                    </label>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <Button
            disabled={busy !== null || !canCreate}
            onClick={create}
            className="w-full py-2 text-sm"
          >
            {busy === "create"
              ? mode === "key-account"
                ? "Connecting…"
                : "Creating client…"
              : mode === "key-account"
                ? `Connect ${picked.size} site${picked.size === 1 ? "" : "s"}`
                : `Create client with ${picked.size} site${picked.size === 1 ? "" : "s"}`}
          </Button>
          <button
            type="button"
            className="w-full text-xs text-zinc-500 hover:underline"
            onClick={() => {
              setSites(null);
              setPicked(new Set());
              setErr(null);
            }}
          >
            ← use a different login
          </button>
        </>
      )}

      {err && <p className="text-xs font-medium text-red-600">{err}</p>}
    </div>
  );
}

/**
 * UtilityCloudForm — the cloud-capture connect form for a utility the operator
 * has ALREADY chosen in the search. This is the old CloudAddLoginForm with its
 * own utility picker removed: the search above is the picker now, so there's
 * one search box in this modal instead of two.
 *
 * We store the login server-side (POST /v1/cloud-capture/credentials) and the
 * backend spins up a client named from the login (ensure_client_for_login),
 * then the headless harvester signs in and fills that client's arrays.
 */
function UtilityCloudForm({
  utility,
  onCreated,
}: {
  utility: SmartHubEntry;
  onCreated: () => void;
}) {
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [consent, setConsent] = useState(false);
  const [saving, setSaving] = useState(false);

  async function submit() {
    const user = username.trim();
    if (!user) {
      toast.show("Enter the login's username or email.", "info");
      return;
    }
    if (!password) {
      toast.show("Enter the login's password.", "info");
      return;
    }
    if (!consent) {
      toast.show("Tick the box to store this login securely.", "info");
      return;
    }
    setSaving(true);
    try {
      await setCloudCredential({
        provider: utility.provider,
        username: user,
        password,
        login_host: utility.host || null,
        enable: true,
        consent: true,
      });
      toast.success(
        `Connected ${utility.name}. We'll pull this login's bills on the next sync ` +
          `and its arrays will appear automatically — no need to re-enter anything.`,
      );
      onCreated();
    } catch (e) {
      toast.show(
        e instanceof Error ? e.message : "Couldn't save that login — try again.",
        "error",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-sm font-semibold text-zinc-900">
        Connect {utility.name}
      </p>
      <p className="text-xs text-zinc-500">
        Give us the login and we create the client from it — no signing into a
        portal, no extension. Their arrays fill in as we pull the bills.
      </p>

      <div className="grid gap-2 sm:grid-cols-2">
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="Login username or email"
          autoComplete="off"
          className="rounded-lg border border-cream-border px-3 py-2 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-primary-400 focus:outline-none focus:ring-2 focus:ring-primary-500/30"
        />
        <input
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          type="password"
          placeholder="Login password"
          autoComplete="new-password"
          className="rounded-lg border border-cream-border px-3 py-2 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-primary-400 focus:outline-none focus:ring-2 focus:ring-primary-500/30"
        />
      </div>

      <label className="flex items-start gap-2 text-xs text-zinc-600">
        <input
          type="checkbox"
          checked={consent}
          onChange={(e) => setConsent(e.target.checked)}
          className="mt-0.5 h-4 w-4 shrink-0 rounded border-cream-border text-primary-600 focus:ring-primary-500/40"
        />
        <span>
          Store this login securely on our servers so we can pull {utility.name}
          &rsquo;s bills automatically. Remove it anytime in the credential vault.
        </span>
      </label>

      <Button
        onClick={() => void submit()}
        disabled={saving || !username.trim() || !password || !consent}
        className="w-full py-2.5 text-sm"
      >
        {saving ? "Connecting…" : "Add client from this login"}
      </Button>
    </div>
  );
}

/**
 * AddClientByLoginModal — two sections, no repetition.
 *
 * Section 1 lists every login already feeding generation reports (utility and
 * monitoring together) with a plain status line, so the operator sees what
 * they've got before adding anything. Section 2 is a single search across the
 * utility catalog AND the monitoring-vendor catalog; picking one result reveals
 * the one form that entry needs.
 *
 * Earlier versions stacked three near-identical credential forms — an
 * unassigned-login picker, a utility form with its own utility picker, and a
 * Locus form — three password fields on one screen. That's gone.
 *
 * The extension path underneath is unchanged: on a non-cloud tenant, picking a
 * utility still opens its portal in a fresh tab after a cookie wipe, the
 * extension POSTs /v1/sync, and <CaptureListener> in ClientsSection toasts the
 * new client. This modal just closes on the way.
 */
export function AddClientByLoginModal({
  open,
  onClose,
  onCaptured,
  onSwitchToManual,
  cloudMode = false,
}: Props) {
  const toast = useToast();
  const navigate = useNavigate();
  // Cloud mode never needs extension probe UI — skip live probe noise.
  const ext = useExtensionStatus(open && !cloudMode);

  // The two catalogs behind the one search. SmartHub portals come from the
  // provider catalog (rows with a smarthub_host); monitoring vendors come from
  // the inverter-vendor catalog. Either failing is non-fatal — whatever loaded
  // is searchable, and manual entry always works.
  const [smarthubPortals, setSmarthubPortals] = useState<SmartHubEntry[]>([]);
  const [vendors, setVendors] = useState<InverterVendorEntry[]>([]);
  const [catalogLoaded, setCatalogLoaded] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<CatalogEntry | null>(null);

  // Operator location for "nearest utilities first". null = not requested or
  // denied (we fall back to catalog order). geoState tracks the UX of the ask.
  const [coords, setCoords] = useState<{ lat: number; lng: number } | null>(null);
  const [geoState, setGeoState] = useState<
    "idle" | "prompting" | "granted" | "denied" | "unsupported"
  >("idle");

  function requestLocation() {
    if (!("geolocation" in navigator)) {
      setGeoState("unsupported");
      return;
    }
    setGeoState("prompting");
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setCoords({ lat: pos.coords.latitude, lng: pos.coords.longitude });
        setGeoState("granted");
      },
      () => {
        // Denied or errored — silently fall back to the alphabetical list.
        setGeoState("denied");
      },
      { enableHighAccuracy: false, timeout: 8000, maximumAge: 600_000 },
    );
  }

  // Close the modal and land the operator on the staging pool, where the sites
  // a login can see get filed under clients. Same route in both routers (the
  // standalone app and the embed's MemoryRouter).
  const goReview = useCallback(() => {
    onClose();
    navigate("/discover");
  }, [navigate, onClose]);

  // Re-probe whenever the modal opens so a freshly-installed extension
  // is detected without a page reload.
  useEffect(() => {
    if (open) void ext.probe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Load both catalogs once the modal opens (cached after first load).
  useEffect(() => {
    if (!open || catalogLoaded) return;
    let cancelled = false;
    (async () => {
      const [providers, vendorList] = await Promise.all([
        getProviders().catch(() => []),
        getInverterVendors().catch(() => []),
      ]);
      if (cancelled) return;
      const portals: SmartHubEntry[] = providers
        .filter((p) => p.scrape_status === "live" && p.smarthub_host)
        .map((p) => ({
          provider: p.code,
          name: p.label,
          hint: p.state || "SmartHub",
          stateCode: (p.state || "").toUpperCase(),
          url: p.portal_url || `https://${p.smarthub_host}/`,
          host: p.smarthub_host || "",
        }))
        .sort((a, b) => a.name.localeCompare(b.name));
      setSmarthubPortals(portals);
      setVendors(vendorList);
      setCatalogLoaded(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [open, catalogLoaded]);

  // The one merged catalog the search runs over: GMP (special-cased — it's not
  // a SmartHub co-op, it has its own version-aware login URL) + every live
  // SmartHub utility + every monitoring vendor.
  const catalog = useMemo<CatalogEntry[]>(() => {
    const gmp: SmartHubEntry = {
      provider: "gmp",
      name: GMP_FRIENDLY,
      hint: "VT",
      stateCode: "VT",
      url: "",
      host: "",
    };
    const utilities: CatalogEntry[] = [gmp, ...smarthubPortals].map((u) => ({
      key: `utility:${u.provider}`,
      kind: "utility",
      code: u.provider,
      label: u.name,
      hint: u.hint,
      utility: u,
    }));
    const monitors: CatalogEntry[] = vendors.map((v) => ({
      key: `monitoring:${v.code}`,
      kind: "monitoring",
      code: v.code,
      label: v.label,
      hint: "",
      vendor: v,
    }));
    return [...utilities, ...monitors];
  }, [smarthubPortals, vendors]);

  // Typing filters both kinds into one list. An empty query shows a short
  // default set (GMP, the account-level monitors, then the nearest few
  // utilities) so the box is never blank. Distance ranking kicks in once the
  // operator has shared their location; otherwise catalog order stands.
  const results = useMemo<CatalogEntry[]>(() => {
    const q = query.trim().toLowerCase();
    const withMiles = (e: CatalogEntry): CatalogEntry =>
      coords && e.utility
        ? { ...e, miles: milesToState(coords, e.utility.stateCode) }
        : { ...e, miles: null };
    const byDistance = (a: CatalogEntry, b: CatalogEntry) => {
      // Unknown distance (no/blank state, or a monitoring vendor) sorts last.
      if (a.miles == null && b.miles == null) return 0;
      if (a.miles == null) return 1;
      if (b.miles == null) return -1;
      return a.miles - b.miles;
    };

    if (!q) {
      const featured = [
        "utility:gmp",
        "monitoring:locus",
        "monitoring:alsoenergy",
        "monitoring:solaredge",
      ];
      const head = featured
        .map((k) => catalog.find((e) => e.key === k))
        .filter((e): e is CatalogEntry => !!e)
        .map(withMiles);
      const rest = catalog
        .filter((e) => e.kind === "utility" && !featured.includes(e.key))
        .map(withMiles)
        .sort(byDistance)
        .slice(0, 6);
      return [...head, ...rest];
    }

    return catalog
      .filter(
        (e) =>
          e.label.toLowerCase().includes(q) ||
          e.code.toLowerCase().includes(q) ||
          e.hint.toLowerCase().includes(q),
      )
      .map(withMiles)
      .sort(byDistance)
      .slice(0, 30);
  }, [catalog, query, coords]);

  /**
   * pick — open a utility's login portal in a fresh tab after a cookie wipe.
   * `code` is the provider code ("gmp" or any SmartHub code); `friendly` is the
   * display name for the toast; `portalUrl` is the SmartHub URL (ignored for GMP,
   * which computes its own version-aware URL).
   */
  function pick(code: string, friendly: string, portalUrl?: string) {
    const isGmp = code === "gmp";
    const url = isGmp ? gmpPortalUrl(ext.version) : (portalUrl ?? "");
    const host = isGmp ? "greenmountainpower.com" : "smarthub.coop";

    // Pattern A: open about:blank SYNCHRONOUSLY (foreground tab guaranteed —
    // no await between click and window.open). We omit noopener so the
    // returned reference is non-null and we can set location.href after the
    // wipe. about:blank has no meaningful cross-origin security concern.
    const t0 = Date.now();
    console.log(`[SO ${t0}] add-login: open about:blank for ${code}`);
    const newTab = window.open("about:blank", "_blank");
    if (!newTab) {
      // Popup blocker — user-initiated click should never hit this in practice.
      return;
    }

    // Await the cookie wipe ack, THEN navigate. This prevents the race where
    // the portal loads with stale session cookies and lands on the previous
    // customer's dashboard.
    //
    // Extension v1.9.109+: a page-initiated wipe needs a one-click approval in
    // the EnergyAgent popup (security hardening — a page script can't silently
    // wipe sessions anymore). We still navigate right away (best-effort, the
    // old behaviour), and tell the operator how to finish the reset: approving
    // in the popup wipes the cookies AND reloads the portal tab signed-out.
    (async () => {
      console.log(`[SO ${Date.now()}] add-login: wipe-start for ${host}`);
      const wipe = await wipeCookiesAndWait(host);
      console.log(`[SO ${Date.now()}] add-login: wipe=${wipe} (+${Date.now() - t0}ms), navigating tab`);
      try {
        newTab.location.href = url;
        console.log(`[SO ${Date.now()}] add-login: tab.location.href set → ${url}`);
      } catch (e) {
        // Should not happen while tab is on about:blank but log if it does.
        console.error("[SO] could not navigate new tab:", e);
      }
      if (wipe === "pending") {
        toast.show(
          `Almost there — if ${friendly} opens already signed in, click the EnergyAgent icon in your toolbar and approve "Reset session". The portal reloads signed-out so you can sign in as the client.`,
          "info",
        );
      }
    })();

    // Snapshot known clients + close modal. CaptureListener handles
    // success notification when the extension POSTs /v1/sync.
    (async () => {
      try {
        const before = await onCaptured();
        try {
          sessionStorage.setItem(
            "so_capture_pending",
            JSON.stringify({
              provider: code,
              startedAt: Date.now(),
              knownIds: before.map((c) => c.id),
            }),
          );
          try {
            window.dispatchEvent(
              new CustomEvent("so:capture-pending", { detail: { provider: code } }),
            );
          } catch { /* ignore */ }
        } catch { /* non-fatal */ }
      } catch { /* parent surfaces its own errors */ }
    })();

    onClose();

    if (!extensionUsable) {
      toast.show(
        "Sign in at the portal, then add the client manually from the dashboard.",
        "info",
      );
    } else {
      toast.success(
        `Opened ${friendly}. Sign in as the new client — their arrays show up here automatically.`,
      );
    }
  }

  const extensionUsable = ext.status === "present-paired";
  const extensionUnpaired = ext.status === "present-unpaired";
  const extensionAbsent = ext.status === "absent";
  const extensionUnknown = ext.status === "unknown";

  // Every "we made something" path ends the same way: refresh the parent's
  // roster and get out of the way.
  const finish = () => {
    void onCaptured();
    onClose();
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Add a client"
      footer={
        <>
          <Button
            variant={extensionUsable ? "ghost" : "primary"}
            onClick={onSwitchToManual}
          >
            {extensionUsable ? "Add manually instead" : "Add manually →"}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            I'm done
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {/* ── 1. What you already have ── */}
        <ConnectedLoginsList onCreated={finish} onReview={goReview} />

        <div className="flex items-center gap-3 pt-1">
          <span className="h-px flex-1 bg-cream-border" />
          <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">
            connect a login
          </span>
          <span className="h-px flex-1 bg-cream-border" />
        </div>

        {/* ── 2. One search across utilities + monitoring, then one form ── */}
        {selected === null ? (
          <div className="space-y-2">
            <p className="text-xs text-zinc-500">
              Find the utility or monitoring portal your client signs into.
            </p>
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search for your utility or monitoring portal…"
              aria-label="Search for your utility or monitoring portal"
              className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm focus:border-primary-400 focus:outline-none focus:ring-1 focus:ring-primary-400"
            />

            {/* Nearest-first control. Asks for browser location, then ranks the
                utilities by distance to their state. Pure convenience —
                denial/absence falls back silently to the catalog order. */}
            {smarthubPortals.length > 1 && (
              <div>
                {geoState !== "granted" ? (
                  <button
                    type="button"
                    onClick={requestLocation}
                    disabled={geoState === "prompting"}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-300 px-2.5 py-1.5 text-xs font-medium text-zinc-600 transition-colors hover:border-primary-400 hover:text-primary-600 disabled:opacity-60"
                  >
                    <span aria-hidden>📍</span>
                    {geoState === "prompting" ? "Locating…" : "Sort by nearest to me"}
                  </button>
                ) : (
                  <div className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-xs font-medium text-emerald-700">
                    <span aria-hidden>📍</span>
                    Sorted by nearest to you
                  </div>
                )}
                {geoState === "denied" && (
                  <span className="ml-2 text-xs text-zinc-400">
                    Location off — search by state instead.
                  </span>
                )}
                {geoState === "unsupported" && (
                  <span className="ml-2 text-xs text-zinc-400">
                    Location isn&apos;t available in this browser.
                  </span>
                )}
              </div>
            )}

            {!catalogLoaded && (
              <p className="text-xs text-zinc-400">Loading portals&hellip;</p>
            )}

            <ul className="max-h-64 space-y-1 overflow-y-auto">
              {results.map((e) => (
                <li key={e.key}>
                  <button
                    type="button"
                    onClick={() => setSelected(e)}
                    className="flex w-full items-center gap-2 rounded-lg border border-cream-border bg-white px-3 py-2 text-left transition-colors hover:border-primary-400 hover:bg-primary-50/40"
                  >
                    <span className="min-w-0 flex-1 truncate text-sm text-zinc-900">
                      {e.label}
                    </span>
                    {e.hint && (
                      <span className="shrink-0 text-xs text-zinc-400">
                        {e.hint}
                        {e.miles != null ? ` · ~${Math.round(e.miles)} mi` : ""}
                      </span>
                    )}
                    <span
                      className={
                        "shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide " +
                        (e.kind === "monitoring"
                          ? "bg-primary-50 text-primary-700"
                          : "bg-zinc-100 text-zinc-500")
                      }
                    >
                      {e.kind === "monitoring" ? "Monitoring" : "Utility"}
                    </span>
                  </button>
                </li>
              ))}
            </ul>

            {catalogLoaded && results.length === 0 && (
              <p className="text-xs text-zinc-500">
                Nothing matches &ldquo;{query}&rdquo;. Submit the utility below
                and we&apos;ll add it.
              </p>
            )}

            {/* Escape hatch: the operator's client uses a utility we don't list
                yet. Submitting routes to a Hermes agent that adds it to the repo. */}
            <SubmitUtilityForm />
          </div>
        ) : (
          <div className="space-y-3">
            <button
              type="button"
              onClick={() => setSelected(null)}
              className="text-xs font-medium text-zinc-500 hover:underline"
            >
              ← back to search
            </button>

            {selected.kind === "monitoring" && selected.vendor && (
              <MonitorConnectForm
                vendor={selected.vendor}
                onCreated={finish}
                onReview={goReview}
              />
            )}

            {selected.kind === "utility" && selected.utility && cloudMode && (
              <UtilityCloudForm utility={selected.utility} onCreated={finish} />
            )}

            {/* Extension tenant: unchanged behaviour — we don't take the
                password, we open the portal and let the extension capture. */}
            {selected.kind === "utility" && selected.utility && !cloudMode && (
              <div className="space-y-3">
                <ExtensionStatusBanner status={ext.status} version={ext.version} />
                {extensionUsable && (
                  <p className="text-sm text-zinc-600">
                    We&apos;ll open {selected.label} in a fresh tab — sign in
                    there as the new client, and their arrays appear in your
                    dashboard automatically.
                  </p>
                )}
                {extensionUnpaired && (
                  <p className="text-sm text-zinc-600">
                    Your extension is installed but not paired to this account
                    yet. You can still open the portal — after sign-in
                    we&apos;ll fall back to a manual refresh.
                  </p>
                )}
                {extensionAbsent && (
                  <p className="text-sm text-zinc-600">
                    Without the extension, auto-capture can&apos;t finish.{" "}
                    <b className="text-zinc-900">Add manually instead</b> —
                    it&apos;s quick, and the extension will continue capturing
                    data on future logins.
                  </p>
                )}
                {extensionUnknown && (
                  <p className="text-sm text-zinc-500">
                    Checking your extension&hellip;
                  </p>
                )}
                <div className={extensionAbsent ? "opacity-50" : ""}>
                  <PortalCard
                    label={selected.label}
                    hint={selected.hint || "Sign in as the client"}
                    cta={`Open ${selected.label} →`}
                    onClick={() =>
                      pick(selected.code, selected.label, selected.utility?.url)
                    }
                    disabled={extensionUnknown}
                  />
                </div>
                <p className="text-xs text-zinc-500">
                  <b className="text-zinc-700">Adding multiple clients?</b> Stay
                  in the portal tab and sign out, then sign into the next
                  client&apos;s account. Each sign-in captures another client
                  automatically — you don&apos;t need to come back here between
                  them.
                </p>
              </div>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}

function ExtensionStatusBanner({
  status,
  version,
}: {
  status: ExtensionStatus;
  version: string | null;
}) {
  if (status === "unknown") return null;
  if (status === "present-paired") {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-600">
        <span aria-hidden>✓</span>
        <span>
          Extension paired{version ? ` (v${version})` : ""} — auto-capture is on.
        </span>
      </div>
    );
  }
  if (status === "present-unpaired") {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
        <span aria-hidden>⚠</span>
        <span>
          Extension installed{version ? ` (v${version})` : ""} but not paired
          yet. Auto-capture may not fire.
        </span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-900">
      <span aria-hidden>✗</span>
      <span>
        Extension not detected. Install it from{" "}
        <a
          href="/onboarding/#/extension"
          target="_blank"
          rel="noopener noreferrer"
          className="font-semibold underline hover:text-red-700"
        >
          Setup
        </a>{" "}
        to enable auto-capture, or add this client manually below.
      </span>
    </div>
  );
}

function PortalCard({
  label,
  hint,
  onClick,
  disabled,
  cta = "Open portal →",
}: {
  label: string;
  hint: string;
  onClick: () => void;
  disabled: boolean;
  cta?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="group flex w-full flex-col items-start gap-1 rounded-xl border-2 border-zinc-200 bg-white p-4 text-left transition-all hover:border-emerald-400 hover:bg-emerald-50/50 disabled:cursor-not-allowed disabled:opacity-50"
    >
      <span className="text-base font-semibold text-zinc-900 group-hover:text-emerald-600">
        {label}
      </span>
      <span className="text-xs text-zinc-500">{hint}</span>
      <span className="mt-2 text-xs font-medium text-emerald-600 group-hover:text-emerald-600">
        {cta}
      </span>
    </button>
  );
}

/**
 * SubmitUtilityForm — the "Don't see your utility?" escape hatch shown under
 * the search results. Collapsed to a single text button by default (click is
 * tax — don't make operators read a form they rarely need). On expand it
 * collects the utility name + optional portal/region/notes and POSTs to
 * /v1/account/request-utility, which emails the SO team and fires the Hermes
 * agent webhook that adds the utility to the repo and opens a PR.
 */
function SubmitUtilityForm() {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const [name, setName] = useState("");
  const [portal, setPortal] = useState("");
  const [region, setRegion] = useState("");
  const [notes, setNotes] = useState("");

  async function submit() {
    const utility = name.trim();
    if (!utility) {
      toast.show("Enter the utility's name first.", "info");
      return;
    }
    setSubmitting(true);
    try {
      await requestUtilityAddition({
        utility_name: utility,
        portal_url: portal.trim() || null,
        region: region.trim() || null,
        notes: notes.trim() || null,
      });
      setDone(true);
      toast.success(
        `Thanks — we got your request for ${utility}. We'll add it and follow up.`,
      );
    } catch (e) {
      toast.show(
        e instanceof Error ? e.message : "Couldn't submit that — try again.",
        "error",
      );
    } finally {
      setSubmitting(false);
    }
  }

  if (done) {
    return (
      <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
        Request received — we&apos;ll add{" "}
        <b>{name.trim() || "your utility"}</b> and let you know when it&apos;s
        ready. You can close this and add the client manually in the meantime.
      </div>
    );
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-4 w-full rounded-xl border border-dashed border-zinc-300 px-4 py-3 text-left text-sm font-medium text-zinc-500 transition-colors hover:border-emerald-400 hover:text-emerald-600"
      >
        Don&apos;t see your utility? Submit a utility for addition →
      </button>
    );
  }

  return (
    <div className="mt-4 space-y-3 rounded-xl border border-zinc-200 bg-zinc-50 p-4">
      <div>
        <p className="text-sm font-semibold text-zinc-900">
          Submit a utility for addition
        </p>
        <p className="mt-0.5 text-xs text-zinc-500">
          Tell us the utility your client signs into and we&apos;ll add support
          for it. SmartHub co-ops are usually quick; others we&apos;ll scope and
          follow up.
        </p>
      </div>

      <div className="space-y-2">
        <input
          type="text"
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Utility name (e.g. Burlington Electric Department)"
          className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm focus:border-emerald-400 focus:outline-none focus:ring-1 focus:ring-emerald-400"
        />
        <input
          type="text"
          value={portal}
          onChange={(e) => setPortal(e.target.value)}
          placeholder="Login / portal URL (optional)"
          className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm focus:border-emerald-400 focus:outline-none focus:ring-1 focus:ring-emerald-400"
        />
        <input
          type="text"
          value={region}
          onChange={(e) => setRegion(e.target.value)}
          placeholder="State / region (optional, e.g. VT)"
          className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm focus:border-emerald-400 focus:outline-none focus:ring-1 focus:ring-emerald-400"
        />
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Anything else we should know? (optional)"
          rows={2}
          className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm focus:border-emerald-400 focus:outline-none focus:ring-1 focus:ring-emerald-400"
        />
      </div>

      <div className="flex items-center gap-2">
        <Button variant="primary" onClick={submit} disabled={submitting}>
          {submitting ? "Submitting…" : "Submit utility"}
        </Button>
        <Button
          variant="ghost"
          onClick={() => setOpen(false)}
          disabled={submitting}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
