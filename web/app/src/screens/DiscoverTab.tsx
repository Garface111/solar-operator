import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { lazyWithRetry } from "../lib/lazyWithRetry";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { SectionTitle } from "../ui/SectionTitle";
import { useToast } from "../ui/Toast";
import { useDashboardContext } from "./DashboardLayout";
import {
  type ClientRow,
  type DiscoveryCandidate,
  type DiscoveryLoginGroup,
  type DiscoveryPool,
  UnauthorizedError,
  importDiscoveryCandidates,
  listClients,
  listDiscoveryCandidates,
  refreshDiscovery,
  setDiscoveryIgnored,
} from "../lib/api";

const AddClientByLoginModal = lazyWithRetry(() =>
  import("../components/AddClientByLoginModal").then((m) => ({
    default: m.AddClientByLoginModal,
  })),
);

// ─── helpers ─────────────────────────────────────────────────────────────────

/** "3 hours ago" / "just now". Coarse on purpose — the pool refreshes nightly,
 *  so minute-level precision would be noise. */
function relativeTime(iso: string | null): string | null {
  if (!iso) return null;
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return null;
  const mins = Math.round(ms / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function kwLabel(kw: number | null): string | null {
  if (kw == null) return null;
  return `${kw % 1 === 0 ? kw : kw.toFixed(1)} kW`;
}

// ─── Skeleton ────────────────────────────────────────────────────────────────

function GroupSkeleton() {
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm">
      <div className="h-4 w-44 animate-pulse rounded bg-zinc-200" />
      <div className="mt-2 h-3 w-64 animate-pulse rounded bg-zinc-100" />
      <div className="mt-4 space-y-2">
        <div className="h-8 w-full animate-pulse rounded bg-zinc-50" />
        <div className="h-8 w-full animate-pulse rounded bg-zinc-50" />
      </div>
    </div>
  );
}

// ─── Candidate row ───────────────────────────────────────────────────────────

function CandidateRow({
  candidate,
  selected,
  onToggle,
  onUnignore,
}: {
  candidate: DiscoveryCandidate;
  selected: boolean;
  onToggle: () => void;
  onUnignore: () => void;
}) {
  const isNew = candidate.status === "new";
  const kw = kwLabel(candidate.peak_power_kw);

  return (
    <li
      className={[
        "flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-2 py-2 transition-colors",
        isNew ? "hover:bg-zinc-50" : "",
      ].join(" ")}
    >
      <input
        type="checkbox"
        checked={selected}
        disabled={!isNew}
        onChange={onToggle}
        aria-label={`Select ${candidate.name}`}
        className={[
          "h-4 w-4 shrink-0 rounded border-zinc-300 text-primary-500",
          "transition-colors duration-150 ease-in-out",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2",
          "disabled:cursor-not-allowed disabled:opacity-40",
        ].join(" ")}
      />
      <span
        className={[
          "min-w-0 flex-1 truncate text-sm",
          candidate.status === "ignored"
            ? "text-zinc-400 line-through"
            : "text-zinc-800",
        ].join(" ")}
        title={candidate.external_id}
      >
        {candidate.name}
      </span>
      {kw && (
        <span className="shrink-0 text-xs tabular-nums text-zinc-500">{kw}</span>
      )}
      {candidate.status === "imported" && (
        <span className="flex shrink-0 items-center gap-1.5">
          <span className="rounded-full bg-primary-50 px-2 py-0.5 text-xs font-medium text-primary-700">
            In your system
          </span>
          {candidate.imported_client_name && (
            <span className="text-xs text-zinc-500">
              {candidate.imported_client_name}
              {/* The roster hides retired clients, so name them as retired
                  rather than pointing at a client with no row in the table. */}
              {candidate.imported_client_active === false && " (retired)"}
            </span>
          )}
        </span>
      )}
      {candidate.status === "ignored" && (
        <button
          type="button"
          onClick={onUnignore}
          className="shrink-0 rounded text-xs font-medium text-zinc-500 underline-offset-2 hover:text-zinc-800 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
        >
          Un-ignore
        </button>
      )}
    </li>
  );
}

// ─── Login group card ────────────────────────────────────────────────────────

function LoginGroup({
  group,
  collapsed,
  onToggleCollapsed,
  showIgnored,
  onToggleShowIgnored,
  selectedIds,
  onToggleCandidate,
  onSelectAllNew,
  onClearGroup,
  onUnignore,
}: {
  group: DiscoveryLoginGroup;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  showIgnored: boolean;
  onToggleShowIgnored: () => void;
  selectedIds: Set<number>;
  onToggleCandidate: (id: number) => void;
  onSelectAllNew: () => void;
  onClearGroup: () => void;
  onUnignore: (id: number) => void;
}) {
  const visible = group.candidates.filter(
    (c) => c.status !== "ignored" || showIgnored,
  );
  const seen = relativeTime(group.last_seen_at);

  return (
    <div className="rounded-xl border border-zinc-200 bg-white shadow-sm">
      <button
        type="button"
        onClick={onToggleCollapsed}
        aria-expanded={!collapsed}
        className="flex w-full flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-xl px-5 py-4 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
      >
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-sm font-semibold text-zinc-900">
            <span
              aria-hidden
              className={[
                "text-zinc-400 transition-transform",
                collapsed ? "" : "rotate-90",
              ].join(" ")}
            >
              ▸
            </span>
            {group.provider_label}
          </p>
          <p className="mt-0.5 truncate pl-5 text-xs text-zinc-500">
            {group.login}
            {seen && ` · checked ${seen}`}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2 text-xs">
          {group.counts.new > 0 && (
            <span className="rounded-full bg-primary-50 px-2 py-0.5 font-medium text-primary-700">
              {group.counts.new} new
            </span>
          )}
          <span className="text-zinc-500">
            {group.counts.imported} in your system
          </span>
          {group.counts.ignored > 0 && (
            <span className="text-zinc-400">{group.counts.ignored} ignored</span>
          )}
        </div>
      </button>

      {/* An expired password can't fix itself — say so where the login lives. */}
      {group.last_error && (
        <p className="mx-5 mb-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          {group.last_error}
        </p>
      )}

      {!collapsed && (
        <div className="border-t border-zinc-100 px-5 py-3">
          <div className="mb-1 flex flex-wrap items-center justify-between gap-2 text-xs">
            <div className="flex items-center gap-3">
              {group.counts.new > 0 && (
                <button
                  type="button"
                  onClick={onSelectAllNew}
                  className="font-medium text-primary-700 underline-offset-2 hover:underline focus:outline-none"
                >
                  Select all new
                </button>
              )}
              <button
                type="button"
                onClick={onClearGroup}
                className="text-zinc-500 underline-offset-2 hover:text-zinc-800 hover:underline focus:outline-none"
              >
                Clear
              </button>
            </div>
            {group.counts.ignored > 0 && (
              <button
                type="button"
                onClick={onToggleShowIgnored}
                className="text-zinc-500 underline-offset-2 hover:text-zinc-800 hover:underline focus:outline-none"
              >
                {showIgnored
                  ? "Hide ignored"
                  : `Show ignored (${group.counts.ignored})`}
              </button>
            )}
          </div>

          {visible.length === 0 ? (
            <p className="py-2 text-sm text-zinc-500">
              Nothing to sort from this login right now.
            </p>
          ) : (
            <ul className="divide-y divide-zinc-100">
              {visible.map((c) => (
                <CandidateRow
                  key={c.id}
                  candidate={c}
                  selected={selectedIds.has(c.id)}
                  onToggle={() => onToggleCandidate(c.id)}
                  onUnignore={() => onUnignore(c.id)}
                />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Component ───────────────────────────────────────────────────────────────

/**
 * Discover — the staging pool between "a login can see it" and "it's mine".
 *
 * One vendor login (a Locus PARTNER account is the extreme case) can read
 * sites belonging to several different operators. Importing everything it
 * sees put other people's arrays in the wrong tenant, so nothing crosses over
 * on its own: every candidate sits here until the operator picks it. Adding a
 * login is a one-time act; this pool is where it keeps paying off.
 */
export default function DiscoverTab() {
  const toast = useToast();
  const { account } = useDashboardContext();

  const [pool, setPool] = useState<DiscoveryPool | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [clients, setClients] = useState<ClientRow[] | null>(null);

  const [refreshing, setRefreshing] = useState(false);
  // Result of the last manual refresh ("Checked 3 logins, found 41 sites").
  const [refreshNote, setRefreshNote] = useState<string | null>(null);

  const [addingByLogin, setAddingByLogin] = useState(false);

  // Curation state. Selection spans groups (one client target for the batch);
  // collapse + show-ignored are per group.
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [ignoredShown, setIgnoredShown] = useState<Set<string>>(new Set());

  // Where the selection lands: an existing client, or one created on the spot.
  const [targetMode, setTargetMode] = useState<"existing" | "new">("existing");
  const [targetClientId, setTargetClientId] = useState<string>("");
  const [newClientName, setNewClientName] = useState("");
  const [busy, setBusy] = useState<"import" | "ignore" | null>(null);

  const loadPool = useCallback(async (): Promise<DiscoveryPool | null> => {
    try {
      const p = await listDiscoveryCandidates();
      setPool(p);
      setLoadError(null);
      return p;
    } catch (err) {
      // A 401 already bounces to login globally — don't stack a second error on
      // top of the session-expiry message.
      if (err instanceof UnauthorizedError) return null;
      setLoadError(
        err instanceof Error ? err.message : "Couldn't load what we can see.",
      );
      return null;
    }
  }, []);

  useEffect(() => {
    void loadPool();
    listClients()
      .then(setClients)
      .catch((err) => {
        if (err instanceof UnauthorizedError) return;
        setClients([]);
      });
  }, [loadPool]);

  const activeClients = useMemo(
    () => (clients ?? []).filter((c) => c.active),
    [clients],
  );

  const groups = pool?.logins ?? [];

  // The client name the server guessed for the first selected candidate —
  // prefills "New client…" so the common case is one click, not typing.
  const suggestedName = useMemo(() => {
    for (const g of groups) {
      for (const c of g.candidates) {
        if (selectedIds.has(c.id) && c.suggested_client) return c.suggested_client;
      }
    }
    return "";
  }, [groups, selectedIds]);

  function toggleCandidate(id: number) {
    setSelectedIds((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  function toggleInSet(
    set: Set<string>,
    setter: (next: Set<string>) => void,
    key: string,
  ) {
    const n = new Set(set);
    if (n.has(key)) n.delete(key);
    else n.add(key);
    setter(n);
  }

  function selectAllNew(group: DiscoveryLoginGroup) {
    setSelectedIds((s) => {
      const n = new Set(s);
      group.candidates.forEach((c) => {
        if (c.status === "new") n.add(c.id);
      });
      return n;
    });
  }

  function clearGroup(group: DiscoveryLoginGroup) {
    setSelectedIds((s) => {
      const n = new Set(s);
      group.candidates.forEach((c) => n.delete(c.id));
      return n;
    });
  }

  async function handleRefresh() {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshNote(null);
    try {
      const res = await refreshDiscovery();
      await loadPool();
      setRefreshNote(
        `Checked ${res.refreshed} login${res.refreshed === 1 ? "" : "s"}, ` +
          `found ${res.found} site${res.found === 1 ? "" : "s"}.`,
      );
      // Per-login failures don't fail the call — surface them so a stale
      // password gets re-entered instead of quietly starving the pool.
      if (res.errors.length > 0) {
        toast.warning(
          `${res.errors.length} login${res.errors.length === 1 ? "" : "s"} couldn't be checked — see the note on each one.`,
        );
      }
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        toast.error(
          err instanceof Error ? err.message : "Couldn't check your logins.",
        );
      }
    } finally {
      setRefreshing(false);
    }
  }

  async function handleImport() {
    if (busy || selectedIds.size === 0) return;
    const name = newClientName.trim();
    if (targetMode === "existing" && !targetClientId) {
      toast.error("Pick which client these belong to.");
      return;
    }
    if (targetMode === "new" && !name) {
      toast.error("Give the new client a name.");
      return;
    }
    setBusy("import");
    try {
      const res = await importDiscoveryCandidates({
        candidateIds: Array.from(selectedIds),
        ...(targetMode === "existing"
          ? { clientId: Number(targetClientId) }
          : { clientName: name }),
      });
      toast.success(res.message);
      setSelectedIds(new Set());
      setNewClientName("");
      setTargetMode("existing");
      await loadPool();
      // A brand-new client has to appear in the target picker straight away.
      listClients().then(setClients).catch(() => { /* keep the stale list */ });
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        toast.error(
          err instanceof Error ? err.message : "Couldn't add those to your system.",
        );
      }
    } finally {
      setBusy(null);
    }
  }

  async function handleIgnore() {
    if (busy || selectedIds.size === 0) return;
    const n = selectedIds.size;
    setBusy("ignore");
    try {
      await setDiscoveryIgnored(Array.from(selectedIds), true);
      setSelectedIds(new Set());
      await loadPool();
      toast.success(`Set ${n} aside.`);
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        toast.error(err instanceof Error ? err.message : "Couldn't ignore those.");
      }
    } finally {
      setBusy(null);
    }
  }

  async function handleUnignore(id: number) {
    try {
      await setDiscoveryIgnored([id], false);
      await loadPool();
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        toast.error(err instanceof Error ? err.message : "Couldn't un-ignore that.");
      }
    }
  }

  const updated = relativeTime(pool?.refreshed_at ?? null);
  const totalNew = groups.reduce((sum, g) => sum + g.counts.new, 0);

  const addLoginButton = (
    <Button
      variant="secondary"
      onClick={() => setAddingByLogin(true)}
      className="px-3 py-2 text-xs sm:px-4 sm:text-sm"
    >
      + Add a login
    </Button>
  );

  return (
    <section className="relative mx-auto w-full max-w-4xl">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <SectionTitle
            title="Discover"
            subtitle="Everything we can see from your connected logins. Pick what belongs in your system."
          />
          <p className="mt-1 text-xs text-zinc-400">
            {updated ? `Updated ${updated}` : "Not checked yet"}
            {refreshNote && ` · ${refreshNote}`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {addLoginButton}
          <Button
            onClick={() => void handleRefresh()}
            disabled={refreshing}
            className="px-3 py-2 text-xs sm:px-4 sm:text-sm"
          >
            {refreshing ? (
              <>
                <Spinner /> Checking…
              </>
            ) : (
              "Refresh"
            )}
          </Button>
        </div>
      </div>

      {pool === null && loadError === null ? (
        <div className="space-y-3">
          <GroupSkeleton />
          <GroupSkeleton />
        </div>
      ) : loadError ? (
        <Card>
          <p className="text-sm font-medium text-red-600">{loadError}</p>
          <div className="pt-3">
            <Button variant="secondary" onClick={() => void loadPool()}>
              Try again
            </Button>
          </div>
        </Card>
      ) : groups.length === 0 ? (
        <Card>
          <div className="space-y-3">
            <h3 className="text-base font-semibold text-zinc-900">
              Nothing connected yet
            </h3>
            <p className="text-sm text-zinc-600">
              Add one utility or vendor login and we&apos;ll show you every site
              it can see. You pick which ones belong to you — nothing enters
              your system until you say so. You only enter a login once.
            </p>
            <div className="pt-1">{addLoginButton}</div>
          </div>
        </Card>
      ) : totalNew === 0 ? (
        <div className="space-y-3">
          <Card>
            <p className="text-sm text-zinc-600">
              Everything we can see is already sorted.
            </p>
          </Card>
          {groups.map((g) => (
            <LoginGroup
              key={g.key}
              group={g}
              collapsed={collapsed.has(g.key)}
              onToggleCollapsed={() => toggleInSet(collapsed, setCollapsed, g.key)}
              showIgnored={ignoredShown.has(g.key)}
              onToggleShowIgnored={() =>
                toggleInSet(ignoredShown, setIgnoredShown, g.key)
              }
              selectedIds={selectedIds}
              onToggleCandidate={toggleCandidate}
              onSelectAllNew={() => selectAllNew(g)}
              onClearGroup={() => clearGroup(g)}
              onUnignore={(id) => void handleUnignore(id)}
            />
          ))}
        </div>
      ) : (
        <div className="space-y-3">
          {groups.map((g) => (
            <LoginGroup
              key={g.key}
              group={g}
              collapsed={collapsed.has(g.key)}
              onToggleCollapsed={() => toggleInSet(collapsed, setCollapsed, g.key)}
              showIgnored={ignoredShown.has(g.key)}
              onToggleShowIgnored={() =>
                toggleInSet(ignoredShown, setIgnoredShown, g.key)
              }
              selectedIds={selectedIds}
              onToggleCandidate={toggleCandidate}
              onSelectAllNew={() => selectAllNew(g)}
              onClearGroup={() => clearGroup(g)}
              onUnignore={(id) => void handleUnignore(id)}
            />
          ))}
        </div>
      )}

      {/* Sticky curation bar — the one place the batch gets a home. */}
      {selectedIds.size > 0 && (
        <div
          className="sticky bottom-4 mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-zinc-200 bg-white px-5 py-3 shadow-lg"
          style={{ marginBottom: "env(safe-area-inset-bottom, 0px)" }}
        >
          <span className="text-sm text-zinc-600">
            {selectedIds.size} selected
          </span>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-2 text-xs text-zinc-600">
              <span className="font-semibold uppercase tracking-wide">Client</span>
              <select
                value={targetMode === "new" ? "__new__" : targetClientId}
                onChange={(e) => {
                  if (e.target.value === "__new__") {
                    setTargetMode("new");
                    setNewClientName((n) => n || suggestedName);
                  } else {
                    setTargetMode("existing");
                    setTargetClientId(e.target.value);
                  }
                }}
                className="rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm text-zinc-800 focus:border-primary-400 focus:outline-none"
              >
                <option value="">Pick a client…</option>
                {activeClients.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
                <option value="__new__">＋ New client…</option>
              </select>
            </label>
            {targetMode === "new" && (
              <input
                type="text"
                value={newClientName}
                onChange={(e) => setNewClientName(e.target.value)}
                placeholder="New client name"
                aria-label="New client name"
                className="rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-800 focus:border-primary-400 focus:outline-none"
              />
            )}
            <Button
              variant="secondary"
              onClick={() => void handleIgnore()}
              disabled={busy !== null}
              className="px-3 py-2 text-sm"
            >
              {busy === "ignore" ? (
                <>
                  <Spinner /> Ignoring…
                </>
              ) : (
                "Ignore"
              )}
            </Button>
            <Button
              onClick={() => void handleImport()}
              disabled={busy !== null}
              className="px-4 py-2 text-sm"
            >
              {busy === "import" ? (
                <>
                  <Spinner /> Adding…
                </>
              ) : (
                "Add to my system"
              )}
            </Button>
          </div>
        </div>
      )}

      {addingByLogin && (
        <Suspense fallback={null}>
          <AddClientByLoginModal
            open={addingByLogin}
            cloudMode={account?.capture_mode === "cloud"}
            onClose={() => {
              setAddingByLogin(false);
              // A freshly-saved login usually has candidates waiting already.
              void loadPool();
            }}
            onCaptured={async () => {
              void loadPool();
              const rows = await listClients();
              setClients(rows);
              return rows;
            }}
            onSwitchToManual={() => setAddingByLogin(false)}
          />
        </Suspense>
      )}
    </section>
  );
}
