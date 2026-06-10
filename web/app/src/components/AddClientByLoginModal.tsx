import { useEffect, useState, useMemo } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { useToast } from "../ui/Toast";
import { requestUtilityAddition, getProviders } from "../lib/api";
import {
  useExtensionStatus,
  type ExtensionStatus,
} from "../lib/useExtensionStatus";
import { wipeCookiesAndWait, gmpPortalUrl } from "../lib/openPortalTab";

// A pickable SmartHub portal, derived at runtime from the provider catalog
// (GET /v1/providers → rows with a smarthub_host). The hardcoded list is gone:
// new utilities added to api/data/providers/*.csv appear here automatically.
interface SmartHubEntry {
  provider: string;
  name: string;
  hint: string;
  url: string;
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
}

const GMP_FRIENDLY = "Green Mountain Power";

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
}: Props) {
  const toast = useToast();
  const ext = useExtensionStatus(open);

  // SmartHub portals, loaded live from the provider catalog (rows with a
  // smarthub_host). Falls back to an empty list on error — GMP + the manual
  // path always work regardless.
  const [smarthubPortals, setSmarthubPortals] = useState<SmartHubEntry[]>([]);
  const [portalsLoaded, setPortalsLoaded] = useState(false);
  const [query, setQuery] = useState("");

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
            url: p.portal_url || `https://${p.smarthub_host}/`,
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

  const filteredPortals = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return smarthubPortals;
    return smarthubPortals.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.hint.toLowerCase().includes(q) ||
        p.provider.toLowerCase().includes(q),
    );
  }, [smarthubPortals, query]);

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
    (async () => {
      console.log(`[SO ${Date.now()}] add-login: wipe-start for ${host}`);
      await wipeCookiesAndWait(host);
      console.log(`[SO ${Date.now()}] add-login: wipe-done (+${Date.now() - t0}ms), navigating tab`);
      try {
        newTab.location.href = url;
        console.log(`[SO ${Date.now()}] add-login: tab.location.href set → ${url}`);
      } catch (e) {
        // Should not happen while tab is on about:blank but log if it does.
        console.error("[SO] could not navigate new tab:", e);
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
                  hint={p.hint}
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

