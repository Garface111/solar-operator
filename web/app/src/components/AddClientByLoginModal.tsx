import { useEffect, useState, useMemo } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { useToast } from "../ui/Toast";
import {
  requestUtilityAddition,
  getProviders,
  getPortalAccess,
  createClient,
  setCloudCredential,
  discoverLocus,
  connectLocusAccount,
  type LocusDiscoveredSite,
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
// they're managed in the credential vault ("Add more logins" below).
const ATTACHABLE: Record<string, { label: string; field: "gmp" | "vec" }> = {
  gmp: { label: "Green Mountain Power", field: "gmp" },
  vec: { label: "Vermont Electric Coop", field: "vec" },
};

/**
 * LinkedLoginsPicker — the primary Add-Client path (Ford 2026-07-16):
 * pick a utility login you've ALREADY linked (from GET /v1/portal-access
 * unassigned_logins, i.e. saved logins no client claims yet) and spin up a
 * client from it — no re-signing into a portal. Plus a big button that opens
 * the Master-account credential vault to add more logins.
 */
function LinkedLoginsPicker({
  onCreated,
  onAddMore,
}: {
  onCreated: () => void;
  onAddMore: () => void;
}) {
  const toast = useToast();
  const [logins, setLogins] = useState<PortalAccessUnassigned[] | null>(null);
  const [openFor, setOpenFor] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getPortalAccess()
      .then((p) => {
        if (cancelled) return;
        setLogins(p.unassigned_logins.filter((l) => ATTACHABLE[l.provider]));
      })
      .catch(() => {
        if (!cancelled) setLogins([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function create(login: PortalAccessUnassigned) {
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
          Add from a login you&apos;ve already linked
        </p>
        <p className="mt-0.5 text-xs text-zinc-500">
          Pick a saved utility login and we&apos;ll create the client from it —
          no need to sign in again.
        </p>
      </div>

      {logins === null ? (
        <div className="rounded-xl border border-cream-border px-4 py-3 text-sm text-zinc-500">
          Loading your linked logins&hellip;
        </div>
      ) : logins.length === 0 ? (
        <div className="rounded-xl border border-dashed border-cream-border px-4 py-3 text-sm text-zinc-600">
          No unassigned utility logins yet. Add one below, then it&apos;ll show
          up here to attach to a client.
        </div>
      ) : (
        <ul className="space-y-2">
          {logins.map((l) => {
            const meta = ATTACHABLE[l.provider];
            const isOpen = openFor === l.username + ":" + l.provider;
            return (
              <li
                key={l.provider + ":" + l.username}
                className="rounded-xl border border-cream-border bg-white p-3 shadow-sm"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-zinc-900">
                      {l.username}
                    </p>
                    <p className="text-xs text-zinc-500">
                      {meta.label}
                      {l.status === "automated" ? " · syncing automatically" : ""}
                    </p>
                  </div>
                  {!isOpen && (
                    <Button
                      onClick={() => {
                        setOpenFor(l.username + ":" + l.provider);
                        setName("");
                      }}
                      className="shrink-0 px-3 py-1.5 text-xs"
                    >
                      Use this login →
                    </Button>
                  )}
                </div>
                {isOpen && (
                  <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                    <input
                      autoFocus
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void create(l);
                      }}
                      placeholder="Client name (e.g. Town of Glover)"
                      className="min-w-0 flex-1 rounded-lg border border-cream-border px-3 py-2 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-primary-400 focus:outline-none focus:ring-2 focus:ring-primary-500/30"
                    />
                    <div className="flex gap-2">
                      <Button
                        onClick={() => void create(l)}
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

      <button
        type="button"
        onClick={onAddMore}
        className="flex w-full items-center justify-center gap-2 rounded-xl bg-primary-600 px-4 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
      >
        <span aria-hidden className="text-base leading-none">＋</span>
        Add more logins to the utility accounts
      </button>
    </div>
  );
}

/**
 * CloudAddLoginForm — the Cloud-Capture "Add a client" path (Ford 2026-07-16).
 *
 * On a cloud-capture tenant the operator should NOT be sent to the utility's
 * website to sign in with the Chrome extension. Instead they hand us the login
 * (utility + username + password + consent); we store it server-side
 * (POST /v1/cloud-capture/credentials) and the backend immediately spins up a
 * "Pulling bills…" client NAMED FROM THE LOGIN (ensure_client_for_login), then
 * the headless harvester signs in and fills that client's arrays. No extension.
 */
function CloudAddLoginForm({
  portals,
  onCreated,
}: {
  portals: SmartHubEntry[];
  onCreated: () => void;
}) {
  const toast = useToast();
  // Utility selection: GMP by default, or a co-op picked from the live list.
  const [util, setUtil] = useState<{ provider: string; label: string; host: string }>({
    provider: "gmp",
    label: GMP_FRIENDLY,
    host: "",
  });
  const [pickingUtil, setPickingUtil] = useState(false);
  const [utilQuery, setUtilQuery] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [consent, setConsent] = useState(false);
  const [saving, setSaving] = useState(false);

  const utilMatches = useMemo(() => {
    const q = utilQuery.trim().toLowerCase();
    const base = q
      ? portals.filter(
          (p) => p.name.toLowerCase().includes(q) || p.hint.toLowerCase().includes(q),
        )
      : portals;
    return base.slice(0, 40);
  }, [portals, utilQuery]);

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
        provider: util.provider,
        username: user,
        password,
        login_host: util.host || null,
        enable: true,
        consent: true,
      });
      toast.success(
        `Connected ${util.label}. We'll pull this login's bills on the next sync ` +
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
      <div>
        <p className="text-sm font-semibold text-zinc-900">Add a client by its utility login</p>
        <p className="mt-0.5 text-xs text-zinc-500">
          Give us the login and we create the client from it — no signing into a
          portal, no extension. Their arrays fill in as we pull the bills.
        </p>
      </div>

      {/* Utility selector */}
      <div>
        <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Utility
        </label>
        {!pickingUtil ? (
          <button
            type="button"
            onClick={() => {
              setPickingUtil(true);
              setUtilQuery("");
            }}
            className="flex w-full items-center justify-between rounded-lg border border-cream-border bg-white px-3 py-2 text-left text-sm text-zinc-900 hover:border-primary-400"
          >
            <span>{util.label}</span>
            <span className="text-xs text-primary-700">Change ▾</span>
          </button>
        ) : (
          <div className="rounded-lg border border-cream-border bg-white p-2">
            <input
              autoFocus
              value={utilQuery}
              onChange={(e) => setUtilQuery(e.target.value)}
              placeholder="Search utilities…"
              className="mb-2 w-full rounded-md border border-cream-border px-2 py-1.5 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-primary-400 focus:outline-none"
            />
            <ul className="max-h-40 space-y-0.5 overflow-y-auto">
              <li>
                <button
                  type="button"
                  onClick={() => {
                    setUtil({ provider: "gmp", label: GMP_FRIENDLY, host: "" });
                    setPickingUtil(false);
                  }}
                  className="w-full rounded-md px-2 py-1.5 text-left text-sm text-zinc-800 hover:bg-primary-50"
                >
                  {GMP_FRIENDLY} <span className="text-xs text-zinc-400">· VT</span>
                </button>
              </li>
              {utilMatches.map((p) => (
                <li key={p.provider}>
                  <button
                    type="button"
                    onClick={() => {
                      setUtil({ provider: p.provider, label: p.name, host: p.host });
                      setPickingUtil(false);
                    }}
                    className="w-full rounded-md px-2 py-1.5 text-left text-sm text-zinc-800 hover:bg-primary-50"
                  >
                    {p.name} <span className="text-xs text-zinc-400">· {p.hint}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* Credentials */}
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
          Store this login securely on our servers so we can pull {util.label}
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

/**
 * LocusAddLoginForm — the login → client → arrays onboarding for a Locus
 * (SolarNOC) monitoring login. Unlike a utility login, Locus settles off a
 * site monitor, so the flow is: enter the login → we discover every site under
 * it → the operator confirms which sites are theirs → we create ONE client
 * named from the login and file the picked sites under it as arrays, then pull
 * their generation so the report is ready. (Ford 2026-07-23.)
 */
function LocusAddLoginForm({ onCreated }: { onCreated: () => void }) {
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [clientName, setClientName] = useState("");
  const [sites, setSites] = useState<LocusDiscoveredSite[] | null>(null);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<"discover" | "create" | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function discover() {
    const user = username.trim();
    if (!user) { toast.show("Enter your Locus username.", "info"); return; }
    if (!password) { toast.show("Enter your Locus password.", "info"); return; }
    setBusy("discover");
    setErr(null);
    try {
      const r = await discoverLocus(user, password);
      setSites(r.sites);
      setPicked(new Set(r.sites.map((s) => s.site_id))); // all pre-checked
      if (!clientName.trim()) setClientName(nameFromLogin(user));
      if (!r.sites.length) setErr(r.message || "No sites found under this login.");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Couldn't reach Locus with that login.");
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
    const name = clientName.trim() || nameFromLogin(username);
    if (!picked.size) { toast.show("Pick at least one site.", "info"); return; }
    setBusy("create");
    setErr(null);
    try {
      const client = await createClient({ name });
      const res = await connectLocusAccount(username.trim(), password, {
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

  return (
    <div className="space-y-3">
      <p className="text-sm font-semibold text-zinc-900">Connect a Locus (SolarNOC) login</p>
      <p className="text-xs text-zinc-500">
        We’ll find every site under this login. The login becomes a client and the
        sites you keep become its arrays — ready for generation reports.
      </p>
      <input
        type="text"
        autoComplete="off"
        placeholder="Locus username"
        value={username}
        onChange={(e) => setUsername(e.target.value)}
        className="w-full rounded-lg border border-zinc-200 px-3 py-2 text-sm focus:border-primary-400 focus:outline-none"
      />
      <input
        type="password"
        autoComplete="off"
        placeholder="Locus password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        className="w-full rounded-lg border border-zinc-200 px-3 py-2 text-sm focus:border-primary-400 focus:outline-none"
      />

      {sites === null ? (
        <Button disabled={busy !== null} onClick={discover} className="w-full py-2 text-sm">
          {busy === "discover" ? "Finding sites…" : "Find my sites"}
        </Button>
      ) : (
        <>
          <label className="block text-xs font-medium text-zinc-600">
            Client name
            <input
              type="text"
              value={clientName}
              onChange={(e) => setClientName(e.target.value)}
              className="mt-1 w-full rounded-lg border border-zinc-200 px-3 py-2 text-sm text-zinc-900 focus:border-primary-400 focus:outline-none"
            />
          </label>
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
          <Button disabled={busy !== null || !picked.size} onClick={create} className="w-full py-2 text-sm">
            {busy === "create"
              ? "Creating client…"
              : `Create client with ${picked.size} site${picked.size === 1 ? "" : "s"}`}
          </Button>
          <button
            type="button"
            className="w-full text-xs text-zinc-500 hover:underline"
            onClick={() => { setSites(null); setPicked(new Set()); setErr(null); }}
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
 * AddClientByLoginModal — the "click a portal, sign in, done" flow.
 *
 * Earlier versions of this modal sat operators on a "Watching for sign-in…"
 * babysitting page after picking a portal. That was its own friction
 * surface — Ford called it "the live capture page" — and it added a
 * cognitive layer the operator never needed.
 *
 * New flow:
 *   1. Operator picks GMP or a SmartHub utility (the SmartHub list is loaded
 *      live from /v1/providers, so it scales as we add utilities nationwide).
 *   2. We close the modal immediately and open the portal in a fresh
 *      foreground tab (extension wipes session cookies first).
 *   3. Operator signs in. The extension POSTs /v1/sync. Backend creates
 *      a Client. SO_CAPTURE_LANDED fires globally.
 *   4. A separate <CaptureListener> mounted in ClientsSection toasts
 *      "<Client name> added" and refreshes the list. The operator sees
 *      that toast from the dashboard tab whenever they come back.
 *   5. To add another, the operator clicks "+ Add client" again. Each
 *      click is a discrete decision; no chained-countdown decision tree.
 */
export function AddClientByLoginModal({
  open,
  onClose,
  onCaptured,
  onSwitchToManual,
  cloudMode = false,
}: Props) {
  const toast = useToast();
  // Cloud mode never needs extension probe UI — skip live probe noise.
  const ext = useExtensionStatus(open && !cloudMode);

  // SmartHub portals, loaded live from the provider catalog (rows with a
  // smarthub_host). Falls back to an empty list on error — GMP + the manual
  // path always work regardless.
  const [smarthubPortals, setSmarthubPortals] = useState<SmartHubEntry[]>([]);
  const [portalsLoaded, setPortalsLoaded] = useState(false);
  const [query, setQuery] = useState("");

  // Operator location for "nearest utilities first". null = not requested or
  // denied (we fall back to alphabetical). geoState tracks the UX of the ask.
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

  // Re-probe whenever the modal opens so a freshly-installed extension
  // is detected without a page reload.
  useEffect(() => {
    if (open) void ext.probe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Load the SmartHub portal list once the modal opens (cached after first load).
  useEffect(() => {
    if (!open || portalsLoaded) return;
    let cancelled = false;
    (async () => {
      try {
        const providers = await getProviders();
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
      } catch {
        // Non-fatal: GMP + manual entry still work. Leave the list empty.
      } finally {
        if (!cancelled) setPortalsLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, portalsLoaded]);

  // Filter by the search box first, then rank. When the operator has shared
  // their location, rank by distance to each utility's state centroid (nearest
  // first; unknown-state utilities sink to the bottom). Otherwise keep the
  // alphabetical order the list was built with.
  const filteredPortals = useMemo(() => {
    const q = query.trim().toLowerCase();
    const base = !q
      ? smarthubPortals
      : smarthubPortals.filter(
          (p) =>
            p.name.toLowerCase().includes(q) ||
            p.hint.toLowerCase().includes(q) ||
            p.provider.toLowerCase().includes(q),
        );

    if (!coords) return base.map((p) => ({ ...p, miles: null as number | null }));

    return base
      .map((p) => ({ ...p, miles: milesToState(coords, p.stateCode) }))
      .sort((a, b) => {
        // Unknown distance (no/blank state) always sorts last.
        if (a.miles == null && b.miles == null)
          return a.name.localeCompare(b.name);
        if (a.miles == null) return 1;
        if (b.miles == null) return -1;
        return a.miles - b.miles || a.name.localeCompare(b.name);
      });
  }, [smarthubPortals, query, coords]);

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
        {/* PRIMARY path (Ford 2026-07-16): reuse a login you've already linked,
            + a big button to add more logins. The connect-a-new-portal picker
            is demoted below the divider. */}
        <LinkedLoginsPicker
          onCreated={() => {
            void onCaptured();
            onClose();
          }}
          onAddMore={() => {
            onClose();
            const w = window as { __aoOpenCredentialVault?: (focus?: boolean) => void };
            if (typeof w.__aoOpenCredentialVault === "function") {
              w.__aoOpenCredentialVault(true);
            } else {
              // Fallback: route the host to the Master account tab where the
              // credential vault lives.
              try {
                window.location.hash = "#account";
              } catch {
                /* ignore */
              }
            }
          }}
        />

        {cloudMode ? (
          <>
            <div className="flex items-center gap-3 pt-1">
              <span className="h-px flex-1 bg-cream-border" />
              <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                or add a new login
              </span>
              <span className="h-px flex-1 bg-cream-border" />
            </div>
            <CloudAddLoginForm
              portals={smarthubPortals}
              onCreated={() => {
                void onCaptured();
                onClose();
              }}
            />
          </>
        ) : (
          <>
            <div className="flex items-center gap-3 pt-1">
              <span className="h-px flex-1 bg-cream-border" />
              <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                or connect a new portal
              </span>
              <span className="h-px flex-1 bg-cream-border" />
            </div>
            <ExtensionStatusBanner status={ext.status} version={ext.version} />

            {extensionUsable && (
              <p className="text-sm text-zinc-600">
                Pick the portal your client signs into. We&apos;ll open it in
                a fresh tab — sign in there as the new client, and their
                arrays appear in your dashboard automatically.
              </p>
            )}
            {extensionUnpaired && (
              <p className="text-sm text-zinc-600">
                Your extension is installed but not paired to this account yet.
                You can still pick a portal — after sign-in we&apos;ll fall back
                to a manual refresh.
              </p>
            )}
            {extensionAbsent && (
              <p className="text-sm text-zinc-600">
                Without the extension, auto-capture can&apos;t finish.{" "}
                <b className="text-zinc-900">Add manually instead</b> — it&apos;s
                quick, and the extension will continue capturing data on future logins.
              </p>
            )}
            {extensionUnknown && (
              <p className="text-sm text-zinc-500">
                Checking your extension&hellip;
              </p>
            )}
          </>
        )}

        {/* Locus (SolarNOC) monitoring login → client → arrays. Orthogonal to
            the utility flows above, so it's always offered. */}
        <div className="flex items-center gap-3 pt-1">
          <span className="h-px flex-1 bg-cream-border" />
          <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">
            or connect a solar monitor
          </span>
          <span className="h-px flex-1 bg-cream-border" />
        </div>
        <LocusAddLoginForm
          onCreated={() => {
            void onCaptured();
            onClose();
          }}
        />

        {!cloudMode && (<>
        <div className={extensionAbsent ? "opacity-50" : ""}>
          {/* GMP — its own button, not SmartHub */}
          <PortalCard
            label="Green Mountain Power"
            hint="Most VT solar clients"
            cta="Open GMP portal →"
            onClick={() => pick("gmp", GMP_FRIENDLY)}
            disabled={extensionUnknown}
          />

          {/* SmartHub utilities group — loaded live from /v1/providers so the
              list grows automatically as we add utilities nationwide. */}
          <div className="mt-4">
            <div className="mb-2 flex items-baseline justify-between gap-2">
              <p className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                SmartHub utilities
              </p>
              {smarthubPortals.length > 0 && (
                <span className="text-xs text-zinc-400">
                  {smarthubPortals.length} supported
                </span>
              )}
            </div>

            {/* Nearest-first control. Asks for browser location, then ranks the
                list by distance to each utility's state. Pure convenience —
                denial/absence falls back silently to the alphabetical list. */}
            {smarthubPortals.length > 1 && (
              <div className="mb-2">
                {geoState !== "granted" ? (
                  <button
                    type="button"
                    onClick={requestLocation}
                    disabled={geoState === "prompting"}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-300 px-2.5 py-1.5 text-xs font-medium text-zinc-600 transition-colors hover:border-emerald-400 hover:text-emerald-600 disabled:opacity-60"
                  >
                    <span aria-hidden>📍</span>
                    {geoState === "prompting"
                      ? "Locating…"
                      : "Sort by nearest to me"}
                  </button>
                ) : (
                  <div className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-xs font-medium text-emerald-700">
                    <span aria-hidden>📍</span>
                    Sorted by nearest to you
                  </div>
                )}
                {geoState === "denied" && (
                  <span className="ml-2 text-xs text-zinc-400">
                    Location off — showing A–Z. Search by state instead.
                  </span>
                )}
                {geoState === "unsupported" && (
                  <span className="ml-2 text-xs text-zinc-400">
                    Location isn&apos;t available in this browser.
                  </span>
                )}
              </div>
            )}

            {smarthubPortals.length > 6 && (
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search utilities by name or state…"
                className="mb-2 w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm focus:border-emerald-400 focus:outline-none focus:ring-1 focus:ring-emerald-400"
              />
            )}

            {!portalsLoaded && (
              <p className="text-xs text-zinc-400">Loading supported utilities…</p>
            )}
            {portalsLoaded && smarthubPortals.length === 0 && (
              <p className="text-xs text-zinc-500">
                Couldn&apos;t load the utility list. You can still use GMP above
                or add the client manually.
              </p>
            )}

            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {filteredPortals.map((p) => (
                <PortalCard
                  key={p.provider}
                  label={p.name}
                  hint={
                    p.miles != null
                      ? `${p.hint} · ~${Math.round(p.miles)} mi`
                      : p.hint
                  }
                  cta="Open portal →"
                  onClick={() => pick(p.provider, p.name, p.url)}
                  disabled={extensionUnknown}
                />
              ))}
            </div>
            {portalsLoaded &&
              smarthubPortals.length > 0 &&
              filteredPortals.length === 0 && (
                <p className="mt-1 text-xs text-zinc-500">
                  No supported utility matches &ldquo;{query}&rdquo;. Submit it
                  below and we&apos;ll add it.
                </p>
              )}
          </div>

          {/* Escape hatch: the operator's client uses a utility we don't list
              yet. Submitting routes to a Hermes agent that adds it to the repo. */}
          <SubmitUtilityForm />
        </div>

        <p className="text-xs text-zinc-500">
          <b className="text-zinc-700">Adding multiple clients?</b> Stay in the
          portal tab and sign out, then sign into the next client's account.
          Each sign-in captures another client automatically — you don't need
          to come back here between them.
        </p>
        </>)}
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
      className="group flex flex-col items-start gap-1 rounded-xl border-2 border-zinc-200 bg-white p-4 text-left transition-all hover:border-emerald-400 hover:bg-emerald-50/50 disabled:cursor-not-allowed disabled:opacity-50"
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
 * SubmitUtilityForm — the "Don't see your utility?" escape hatch shown at the
 * bottom of the portal list. Collapsed to a single text button by default
 * (click is tax — don't make operators read a form they rarely need). On
 * expand it collects the utility name + optional portal/region/notes and POSTs
 * to /v1/account/request-utility, which emails the SO team and fires the
 * Hermes agent webhook that adds the utility to the repo and opens a PR.
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

