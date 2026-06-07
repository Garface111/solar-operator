import { lazyWithRetry } from "../../lib/lazyWithRetry";
import { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  Panel,
  ReactFlow,
  useNodesState,
  useReactFlow,
  type Node,
  type NodeTypes,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { CanvasActionsContext, type CanvasActions, type Density } from './canvasContext';
import { ClientNodeComponent, type ClientNodeData } from './ClientNode';
import { UnclassifiedNodeComponent, type UnclassifiedNodeData } from './UnclassifiedAccountNode';
import { type ClientData, type UtilityAccount, type Utility, clientArrayCount } from './mockData';
import {
  getCanvasData,
  mergeClientInto,
  patchCanvasPositions,
  pinClient,
  reassignAccount,
  reassignArray,
  updateClient,
  updateArray,
  createClient,
  deleteClient,
  undoDelete,
  listClients,
  getSession,
  clearSession,
  UNAUTHORIZED_EVENT,
  type CanvasResponse,
} from '../../lib/api';
import { useToast } from '../../ui/Toast';
import { Spinner } from '../../ui/Spinner';

const AddClientByLoginModal = lazyWithRetry(() =>
  import('../AddClientByLoginModal').then((m) => ({ default: m.AddClientByLoginModal })),
);
import { CommandPalette } from './CommandPalette';
import { DevPanel } from './DevPanel';
import { SandboxWalkthrough } from './SandboxWalkthrough';

// ── Node type registry (stable reference — must live outside component) ────

const NODE_TYPES: NodeTypes = {
  client: ClientNodeComponent,
  unclassified: UnclassifiedNodeComponent,
};

// ── Layout types ────────────────────────────────────────────────────────────

export type LayoutMode = 'sorted' | 'free';
export type SortKey = 'name' | 'recent' | 'arrays' | 'pinned';

// ── Layout constants ────────────────────────────────────────────────────────

const GRID: Record<Density, { COL_W: number; ROW_H: number }> = {
  full:    { COL_W: 330, ROW_H: 295 },
  compact: { COL_W: 250, ROW_H: 200 },
  dense:   { COL_W: 190, ROW_H:  90 },
};

// Columns per density tier — used by layout functions even though density is
// always 'full' at runtime.
const DENSITY_COLS: Record<Density, number> = { full: 4, compact: 5, dense: 7 };

// ── Provider normalizer ─────────────────────────────────────────────────────

function normalizeProvider(p: string): Utility {
  const u = p.toUpperCase() as Utility;
  return (['GMP', 'VEC', 'WEC'] as Utility[]).includes(u) ? u : 'GMP';
}

// ── Sorted position computation ─────────────────────────────────────────────
// These are PURE functions — positions are computed from sort rank, never
// stored. This makes position randomization architecturally impossible in
// sorted mode: the DB's canvas_x/canvas_y become irrelevant.

function computeSortedPositionsFromApiClients(
  clients: CanvasResponse['clients'],
  sortKey: SortKey,
  density: Density,
): Map<number, { x: number; y: number }> {
  const cols = DENSITY_COLS[density];
  const { COL_W, ROW_H } = GRID[density];
  const sorted = [...clients].sort((a, b) => {
    switch (sortKey) {
      case 'name': return a.name.localeCompare(b.name);
      case 'recent': return b.id - a.id;
      case 'arrays': {
        const ca = a.accounts.filter((acc) => acc.array_name != null).length;
        const cb = b.accounts.filter((acc) => acc.array_name != null).length;
        return cb - ca;
      }
      case 'pinned':
        if (a.canvas_pinned !== b.canvas_pinned) return a.canvas_pinned ? -1 : 1;
        return a.name.localeCompare(b.name);
      default: return 0;
    }
  });
  const map = new Map<number, { x: number; y: number }>();
  sorted.forEach((client, rank) => {
    map.set(client.id, {
      x: (rank % cols) * COL_W + 40,
      y: Math.floor(rank / cols) * ROW_H + 40,
    });
  });
  return map;
}

function computeSortedPositionsFromNodes(
  clientNodes: Node[],
  sortKey: SortKey,
  density: Density,
): Map<string, { x: number; y: number }> {
  const cols = DENSITY_COLS[density];
  const { COL_W, ROW_H } = GRID[density];
  const sorted = [...clientNodes].sort((a, b) => {
    const ad = (a.data as ClientNodeData).client;
    const bd = (b.data as ClientNodeData).client;
    switch (sortKey) {
      case 'name': return ad.name.localeCompare(bd.name);
      case 'recent': {
        const an = parseInt(a.id.replace('client_', ''), 10);
        const bn = parseInt(b.id.replace('client_', ''), 10);
        return bn - an;
      }
      case 'arrays': return clientArrayCount(bd) - clientArrayCount(ad);
      case 'pinned':
        if (!!ad.pinned !== !!bd.pinned) return ad.pinned ? -1 : 1;
        return ad.name.localeCompare(bd.name);
      default: return 0;
    }
  });
  const map = new Map<string, { x: number; y: number }>();
  sorted.forEach((node, rank) => {
    map.set(node.id, {
      x: (rank % cols) * COL_W + 40,
      y: Math.floor(rank / cols) * ROW_H + 40,
    });
  });
  return map;
}

// ── API → React Flow nodes ──────────────────────────────────────────────────

function buildNodesFromApi(
  data: CanvasResponse,
  layoutMode: LayoutMode,
  sortedPositions: Map<number, { x: number; y: number }>,
  density: Density,
  initiallyExpanded: boolean = false,
): Node[] {
  const nodes: Node[] = [];
  const { COL_W } = GRID[density];
  const cols = DENSITY_COLS[density];
  let autoAccIdx = 0;

  data.clients.forEach((client, i) => {
    const clientData: ClientData = {
      id: `client_${client.id}`,
      name: client.name,
      contact_email: client.contact_email ?? null,
      logins: client.logins as Partial<Record<Utility, string | null>> | undefined,
      pinned: client.canvas_pinned ?? false,
      accounts: client.accounts.map((acc) => ({
        id: `account_${acc.id}`,
        utility: normalizeProvider(acc.provider),
        account_number: acc.account_number,
        customer_number: acc.customer_number ?? null,
        owner_name: (acc.service_address as Record<string, string> | null)?.street ?? '',
        login_origin_client_id: acc.login_origin_client_id ?? null,
        arrays: acc.array_name != null
          ? [{
              id: acc.array_id != null ? `arr_${acc.array_id}` : `arr_u_${acc.id}`,
              name: acc.array_name,
              nepool_gis_id: acc.nepool_gis_id ?? '',
              mwh_per_qtr: 0,
              reassigned_at: (acc as unknown as Record<string, unknown>).array_reassigned_at as string | null ?? null,
              deleted_at: acc.array_deleted_at ?? null,
            }]
          : [],
      })),
    };

    // Sorted mode: position is ALWAYS computed from sort rank — DB coords ignored.
    // Free mode: use saved backend coords if present, else fall back to sorted slot.
    let position: { x: number; y: number };
    if (layoutMode === 'sorted') {
      position = sortedPositions.get(client.id) ?? { x: 40, y: 40 };
    } else {
      if (client.canvas_x != null && client.canvas_y != null) {
        position = { x: client.canvas_x, y: client.canvas_y };
      } else {
        position = sortedPositions.get(client.id) ?? { x: 40, y: 40 };
      }
    }

    nodes.push({
      id: `client_${client.id}`,
      type: 'client',
      position,
      draggable: layoutMode === 'free',
      data: { client: clientData, expanded: initiallyExpanded, entryDelay: i * 30 } as ClientNodeData,
    });
  });

  data.unclassified.forEach((acc, i) => {
    const accountData: UtilityAccount = {
      id: `account_${acc.id}`,
      utility: normalizeProvider(acc.provider),
      account_number: acc.account_number,
      owner_name: (acc.service_address as Record<string, string> | null)?.street ?? '',
      arrays: acc.array_name != null
        ? [{
            id: acc.array_id != null ? `arr_${acc.array_id}` : `arr_u_${acc.id}`,
            name: acc.array_name,
            nepool_gis_id: acc.nepool_gis_id ?? '',
            mwh_per_qtr: 0,
            reassigned_at: (acc as unknown as Record<string, unknown>).array_reassigned_at as string | null ?? null,
            deleted_at: acc.array_deleted_at ?? null,
          }]
        : [],
    };

    const hasPos = acc.canvas_x != null && acc.canvas_y != null;
    let position: { x: number; y: number };
    if (hasPos) {
      position = { x: acc.canvas_x!, y: acc.canvas_y! };
    } else {
      const idx = autoAccIdx++;
      position = { x: cols * COL_W + 80, y: idx * 240 + 40 };
    }

    nodes.push({
      id: `account_${acc.id}`,
      type: 'unclassified',
      position,
      data: { account: accountData, entryDelay: (data.clients.length + i) * 30 } as UnclassifiedNodeData,
    });
  });

  return nodes;
}

// ── State types ─────────────────────────────────────────────────────────────

interface MergeDialog {
  sourceId: string;
  targetId: string;
  sourceName: string;
  targetName: string;
  /** Pre-drag position of the source node so cancelling the dialog can
   *  snap the dragged card back instead of leaving it overlapping the
   *  target. */
  sourceOrigin: { x: number; y: number };
}

interface ContextMenu {
  x: number;
  y: number;
  nodeId: string;
  nodeType: string;
}

interface UndoEntry {
  label: string;
  undo: () => void;
  redo?: () => void;
  timestamp: number;
}

// ── Component ───────────────────────────────────────────────────────────────

export default function SandboxCanvas() {
  const toast = useToast();
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [mergeDialog, setMergeDialog] = useState<MergeDialog | null>(null);
  const [undoStack, setUndoStack] = useState<UndoEntry[]>([]);
  const [redoStack, setRedoStack] = useState<UndoEntry[]>([]);
  const [renamingNodeId, setRenamingNodeId] = useState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<ContextMenu | null>(null);
  const [showAddByLogin, setShowAddByLogin] = useState(false);
  const [originLookup, setOriginLookup] = useState<NonNullable<CanvasResponse['clients_index']>>({});
  const [lastCapturedClientId, setLastCapturedClientId] = useState<number | null>(null);

  // SSE status state kept for internal tracking — UI indicator removed
  // Jun 6, but the setter is still called in 7+ places to record reconnect
  // attempts (preserved so we can re-surface the pill in DevPanel later).
  // Read side is intentionally unused; `_sseStatus` silences strict-tsc.
  const [_sseStatus, setSseStatus] = useState<'connected' | 'reconnecting' | 'disconnected'>('disconnected');
  void _sseStatus;

  const density: Density = 'full';

  const [layoutMode, setLayoutMode] = useState<LayoutMode>(() => {
    try {
      const saved = localStorage.getItem('so:sandbox:layout-mode');
      if (saved === 'sorted' || saved === 'free') return saved as LayoutMode;
    } catch { /* ignore */ }
    return 'sorted';
  });

  const [sortKey, setSortKey] = useState<SortKey>(() => {
    try {
      const saved = localStorage.getItem('so:sandbox:sort');
      if (['name', 'recent', 'arrays', 'pinned'].includes(saved ?? '')) return saved as SortKey;
    } catch { /* ignore */ }
    return 'name';
  });

  const { getIntersectingNodes, fitView, setCenter } = useReactFlow();

  // Always-fresh refs used in callbacks to avoid stale closures
  const densityRef = useRef<Density>(density);
  densityRef.current = density;
  const layoutModeRef = useRef<LayoutMode>(layoutMode);
  layoutModeRef.current = layoutMode;
  const sortKeyRef = useRef<SortKey>(sortKey);
  sortKeyRef.current = sortKey;

  // SSE: ref so the connection effect can abort cleanly on unmount.
  const sseAbortRef = useRef<AbortController | null>(null);
  // SSE: timestamp of the last so:capture-cleared event — used to de-dupe
  // SSE toasts from toasts already fired by CaptureListener in the same tab.
  const recentCaptureClearedRef = useRef<number>(0);

  // Pre-drag node positions captured at drag start so we can snap a node
  // back if a merge dialog gets cancelled.
  const dragOriginRef = useRef<Map<string, { x: number; y: number }>>(new Map());

  const nodesRef = useRef<Node[]>(nodes);
  nodesRef.current = nodes;

  const undoStackRef = useRef<UndoEntry[]>(undoStack);
  undoStackRef.current = undoStack;
  const redoStackRef = useRef<UndoEntry[]>(redoStack);
  redoStackRef.current = redoStack;

  // Per-node debounce timers for position persistence (free mode only)
  const posTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const pendingPosRef = useRef<Map<string, { x: number; y: number }>>(new Map());

  // Snapshot of client IDs before the portal-picker modal opens
  const clientIdsBeforeModal = useRef<Set<number>>(new Set());

  // Esc closes the context menu
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setContextMenu(null);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  // ── Data loading ──────────────────────────────────────────────────────────

  const loadCanvas = useCallback(async (opts: { silent?: boolean; sseReveal?: boolean } = {}) => {
    setLoadError(null);
    if (!opts.silent && !opts.sseReveal) setLoading(true);
    // Capture pre-load node IDs so newly added nodes can keep their reveal
    // animation while existing nodes get entryDelay:0 (no flicker on refresh).
    const prevIds = opts.sseReveal ? new Set(nodesRef.current.map((n) => n.id)) : null;
    try {
      const data = await getCanvasData();
      const effectiveDensity: Density = 'full';
      // Positions are computed from sort rank — never rely on DB canvas_x/canvas_y in sorted mode
      const sortedPositions = computeSortedPositionsFromApiClients(
        data.clients,
        sortKeyRef.current,
        effectiveDensity,
      );
      // Bruce Jun 6: sandbox client cards land fully unfolded so arrays are
      // immediately visible (the whole point of the canvas). Operators can
      // collapse individual cards via the chevron; we preserve those choices
      // across SSE refreshes by reading the prior expanded state from the
      // in-memory node graph keyed by node id. Brand-new cards default true.
      const priorExpanded = new Map<string, boolean>();
      for (const n of nodesRef.current) {
        if (n.type === 'client' && n.data && typeof n.data === 'object') {
          const d = n.data as ClientNodeData;
          if (typeof d.expanded === 'boolean') priorExpanded.set(n.id, d.expanded);
        }
      }
      const built = buildNodesFromApi(data, layoutModeRef.current, sortedPositions, effectiveDensity, true)
        .map((n) => {
          if (n.type !== 'client' || !n.data || typeof n.data !== 'object') return n;
          const prior = priorExpanded.get(n.id);
          if (prior === undefined) return n; // brand-new → keep default (true)
          return { ...n, data: { ...(n.data as ClientNodeData), expanded: prior } };
        });
      const finalNodes = (opts.silent || opts.sseReveal)
        ? built.map((n) => {
            if (!n.data || typeof n.data !== 'object') return n;
            // sseReveal: new nodes keep their entryDelay (animation plays);
            // existing nodes get 0 (no re-animation on silent refresh).
            const isNew = opts.sseReveal && prevIds != null && !prevIds.has(n.id);
            return isNew
              ? n
              : { ...n, data: { ...n.data, entryDelay: 0 } as typeof n.data };
          })
        : built;
      setNodes(finalNodes);
      setOriginLookup(data.clients_index ?? {});
      // Notify peers (e.g. the ClientsTable below) that canvas state changed
      // so they can refetch without polling. Fires AFTER nodes are committed.
      window.dispatchEvent(new CustomEvent('so:sandbox:mutated'));
      // Bruce Jun 6: always center cards on the initial load (non-silent),
      // regardless of any previously persisted viewport. The "Fit to view"
      // toolbar button is one click away if the operator wants to recenter
      // again after panning. Silent SSE refreshes still respect the user's
      // current viewport — we only re-center on the first paint of this
      // canvas instance.
      if (built.length > 0 && !opts.silent) {
        setTimeout(() => fitView({ padding: 0.35, duration: 300, maxZoom: 0.85 }), 80);
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Failed to load canvas');
    } finally {
      if (!opts.silent) setLoading(false);
    }
  }, [setNodes, fitView]);

  useEffect(() => { void loadCanvas(); }, [loadCanvas]);

  // First-visit race fix: if a capture landed BEFORE this canvas mounted
  // (e.g. user just finished onboarding → got redirected → extension fired
  // SO_CAPTURE_LANDED while we were still en route), CaptureListener writes
  // a timestamp to localStorage. We check it once on mount, and if it's
  // fresh AND our first load came back empty, poll until the new client
  // shows up or we give up (~8s). Clears the flag once consumed.
  useEffect(() => {
    let canceled = false;
    let attempts = 0;
    const MAX_ATTEMPTS = 10; // 10 × 800ms = 8s
    const POLL_MS = 800;
    const FRESH_WINDOW_MS = 60_000; // capture flag must be within last 60s
    const tryRecover = async () => {
      if (canceled) return;
      let tsRaw: string | null = null;
      try { tsRaw = localStorage.getItem('so:capture:landed:ts'); } catch { /* noop */ }
      if (!tsRaw) return;
      const ts = parseInt(tsRaw, 10);
      if (!ts || Date.now() - ts > FRESH_WINDOW_MS) {
        try { localStorage.removeItem('so:capture:landed:ts'); } catch { /* noop */ }
        return;
      }
      // Already populated → consume the flag and stop.
      if (nodesRef.current.some((n) => n.type === 'client')) {
        try { localStorage.removeItem('so:capture:landed:ts'); } catch { /* noop */ }
        return;
      }
      attempts += 1;
      await loadCanvas({ silent: true });
      if (canceled) return;
      if (nodesRef.current.some((n) => n.type === 'client')) {
        try { localStorage.removeItem('so:capture:landed:ts'); } catch { /* noop */ }
        return;
      }
      if (attempts < MAX_ATTEMPTS) {
        setTimeout(() => void tryRecover(), POLL_MS);
      } else {
        try { localStorage.removeItem('so:capture:landed:ts'); } catch { /* noop */ }
      }
    };
    // Give the initial loadCanvas a beat to settle, then start polling.
    const kickoff = setTimeout(() => void tryRecover(), 400);
    return () => {
      canceled = true;
      clearTimeout(kickoff);
    };
  }, [loadCanvas]);

  // Keep loadCanvas accessible in the SSE effect without adding it as a dep
  // (which would restart the connection on every render).
  const loadCanvasRef = useRef(loadCanvas);
  loadCanvasRef.current = loadCanvas;

  // SSE live-push: subscribe to /v1/events and update the canvas when a
  // capture.landed event arrives. Uses fetch + ReadableStream so we can
  // send the Authorization header (browser EventSource cannot).
  //
  // Connection lifecycle:
  //   connected    → streaming, canvas auto-updates on events
  //   reconnecting → waiting to retry (exponential backoff: 1s→2s→4s→30s cap)
  //   disconnected → auth failure or unmount — no retry
  //
  // On reconnect, a catch-up loadCanvas() is done to recover any events
  // missed while the connection was down.
  useEffect(() => {
    let canceled = false;
    let retryDelay = 1000;

    const connect = async () => {
      if (canceled) return;
      const token = getSession();
      if (!token) {
        setSseStatus('disconnected');
        return;
      }

      const ac = new AbortController();
      sseAbortRef.current = ac;
      setSseStatus('reconnecting');

      try {
        const resp = await fetch('/v1/events', {
          headers: { Authorization: `Bearer ${token}` },
          signal: ac.signal,
        });

        if (resp.status === 401) {
          clearSession();
          window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
          setSseStatus('disconnected');
          return; // auth failure — don't retry
        }

        if (!resp.ok || !resp.body) {
          throw new Error(`SSE response ${resp.status}`);
        }

        setSseStatus('connected');
        retryDelay = 1000; // reset backoff on successful connect

        // Catch-up: reload canvas once so we don't miss events that
        // landed while we were connecting or reconnecting.
        await loadCanvasRef.current({ silent: true });

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (!canceled) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop() ?? '';
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const payload = JSON.parse(line.slice(6)) as {
                type: string;
                client_id?: number;
                client_name?: string;
                is_new_client?: boolean;
              };
              if (payload.type === 'capture.landed') {
                // De-dupe: CaptureListener handles the toast when
                // SO_CAPTURE_LANDED fires in this tab (same-tab flow).
                // If so:capture-cleared fired within 3s, that toast
                // already ran — skip ours to avoid duplication.
                const alreadyToasted = Date.now() - recentCaptureClearedRef.current < 3000;
                // Reload canvas with reveal animation for new nodes.
                await loadCanvasRef.current({ sseReveal: true });
                // Write the breadcrumb flag so post-redirect catches also work.
                try { localStorage.setItem('so:capture:landed:ts', String(Date.now())); } catch { /* noop */ }
                if (!alreadyToasted && payload.is_new_client && payload.client_name) {
                  toastRef.current.success(
                    `${payload.client_name} added — they're on your dashboard.`,
                  );
                }
              }
            } catch { /* malformed JSON — ignore */ }
          }
        }
      } catch (err) {
        if (canceled) return;
        if (err instanceof DOMException && err.name === 'AbortError') return;
        // Network error — fall through to reconnect with backoff
        setSseStatus('reconnecting');
      }

      if (!canceled) {
        // Catch-up before retry so events from the gap aren't lost.
        await loadCanvasRef.current({ silent: true });
        setSseStatus('reconnecting');
        setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 30_000);
      }
    };

    connect();

    return () => {
      canceled = true;
      sseAbortRef.current?.abort();
      setSseStatus('disconnected');
    };
  }, []); // Stable: all dependencies accessed via refs

  // Forward-refs to drag-target actions so the document-level rescue handler
  // (declared below, before these callbacks) can call them. We bind these
  // after their useCallbacks resolve.
  const moveLoginRef = useRef<((src: string, util: 'GMP'|'VEC'|'WEC', dst: string, origin?: number|null, loginId?: string|null) => void) | null>(null);
  const moveAccountRef = useRef<((src: string, accountId: string, dst: string) => void) | null>(null);
  const moveArrayRef = useRef<((src: string, arrayId: string, dst: string, subMeterCount: number) => void) | null>(null);
  // Ref kept fresh on every render so the stale-closure onDrop handler can call toast.
  const toastRef = useRef(toast);

  // ── Rescue handler: ReactFlow eats dragover/drop on its pane wrapper, so
  // login row drops onto client cards silently fail despite the card having
  // onDragOver/onDrop handlers. We attach our own listeners at the document
  // level in capture phase, manually hit-test for a client card under the
  // pointer, and call moveLoginToClient ourselves. This bypasses RF entirely.
  useEffect(() => {
    const findTargetClientId = (clientX: number, clientY: number): string | null => {
      const stack = document.elementsFromPoint(clientX, clientY);
      for (const el of stack) {
        const card = (el as HTMLElement).closest?.('[data-walkthrough="client-card"]') as HTMLElement | null;
        if (card) {
          const id = card.getAttribute('data-walkthrough-client-id');
          if (id) return id;
        }
      }
      return null;
    };

    const onDragOver = (e: DragEvent) => {
      if (!e.dataTransfer) return;
      const types = Array.from(e.dataTransfer.types);
      if (
        !types.includes('application/x-so-login') &&
        !types.includes('application/x-so-account') &&
        !types.includes('application/x-so-array')
      ) return;
      // Without preventDefault here, drop never fires (browser default is
      // to refuse drops). React Flow's wrapper swallows the bubbled event
      // from the card, so we have to do it ourselves at document level.
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
    };

    const onDrop = (e: DragEvent) => {
      if (!e.dataTransfer) return;
      const loginRaw = e.dataTransfer.getData('application/x-so-login');
      const accountRaw = e.dataTransfer.getData('application/x-so-account');
      const arrayRaw = e.dataTransfer.getData('application/x-so-array');
      if (!loginRaw && !accountRaw && !arrayRaw) return;
      const targetClientId = findTargetClientId(e.clientX, e.clientY);
      if (arrayRaw && !targetClientId) {
        toastRef.current.show('Drop on a client card to move this array', 'info');
        e.preventDefault();
        return;
      }
      if (!targetClientId) return;
      e.preventDefault();
      e.stopPropagation();
      try {
        if (loginRaw) {
          const { srcClientId, utility, originClientId, loginId } = JSON.parse(loginRaw) as {
            srcClientId: string;
            utility: 'GMP' | 'VEC' | 'WEC';
            originClientId?: number | null;
            loginId?: string | null;
          };
          if (srcClientId !== targetClientId) {
            moveLoginRef.current?.(srcClientId, utility, targetClientId, originClientId, loginId);
          }
        } else if (accountRaw) {
          const { srcClientId, accountId } = JSON.parse(accountRaw) as { srcClientId: string; accountId: string };
          if (srcClientId !== targetClientId) {
            moveAccountRef.current?.(srcClientId, accountId, targetClientId);
          }
        } else if (arrayRaw) {
          const { srcClientId, arrayId, subMeterCount } = JSON.parse(arrayRaw) as {
            srcClientId: string;
            arrayId: string;
            subMeterCount: number;
            arrayName: string;
            accountId: string;
            accountNumber: string;
          };
          if (srcClientId !== targetClientId) {
            moveArrayRef.current?.(srcClientId, arrayId, targetClientId, subMeterCount);
          }
        }
      } catch { /* malformed payload */ }
    };

    document.addEventListener('dragover', onDragOver, true);
    document.addEventListener('drop', onDrop, true);
    return () => {
      document.removeEventListener('dragover', onDragOver, true);
      document.removeEventListener('drop', onDrop, true);
    };
  }, []);

  /** After a new client is created, select it and pan to it so the user
   *  sees the result. In sorted mode, uses fitView to show its grid slot;
   *  in free mode, setCenter to the exact position. */
  const centerOnClientId = useCallback((numericId: number) => {
    const nodeId = `client_${numericId}`;
    const tryFocus = (attempt = 0) => {
      const node = nodesRef.current.find((n) => n.id === nodeId);
      if (node) {
        setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === nodeId })));
        if (layoutModeRef.current === 'free') {
          setCenter(node.position.x + 144, node.position.y + 110, { zoom: 1, duration: 600 });
        } else {
          fitView({ nodes: [{ id: nodeId }], padding: 0.8, duration: 400, maxZoom: 1.0 });
        }
        return;
      }
      if (attempt < 20) setTimeout(() => tryFocus(attempt + 1), 100);
    };
    tryFocus();
  }, [setCenter, setNodes, fitView]);

  // Refresh the canvas when a new capture completes. Also record the
  // timestamp so the SSE handler knows CaptureListener already toasted.
  useEffect(() => {
    const onCapture = () => {
      recentCaptureClearedRef.current = Date.now();
      void loadCanvas();
    };
    window.addEventListener('so:capture-cleared', onCapture);
    return () => window.removeEventListener('so:capture-cleared', onCapture);
  }, [loadCanvas]);

  // ── Undo / redo stack ─────────────────────────────────────────────────────

  const pushUndo = useCallback((entry: UndoEntry) => {
    setUndoStack(prev => [entry, ...prev].slice(0, 25));
    setRedoStack([]);
  }, []);

  const handleUndo = useCallback(() => {
    const stack = undoStackRef.current;
    if (stack.length === 0) return;
    const [top, ...rest] = stack;
    setUndoStack(rest);
    if (top.redo) setRedoStack(prev => [top, ...prev]);
    top.undo();
    toast.show(`Undid: ${top.label}`, 'info');
  }, [toast]);

  const handleRedo = useCallback(() => {
    const stack = redoStackRef.current;
    if (stack.length === 0) return;
    const [top, ...rest] = stack;
    if (!top.redo) { setRedoStack(rest); return; }
    setRedoStack(rest);
    setUndoStack(prev => [top, ...prev].slice(0, 25));
    top.redo();
    toast.show(`Redid: ${top.label}`, 'info');
  }, [toast]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement) {
        const tag = e.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;
      }
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key === 'z') {
        e.preventDefault();
        handleUndo();
      } else if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key === 'z') {
        e.preventDefault();
        handleRedo();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [handleUndo, handleRedo]);

  // ── Canvas actions ────────────────────────────────────────────────────────

  const toggleExpand = useCallback(
    (nodeId: string) => {
      setNodes((ns) =>
        ns.map((n) =>
          n.id === nodeId && n.type === 'client'
            ? { ...n, data: { ...(n.data as ClientNodeData), expanded: !(n.data as ClientNodeData).expanded } }
            : n,
        ),
      );
    },
    [setNodes],
  );

  const startRename = useCallback((nodeId: string) => {
    setContextMenu(null);
    setRenamingNodeId(nodeId);
  }, []);

  const finishRename = useCallback(
    (nodeId: string, name: string) => {
      setRenamingNodeId(null);
      if (!name) return;

      const current = nodesRef.current;
      const node = current.find((n) => n.id === nodeId && n.type === 'client');
      if (!node) return;
      const oldName = (node.data as ClientNodeData).client.name;
      if (oldName === name) return;

      const applyName = (n: string) =>
        setNodes((ns) =>
          ns.map((nd) => {
            if (nd.id !== nodeId || nd.type !== 'client') return nd;
            const d = nd.data as ClientNodeData;
            return { ...nd, data: { ...d, client: { ...d.client, name: n } } };
          }),
        );

      applyName(name);

      const numId = parseInt(nodeId.replace('client_', ''), 10);
      if (!isNaN(numId)) {
        updateClient(numId, { name }).catch(() => {
          applyName(oldName);
          toast.show('Rename failed — reverted.', 'error');
        });
        pushUndo({
          label: `Rename to "${name}"`,
          timestamp: Date.now(),
          undo: () => {
            applyName(oldName);
            updateClient(numId, { name: oldName }).catch(() =>
              toast.show('Undo rename failed.', 'error'),
            );
          },
          redo: () => {
            applyName(name);
            updateClient(numId, { name }).catch(() =>
              toast.show('Redo rename failed.', 'error'),
            );
          },
        });
      }
    },
    [setNodes, pushUndo, toast],
  );

  const cancelRename = useCallback(() => setRenamingNodeId(null), []);

  const startRenameArray = useCallback((arrayId: number) => {
    setContextMenu(null);
    setRenamingNodeId(`array_${arrayId}`);
  }, []);

  const finishRenameArray = useCallback(
    (arrayId: number, name: string) => {
      setRenamingNodeId(null);
      if (!name) return;

      const current = nodesRef.current;
      let ownerNodeId: string | null = null;
      let clientNumId = NaN;
      let oldName = '';
      for (const n of current) {
        if (n.type !== 'client') continue;
        const d = n.data as ClientNodeData;
        for (const acc of d.client.accounts) {
          const match = acc.arrays.find((arr) => arr.id === `arr_${arrayId}`);
          if (match) {
            ownerNodeId = n.id;
            clientNumId = parseInt(n.id.replace('client_', ''), 10);
            oldName = match.name;
            break;
          }
        }
        if (ownerNodeId) break;
      }
      if (!ownerNodeId || oldName === name) return;

      const applyName = (n: string) =>
        setNodes((ns) =>
          ns.map((nd) => {
            if (nd.id !== ownerNodeId || nd.type !== 'client') return nd;
            const cd = nd.data as ClientNodeData;
            return {
              ...nd,
              data: {
                ...cd,
                client: {
                  ...cd.client,
                  accounts: cd.client.accounts.map((acc) => ({
                    ...acc,
                    arrays: acc.arrays.map((arr) =>
                      arr.id === `arr_${arrayId}` ? { ...arr, name: n } : arr,
                    ),
                  })),
                },
              },
            };
          }),
        );

      applyName(name);

      if (!isNaN(arrayId) && !isNaN(clientNumId)) {
        updateArray(clientNumId, arrayId, { name }).catch(() => {
          applyName(oldName);
          toast.show('Array rename failed — reverted.', 'error');
        });
      }
    },
    [setNodes, toast],
  );

  const deleteNode = useCallback(
    (nodeId: string) => {
      setContextMenu(null);
      const isClient = nodeId.startsWith('client_');
      const isAccount = nodeId.startsWith('account_');
      const numId = parseInt(nodeId.replace(isClient ? 'client_' : 'account_', ''), 10);
      const snapshot = nodesRef.current;
      const removedNode = snapshot.find((n) => n.id === nodeId);
      if (!removedNode) return;
      const removedName = isClient
        ? (removedNode.data as ClientNodeData).client.name
        : (removedNode.data as UnclassifiedNodeData).account?.account_number ?? 'item';

      setNodes((ns) => ns.filter((n) => n.id !== nodeId));

      if (isClient && !isNaN(numId)) {
        let currentToken: string | null = null;
        const restoreViaReload = () => { void loadCanvas({ silent: true }); };

        deleteClient(numId)
          .then((res) => {
            currentToken = res.undo_token;
            toast.show(`Deleted ${removedName}. Cmd+Z to undo.`, 'info');
            pushUndo({
              label: `Delete client "${removedName}"`,
              timestamp: Date.now(),
              undo: () => {
                if (!currentToken) { restoreViaReload(); return; }
                const tok = currentToken;
                currentToken = null;
                undoDelete(tok)
                  .then(() => restoreViaReload())
                  .catch(() => {
                    toast.show('Undo failed — 5-minute window may have expired.', 'error');
                    restoreViaReload();
                  });
              },
              redo: () => {
                setNodes((ns) => ns.filter((n) => n.id !== nodeId));
                deleteClient(numId)
                  .then((res2) => { currentToken = res2.undo_token; })
                  .catch(() => {
                    toast.show('Redo delete failed.', 'error');
                    restoreViaReload();
                  });
              },
            });
          })
          .catch(() => {
            setNodes(snapshot);
            toast.show('Delete failed — reverted.', 'error');
          });
      } else if (isAccount && !isNaN(numId)) {
        toast.show(`Hidden ${removedName}. Cmd+Z to restore.`, 'info');
        pushUndo({
          label: `Hide account ${removedName}`,
          timestamp: Date.now(),
          undo: () => {
            const frozen = snapshot.map((n) =>
              n.id === nodeId && n.data && typeof n.data === 'object'
                ? { ...n, data: { ...n.data, entryDelay: 0 } as typeof n.data }
                : n,
            );
            setNodes(frozen);
          },
          redo: () => setNodes((ns) => ns.filter((n) => n.id !== nodeId)),
        });
      } else {
        toast.show('Hidden from canvas — reload to restore.', 'info');
      }
    },
    [setNodes, toast, pushUndo, loadCanvas],
  );

  const detachAccount = useCallback(
    (clientId: string, accountId: string) => {
      const current = nodesRef.current;
      const clientNode = current.find((n) => n.id === clientId && n.type === 'client');
      if (!clientNode) return;
      const d = clientNode.data as ClientNodeData;
      const detached = d.client.accounts.find((a) => a.id === accountId);
      if (!detached) return;

      const snapshot = current;

      const applyDetach = () =>
        setNodes((ns) => {
          const updatedClient: ClientData = {
            ...d.client,
            accounts: d.client.accounts.filter((a) => a.id !== accountId),
          };
          const unclNode: Node = {
            id: accountId,
            type: 'unclassified',
            position: { x: clientNode.position.x + 360, y: clientNode.position.y },
            data: { account: detached, entryDelay: 0 } as UnclassifiedNodeData,
          };
          return [
            ...ns.map((n) => n.id === clientId ? { ...n, data: { ...d, client: updatedClient } } : n),
            unclNode,
          ];
        });

      applyDetach();
      toast.show(`${detached.utility} · ${detached.account_number} detached.`, 'info');

      const numId = parseInt(accountId.replace('account_', ''), 10);
      const clientNumId = parseInt(clientId.replace('client_', ''), 10);
      if (!isNaN(numId)) {
        reassignAccount(numId, null).catch(() => {
          setNodes(snapshot);
          toast.show('Detach failed — reverted.', 'error');
        });
        if (!isNaN(clientNumId)) {
          pushUndo({
            label: `Detach ${detached.utility} · ${detached.account_number}`,
            timestamp: Date.now(),
            undo: () => {
              setNodes(snapshot);
              reassignAccount(numId, clientNumId).catch(() =>
                toast.show('Undo detach failed.', 'error'),
              );
            },
            redo: () => {
              applyDetach();
              reassignAccount(numId, null).catch(() =>
                toast.show('Redo detach failed.', 'error'),
              );
            },
          });
        }
      }
    },
    [setNodes, toast, pushUndo],
  );

  const attachToClient = useCallback(
    (unclassifiedId: string, targetClientId: string) => {
      const current = nodesRef.current;
      const unclNode = current.find((n) => n.id === unclassifiedId && n.type === 'unclassified');
      const clientNode = current.find((n) => n.id === targetClientId && n.type === 'client');
      if (!unclNode || !clientNode) return;

      const uData = unclNode.data as UnclassifiedNodeData;
      const cData = clientNode.data as ClientNodeData;

      const snapshot = current;

      const applyAttach = () =>
        setNodes((ns) => {
          const updatedClient: ClientData = {
            ...cData.client,
            accounts: [...cData.client.accounts, uData.account],
          };
          return ns
            .filter((n) => n.id !== unclassifiedId)
            .map((n) => n.id === targetClientId ? { ...n, data: { ...cData, client: updatedClient } } : n);
        });

      applyAttach();
      toast.show(
        `${uData.account.utility} · ${uData.account.account_number} added to ${cData.client.name}.`,
        'success',
      );

      const accNumId = parseInt(unclassifiedId.replace('account_', ''), 10);
      const clientNumId = parseInt(targetClientId.replace('client_', ''), 10);
      if (!isNaN(accNumId) && !isNaN(clientNumId)) {
        reassignAccount(accNumId, clientNumId)
          .then(() => { void loadCanvas(); })
          .catch(() => {
            setNodes(snapshot);
            toast.show('Move failed — reverted.', 'error');
          });
        pushUndo({
          label: `Assign ${uData.account.utility} · ${uData.account.account_number} to ${cData.client.name}`,
          timestamp: Date.now(),
          undo: () => {
            setNodes(snapshot);
            reassignAccount(accNumId, null).catch(() =>
              toast.show('Undo assign failed.', 'error'),
            );
          },
          redo: () => {
            applyAttach();
            reassignAccount(accNumId, clientNumId)
              .then(() => { void loadCanvas(); })
              .catch(() => toast.show('Redo assign failed.', 'error'));
          },
        });
      }
    },
    [setNodes, toast, loadCanvas, pushUndo],
  );

  const confirmMerge = useCallback(
    async (survivorId: string) => {
      if (!mergeDialog) return;
      const otherId = survivorId === mergeDialog.sourceId ? mergeDialog.targetId : mergeDialog.sourceId;

      const snapshot = nodesRef.current;
      const survivor = snapshot.find((n) => n.id === survivorId && n.type === 'client');
      const other = snapshot.find((n) => n.id === otherId && n.type === 'client');
      if (!survivor || !other) { setMergeDialog(null); return; }

      const sData = survivor.data as ClientNodeData;
      const oData = other.data as ClientNodeData;
      const merged: ClientData = {
        ...sData.client,
        accounts: [...sData.client.accounts, ...oData.client.accounts],
      };

      setNodes((ns) =>
        ns
          .filter((n) => n.id !== otherId)
          .map((n) => n.id === survivorId ? { ...n, data: { ...sData, client: merged } } : n),
      );
      setMergeDialog(null);

      pushUndo({
        label: `Merge "${oData.client.name}" into "${sData.client.name}"`,
        timestamp: Date.now(),
        undo: () => {
          setNodes(snapshot);
          // NOTE: server state is still merged — this is a view-only revert.
          // TODO: add /v1/account/clients/unmerge endpoint for true server-side undo.
        },
      });

      if (survivorId.startsWith('client_') && otherId.startsWith('client_')) {
        const srcId = parseInt(otherId.replace('client_', ''), 10);
        const dstId = parseInt(survivorId.replace('client_', ''), 10);
        try {
          await mergeClientInto(srcId, dstId);
          void loadCanvas();
        } catch (_err) {
          setNodes(snapshot);
          toast.show('Merge failed — changes reverted.', 'error');
        }
      }
    },
    [mergeDialog, setNodes, loadCanvas, toast, pushUndo],
  );

  const cancelMerge = useCallback(() => {
    setMergeDialog((dlg) => {
      if (dlg) {
        const { sourceId, sourceOrigin } = dlg;
        setNodes((ns) =>
          ns.map((n) => (n.id === sourceId ? { ...n, position: sourceOrigin } : n)),
        );
        dragOriginRef.current.delete(sourceId);
      }
      return null;
    });
  }, [setNodes]);

  // ── Position persistence (debounced, free mode only) ──────────────────────

  const flushPositions = useCallback(() => {
    const updates: { node_type: 'client' | 'account'; node_id: number; x: number; y: number }[] = [];
    for (const [nodeId, pending] of pendingPosRef.current.entries()) {
      const isClient = nodeId.startsWith('client_');
      const isAccount = nodeId.startsWith('account_');
      if (!isClient && !isAccount) continue;
      const numId = parseInt(nodeId.replace(isClient ? 'client_' : 'account_', ''), 10);
      if (isNaN(numId)) continue;
      updates.push({ node_type: isClient ? 'client' : 'account', node_id: numId, x: pending.x, y: pending.y });
    }
    if (updates.length === 0) return;
    pendingPosRef.current.clear();
    for (const t of posTimers.current.values()) clearTimeout(t);
    posTimers.current.clear();
    try {
      const blob = new Blob([JSON.stringify(updates)], { type: 'application/json' });
      const ok = navigator.sendBeacon?.('/v1/sandbox/positions', blob);
      if (ok) return;
    } catch { /* fall through to regular save */ }
    patchCanvasPositions(updates).catch(() => { /* best effort */ });
  }, []);

  useEffect(() => {
    const handler = () => flushPositions();
    window.addEventListener('beforeunload', handler);
    window.addEventListener('pagehide', handler);
    return () => {
      window.removeEventListener('beforeunload', handler);
      window.removeEventListener('pagehide', handler);
      flushPositions();
    };
  }, [flushPositions]);

  const savePosition = useCallback((nodeId: string, x: number, y: number) => {
    // Only save positions in free mode — sorted mode positions are computed, not stored.
    if (layoutModeRef.current === 'sorted') return;
    pendingPosRef.current.set(nodeId, { x, y });
    const existing = posTimers.current.get(nodeId);
    if (existing) clearTimeout(existing);
    posTimers.current.set(nodeId, setTimeout(() => {
      const pending = pendingPosRef.current.get(nodeId);
      pendingPosRef.current.delete(nodeId);
      posTimers.current.delete(nodeId);
      if (!pending) return;
      const isClient = nodeId.startsWith('client_');
      const isAccount = nodeId.startsWith('account_');
      if (!isClient && !isAccount) return;
      const numId = parseInt(nodeId.replace(isClient ? 'client_' : 'account_', ''), 10);
      if (isNaN(numId)) return;
      patchCanvasPositions([{
        node_type: isClient ? 'client' : 'account',
        node_id: numId,
        x: pending.x,
        y: pending.y,
      }]).catch(() => { /* position drift is acceptable */ });
    }, 150));
  }, []);

  // ── Drag: live merge-intent highlight (free mode only) ────────────────────

  const onNodeDragStart = useCallback(
    (_event: MouseEvent | TouchEvent, node: Node) => {
      if (node.type !== 'client') return;
      dragOriginRef.current.set(node.id, { x: node.position.x, y: node.position.y });
    },
    [],
  );

  const onNodeDrag = useCallback(
    (_event: MouseEvent | TouchEvent, node: Node) => {
      if (node.type !== 'client') return;
      const hits = getIntersectingNodes(node)
        .filter((n) => n.id !== node.id && n.type === 'client');
      const targetId = hits[0]?.id ?? null;
      setNodes((ns) =>
        ns.map((n) => {
          if (n.type !== 'client') return n;
          const d = n.data as ClientNodeData;
          let nextIntent: 'source' | 'target' | null = null;
          if (n.id === node.id && targetId) nextIntent = 'source';
          else if (n.id === targetId) nextIntent = 'target';
          if (d.mergeIntent === nextIntent) return n;
          return { ...n, data: { ...d, mergeIntent: nextIntent } };
        }),
      );
    },
    [getIntersectingNodes, setNodes],
  );

  const clearMergeIntent = useCallback(() => {
    setNodes((ns) =>
      ns.map((n) => {
        if (n.type !== 'client') return n;
        const d = n.data as ClientNodeData;
        if (!d.mergeIntent) return n;
        return { ...n, data: { ...d, mergeIntent: null } };
      }),
    );
  }, [setNodes]);

  const onNodeDragStop = useCallback(
    (_event: MouseEvent | TouchEvent, node: Node) => {
      clearMergeIntent();
      const hits = getIntersectingNodes(node).filter((n) => n.id !== node.id);

      if (node.type === 'unclassified') {
        const target = hits.find((n) => n.type === 'client');
        if (target) { attachToClient(node.id, target.id); return; }
      } else if (node.type === 'client') {
        const target = hits.find((n) => n.type === 'client');
        if (target) {
          const nData = node.data as ClientNodeData;
          const tData = target.data as ClientNodeData;
          const origin = dragOriginRef.current.get(node.id) ?? node.position;
          setMergeDialog({
            sourceId: node.id,
            targetId: target.id,
            sourceName: nData.client.name,
            targetName: tData.client.name,
            sourceOrigin: { x: origin.x, y: origin.y },
          });
          return;
        }
      }

      dragOriginRef.current.delete(node.id);
      savePosition(node.id, node.position.x, node.position.y);
    },
    [getIntersectingNodes, attachToClient, savePosition, clearMergeIntent],
  );

  // ── Layout mode / sort key controls ──────────────────────────────────────

  const handleLayoutModeChange = useCallback((mode: LayoutMode) => {
    setLayoutMode(mode);
    try { localStorage.setItem('so:sandbox:layout-mode', mode); } catch { /* ignore */ }

    if (mode === 'sorted') {
      // Recompute positions from sort key and disable client node dragging
      setNodes((ns) => {
        const clientNodes = ns.filter((n) => n.type === 'client');
        const positions = computeSortedPositionsFromNodes(clientNodes, sortKeyRef.current, densityRef.current);
        return ns.map((n) => {
          if (n.type !== 'client') return n;
          const pos = positions.get(n.id);
          return { ...n, draggable: false, ...(pos ? { position: pos } : {}) };
        });
      });
      setTimeout(() => fitView({ padding: 0.35, duration: 400, maxZoom: 0.85 }), 80);
    } else {
      // Free mode — re-enable client node dragging
      setNodes((ns) =>
        ns.map((n) => (n.type === 'client' ? { ...n, draggable: true } : n)),
      );
    }
  }, [fitView, setNodes]);

  const handleSortKeyChange = useCallback((key: SortKey) => {
    setSortKey(key);
    try { localStorage.setItem('so:sandbox:sort', key); } catch { /* ignore */ }

    if (layoutModeRef.current === 'sorted') {
      setNodes((ns) => {
        const clientNodes = ns.filter((n) => n.type === 'client');
        const positions = computeSortedPositionsFromNodes(clientNodes, key, densityRef.current);
        return ns.map((n) => {
          if (n.type !== 'client') return n;
          const pos = positions.get(n.id);
          return pos ? { ...n, position: pos } : n;
        });
      });
    }
  }, [setNodes]);

  // ── Auto-arrange (free mode) ──────────────────────────────────────────────

  const autoArrange = useCallback(() => {
    const { COL_W: colW } = GRID[densityRef.current];
    const cols = DENSITY_COLS[densityRef.current];
    setNodes((ns) => {
      const clientNodes = ns.filter((n) => n.type === 'client');
      const positions = computeSortedPositionsFromNodes(clientNodes, sortKeyRef.current, densityRef.current);
      const uncNodes = ns.filter((n) => n.type === 'unclassified');
      return [
        ...clientNodes.map((n) => {
          const pos = positions.get(n.id);
          return pos ? { ...n, position: pos } : n;
        }),
        ...uncNodes.map((n, i) => ({
          ...n,
          position: { x: cols * colW + 80, y: i * 240 + 40 },
        })),
      ];
    });
    setTimeout(() => fitView({ padding: 0.35, duration: 400, maxZoom: 0.85 }), 80);
  }, [setNodes, fitView]);

  // ── Cross-client account drag (HTML5 dnd) ────────────────────────────────

  const moveAccountToClient = useCallback(
    (srcClientId: string, accountId: string, dstClientId: string) => {
      if (srcClientId === dstClientId) return;
      const current = nodesRef.current;
      const src = current.find((n) => n.id === srcClientId && n.type === 'client');
      const dst = current.find((n) => n.id === dstClientId && n.type === 'client');
      if (!src || !dst) return;

      const srcData = src.data as ClientNodeData;
      const dstData = dst.data as ClientNodeData;
      const moved = srcData.client.accounts.find((a) => a.id === accountId);
      if (!moved) return;

      // Rewrite login_origin_client_id so the optimistic local copy renders
      // as a "home" login under the destination instead of carrying the
      // stale "from <previous client>" badge until the next loadCanvas.
      const dstNumIdOptim = parseInt(dstClientId.replace('client_', ''), 10);
      const movedRebased = !isNaN(dstNumIdOptim)
        ? { ...moved, login_origin_client_id: dstNumIdOptim }
        : moved;

      const snapshot = current;

      const applyMove = () =>
        setNodes((ns) =>
          ns.map((n) => {
            if (n.id === srcClientId) {
              return {
                ...n,
                data: {
                  ...srcData,
                  client: {
                    ...srcData.client,
                    accounts: srcData.client.accounts.filter((a) => a.id !== accountId),
                  },
                },
              };
            }
            if (n.id === dstClientId) {
              return {
                ...n,
                data: {
                  ...dstData,
                  client: {
                    ...dstData.client,
                    accounts: [...dstData.client.accounts, movedRebased],
                  },
                },
              };
            }
            return n;
          }),
        );

      applyMove();
      toast.show(
        `${moved.utility} · ${moved.account_number} → ${dstData.client.name}.`,
        'success',
      );

      const accNumId = parseInt(accountId.replace('account_', ''), 10);
      const srcNumId = parseInt(srcClientId.replace('client_', ''), 10);
      const dstNumId = parseInt(dstClientId.replace('client_', ''), 10);
      if (!isNaN(accNumId) && !isNaN(dstNumId)) {
        reassignAccount(accNumId, dstNumId)
          .then(() => { void loadCanvas(); })
          .catch(() => {
            setNodes(snapshot);
            toast.show('Move failed — reverted.', 'error');
          });
        if (!isNaN(srcNumId)) {
          pushUndo({
            label: `Move ${moved.utility} · ${moved.account_number} to ${dstData.client.name}`,
            timestamp: Date.now(),
            undo: () => {
              setNodes(snapshot);
              reassignAccount(accNumId, srcNumId).catch(() =>
                toast.show('Undo move failed.', 'error'),
              );
            },
            redo: () => {
              applyMove();
              reassignAccount(accNumId, dstNumId)
                .then(() => { void loadCanvas(); })
                .catch(() => toast.show('Redo move failed.', 'error'));
            },
          });
        }
      }
    },
    [setNodes, toast, loadCanvas, pushUndo],
  );

  // ── Login-level (group) actions ──────────────────────────────────────────

  const detachLogin = useCallback(
    (clientId: string, utility: 'GMP' | 'VEC' | 'WEC', originClientId?: number | null, loginId?: string | null) => {
      const current = nodesRef.current;
      const clientNode = current.find((n) => n.id === clientId && n.type === 'client');
      if (!clientNode) return;
      const d = clientNode.data as ClientNodeData;
      const ownNumId = (() => {
        const m = clientId.match(/^client_(\d+)$/);
        return m ? parseInt(m[1], 10) : null;
      })();
      const detachedAccounts = d.client.accounts.filter((a) => {
        if (a.utility !== utility) return false;
        const aOrigin =
          a.login_origin_client_id != null && a.login_origin_client_id !== ownNumId
            ? a.login_origin_client_id
            : null;
        const want = originClientId ?? null;
        if (aOrigin !== want) return false;
        if (loginId != null) {
          const aLoginId = `${a.utility}-${aOrigin ?? 'home'}`;
          if (aLoginId !== loginId) return false;
        }
        return true;
      });
      if (detachedAccounts.length === 0) return;

      const snapshot = current;

      const applyDetach = () =>
        setNodes((ns) => {
          const updatedClient: ClientData = {
            ...d.client,
            accounts: d.client.accounts.filter((a) => !detachedAccounts.includes(a)),
          };
          const baseX = clientNode.position.x + 360;
          const baseY = clientNode.position.y;
          const newNodes: Node[] = detachedAccounts.map((acc, i) => ({
            id: acc.id,
            type: 'unclassified',
            position: { x: baseX + (i % 2) * 200, y: baseY + Math.floor(i / 2) * 110 },
            data: { account: acc, entryDelay: i * 25 } as UnclassifiedNodeData,
          }));
          return [
            ...ns.map((n) => n.id === clientId ? { ...n, data: { ...d, client: updatedClient } } : n),
            ...newNodes,
          ];
        });

      applyDetach();
      toast.show(
        `${utility} login (${detachedAccounts.length} ${detachedAccounts.length === 1 ? 'account' : 'accounts'}) detached.`,
        'info',
      );

      Promise.all(
        detachedAccounts.map((acc) => {
          const numId = parseInt(acc.id.replace('account_', ''), 10);
          if (isNaN(numId)) return Promise.resolve();
          return reassignAccount(numId, null);
        }),
      ).catch(() => {
        setNodes(snapshot);
        toast.show('Detach failed — reverted.', 'error');
      });

      if (ownNumId !== null) {
        pushUndo({
          label: `Detach ${utility} login (${detachedAccounts.length} account${detachedAccounts.length === 1 ? '' : 's'})`,
          timestamp: Date.now(),
          undo: () => {
            setNodes(snapshot);
            Promise.all(
              detachedAccounts.map((acc) => {
                const numId = parseInt(acc.id.replace('account_', ''), 10);
                if (isNaN(numId)) return Promise.resolve();
                return reassignAccount(numId, ownNumId);
              }),
            ).catch(() => toast.show('Undo detach login failed.', 'error'));
          },
          redo: () => {
            applyDetach();
            Promise.all(
              detachedAccounts.map((acc) => {
                const numId = parseInt(acc.id.replace('account_', ''), 10);
                if (isNaN(numId)) return Promise.resolve();
                return reassignAccount(numId, null);
              }),
            ).catch(() => toast.show('Redo detach login failed.', 'error'));
          },
        });
      }
    },
    [setNodes, toast, pushUndo],
  );

  const moveLoginToClient = useCallback(
    (srcClientId: string, utility: 'GMP' | 'VEC' | 'WEC', dstClientId: string, originClientId?: number | null, loginId?: string | null) => {
      if (srcClientId === dstClientId) return;
      const current = nodesRef.current;
      const src = current.find((n) => n.id === srcClientId && n.type === 'client');
      const dst = current.find((n) => n.id === dstClientId && n.type === 'client');
      if (!src || !dst) return;

      const srcData = src.data as ClientNodeData;
      const dstData = dst.data as ClientNodeData;
      const srcOwnNumId = (() => {
        const m = srcClientId.match(/^client_(\d+)$/);
        return m ? parseInt(m[1], 10) : null;
      })();
      const moved = srcData.client.accounts.filter((a) => {
        if (a.utility !== utility) return false;
        const aOrigin =
          a.login_origin_client_id != null && a.login_origin_client_id !== srcOwnNumId
            ? a.login_origin_client_id
            : null;
        const want = originClientId ?? null;
        if (aOrigin !== want) return false;
        if (loginId != null) {
          const aLoginId = `${a.utility}-${aOrigin ?? 'home'}`;
          if (aLoginId !== loginId) return false;
        }
        return true;
      });
      if (moved.length === 0) return;

      // Numeric ID of the destination client — used both for the server
      // reassign call AND to rewrite login_origin_client_id on the moved
      // accounts so the optimistic local copy renders as a "home" login
      // (no "from <previous client>" badge) instead of carrying the stale
      // origin from before the move.
      const dstNumIdEarly = parseInt(dstClientId.replace('client_', ''), 10);
      const movedRebased = !isNaN(dstNumIdEarly)
        ? moved.map((a) => ({ ...a, login_origin_client_id: dstNumIdEarly }))
        : moved;

      const snapshot = current;

      const applyMove = () =>
        setNodes((ns) =>
          ns.map((n) => {
            if (n.id === srcClientId) {
              return {
                ...n,
                data: {
                  ...srcData,
                  client: {
                    ...srcData.client,
                    accounts: srcData.client.accounts.filter((a) => !moved.includes(a)),
                  },
                },
              };
            }
            if (n.id === dstClientId) {
              return {
                ...n,
                data: {
                  ...dstData,
                  client: {
                    ...dstData.client,
                    accounts: [...dstData.client.accounts, ...movedRebased],
                  },
                },
              };
            }
            return n;
          }),
        );

      applyMove();
      toast.show(
        `${utility} login (${moved.length} ${moved.length === 1 ? 'account' : 'accounts'}) → ${dstData.client.name}.`,
        'success',
      );

      const dstNumId = dstNumIdEarly;
      if (isNaN(dstNumId)) return;
      Promise.all(
        moved.map((acc) => {
          const numId = parseInt(acc.id.replace('account_', ''), 10);
          if (isNaN(numId)) return Promise.resolve();
          return reassignAccount(numId, dstNumId);
        }),
      )
        .then(() => { void loadCanvas(); })
        .catch(() => {
          setNodes(snapshot);
          toast.show('Move failed — reverted.', 'error');
        });

      if (srcOwnNumId !== null) {
        pushUndo({
          label: `Move ${utility} login to ${dstData.client.name}`,
          timestamp: Date.now(),
          undo: () => {
            setNodes(snapshot);
            Promise.all(
              moved.map((acc) => {
                const numId = parseInt(acc.id.replace('account_', ''), 10);
                if (isNaN(numId)) return Promise.resolve();
                return reassignAccount(numId, srcOwnNumId);
              }),
            ).catch(() => toast.show('Undo move login failed.', 'error'));
          },
          redo: () => {
            applyMove();
            Promise.all(
              moved.map((acc) => {
                const numId = parseInt(acc.id.replace('account_', ''), 10);
                if (isNaN(numId)) return Promise.resolve();
                return reassignAccount(numId, dstNumId);
              }),
            )
              .then(() => { void loadCanvas(); })
              .catch(() => toast.show('Redo move login failed.', 'error'));
          },
        });
      }
    },
    [setNodes, toast, loadCanvas, pushUndo],
  );

  // ── Array-level drag ──────────────────────────────────────────────────────

  const moveArrayToClient = useCallback(
    (srcClientId: string, arrayId: string, dstClientId: string, subMeterCount: number) => {
      if (srcClientId === dstClientId) return;
      if (subMeterCount > 1) {
        if (!window.confirm(
          `This array has ${subMeterCount} sub-meter accounts — they'll move together. Proceed?`,
        )) return;
      }
      const current = nodesRef.current;
      const src = current.find((n) => n.id === srcClientId && n.type === 'client');
      const dst = current.find((n) => n.id === dstClientId && n.type === 'client');
      if (!src || !dst) return;

      const srcData = src.data as ClientNodeData;
      const dstData = dst.data as ClientNodeData;

      const movedAccounts = srcData.client.accounts.filter((a) =>
        a.arrays.some((ar) => ar.id === arrayId),
      );
      if (movedAccounts.length === 0) return;

      const arrayName = movedAccounts[0].arrays.find((ar) => ar.id === arrayId)?.name ?? arrayId;
      const snapshot = current;

      const applyMove = () =>
        setNodes((ns) =>
          ns.map((n) => {
            if (n.id === srcClientId) {
              return {
                ...n,
                data: {
                  ...srcData,
                  client: {
                    ...srcData.client,
                    accounts: srcData.client.accounts.filter((a) => !movedAccounts.includes(a)),
                  },
                },
              };
            }
            if (n.id === dstClientId) {
              return {
                ...n,
                data: {
                  ...dstData,
                  client: {
                    ...dstData.client,
                    accounts: [...dstData.client.accounts, ...movedAccounts],
                  },
                },
              };
            }
            return n;
          }),
        );

      applyMove();
      toast.show(`Array "${arrayName}" → ${dstData.client.name}.`, 'success');

      const arrNumId = parseInt(arrayId.replace('arr_', ''), 10);
      const dstNumId = parseInt(dstClientId.replace('client_', ''), 10);
      const srcNumId = parseInt(srcClientId.replace('client_', ''), 10);

      if (!isNaN(arrNumId) && !isNaN(dstNumId)) {
        reassignArray(arrNumId, dstNumId)
          .then(() => { void loadCanvas(); })
          .catch(() => {
            setNodes(snapshot);
            toast.show('Move failed — reverted.', 'error');
          });
        if (!isNaN(srcNumId)) {
          pushUndo({
            label: `Move array "${arrayName}" to ${dstData.client.name}`,
            timestamp: Date.now(),
            undo: () => {
              setNodes(snapshot);
              reassignArray(arrNumId, srcNumId).catch(() =>
                toast.show('Undo array move failed.', 'error'),
              );
            },
            redo: () => {
              applyMove();
              reassignArray(arrNumId, dstNumId)
                .then(() => { void loadCanvas(); })
                .catch(() => toast.show('Redo array move failed.', 'error'));
            },
          });
        }
      }
    },
    [setNodes, toast, loadCanvas, pushUndo],
  );

  // Bind drag-rescue forward-refs now that the callbacks exist.
  moveLoginRef.current = moveLoginToClient;
  moveAccountRef.current = moveAccountToClient;
  moveArrayRef.current = moveArrayToClient;
  toastRef.current = toast;

  const togglePin = useCallback(
    (clientId: string) => {
      const numId = parseInt(clientId.replace('client_', ''), 10);
      if (isNaN(numId)) return;
      const current = nodesRef.current;
      const node = current.find((n) => n.id === clientId && n.type === 'client');
      if (!node) return;
      const wasPinned = !!(node.data as ClientNodeData).client.pinned;
      const next = !wasPinned;

      const applyPin = (val: boolean) =>
        setNodes((ns) =>
          ns.map((n) => {
            if (n.id !== clientId) return n;
            const d = n.data as ClientNodeData;
            return { ...n, data: { ...d, client: { ...d.client, pinned: val } } };
          }),
        );

      applyPin(next);
      toast.show(next ? 'Starred.' : 'Unstarred.', 'info');
      pinClient(numId, next).catch(() => {
        applyPin(wasPinned);
        toast.show('Pin failed — reverted.', 'error');
      });

      // In sorted mode with "pinned" sort key, recompute positions after pin change
      if (layoutModeRef.current === 'sorted') {
        setNodes((ns) => {
          const clientNodes = ns.filter((n) => n.type === 'client');
          const positions = computeSortedPositionsFromNodes(clientNodes, sortKeyRef.current, densityRef.current);
          return ns.map((n) => {
            if (n.type !== 'client') return n;
            const pos = positions.get(n.id);
            return pos ? { ...n, position: pos } : n;
          });
        });
      }

      pushUndo({
        label: next ? `Pin "${(node.data as ClientNodeData).client.name}"` : `Unpin "${(node.data as ClientNodeData).client.name}"`,
        timestamp: Date.now(),
        undo: () => {
          applyPin(wasPinned);
          pinClient(numId, wasPinned).catch(() =>
            toast.show('Undo pin failed.', 'error'),
          );
        },
        redo: () => {
          applyPin(next);
          pinClient(numId, next).catch(() =>
            toast.show('Redo pin failed.', 'error'),
          );
        },
      });
    },
    [setNodes, toast, pushUndo],
  );

  // Generic client field patch — used by the inline-editable Name / email
  // spots in the sandbox header. Optimistic local update + PATCH; rolls back
  // on server error. Skips no-op patches so blur-with-no-change is free.
  const updateClientField = useCallback(
    async (clientId: string, patch: { name?: string; contact_email?: string | null }) => {
      const numId = parseInt(clientId.replace('client_', ''), 10);
      if (isNaN(numId)) return;
      const before = nodesRef.current.find((n) => n.id === clientId);
      if (!before) return;
      const beforeData = before.data as ClientNodeData;
      // No-op guard.
      const sameName = patch.name === undefined || patch.name === beforeData.client.name;
      const beforeEmail = (beforeData.client as { contact_email?: string | null }).contact_email ?? null;
      const nextEmail = patch.contact_email === undefined ? undefined : (patch.contact_email || null);
      const sameEmail = nextEmail === undefined || nextEmail === beforeEmail;
      if (sameName && sameEmail) return;
      // Optimistic apply.
      setNodes((ns) =>
        ns.map((n) => {
          if (n.id !== clientId) return n;
          const d = n.data as ClientNodeData;
          return {
            ...n,
            data: {
              ...d,
              client: {
                ...d.client,
                ...(patch.name !== undefined ? { name: patch.name } : {}),
                ...(patch.contact_email !== undefined ? { contact_email: patch.contact_email || null } : {}),
              },
            },
          };
        }),
      );
      try {
        const apiPatch: { name?: string; contact_email?: string | null } = {};
        if (patch.name !== undefined) apiPatch.name = patch.name;
        if (patch.contact_email !== undefined) apiPatch.contact_email = patch.contact_email || null;
        await updateClient(numId, apiPatch);
        // Re-apply the patch AFTER the PATCH succeeds. Reason: an inflight
        // capture-cleared / sandbox-mutated event can fire `loadCanvas()`
        // concurrently and overwrite our optimistic state with a pre-PATCH
        // server snapshot. Re-applying here guarantees the field sticks
        // even if a racing reload landed in the middle. The PATCH is now
        // committed, so any subsequent loadCanvas will already include it.
        setNodes((ns) =>
          ns.map((n) => {
            if (n.id !== clientId) return n;
            const d = n.data as ClientNodeData;
            return {
              ...n,
              data: {
                ...d,
                client: {
                  ...d.client,
                  ...(patch.name !== undefined ? { name: patch.name } : {}),
                  ...(patch.contact_email !== undefined ? { contact_email: patch.contact_email || null } : {}),
                },
              },
            };
          }),
        );
      } catch (err) {
        // Roll back on failure.
        setNodes((ns) =>
          ns.map((n) => (n.id === clientId ? before : n)),
        );
        toast.show(err instanceof Error ? err.message : 'Update failed', 'error');
      }
    },
    [setNodes, toast],
  );

  // ── Add placeholder client (no modal — spawn blank, drop into rename) ────
  // Ford Jun6'26: cleaner than the modal. One-click → blank client appears,
  // centered on canvas, name field active for inline edit. Walkthrough +
  // toolbar + placeholder-CTA all route here. Existing AddClientModal kept
  // around as a defensive callable via prop if anyone wires it, but no UI
  // path opens it anymore.
  // Repeat clicks auto-increment: "Untitled client" → "Untitled client 2"
  // → "Untitled client 3". Spam-safe: concurrent invocations track
  // in-flight names in a ref so they don't race to the same name (the
  // Client table has UNIQUE (tenant_id, name) — 4 of 5 rapid clicks
  // would otherwise 409 because loadCanvas hadn't resolved yet).
  const pendingClientNamesRef = useRef<Set<string>>(new Set());
  const handleAddPlaceholderClient = useCallback(async () => {
    // Compute next available name considering BOTH on-canvas labels and
    // any names currently being created by an earlier in-flight click.
    const names = new Set<string>(pendingClientNamesRef.current);
    for (const n of nodesRef.current) {
      if (n.type === 'client' && n.data && typeof n.data === 'object') {
        // The on-canvas client name lives at data.client.name (not data.label —
        // we had that wrong; it returned undefined which meant the on-canvas
        // de-dupe loop never saw existing clients and spam-click #2 collided
        // on the (tenant_id, name) UNIQUE constraint).
        const nm = (n.data as ClientNodeData).client?.name;
        if (typeof nm === 'string') names.add(nm);
      }
    }
    let nextName = 'Untitled client';
    if (names.has(nextName)) {
      let i = 2;
      while (names.has(`Untitled client ${i}`)) i++;
      nextName = `Untitled client ${i}`;
    }
    pendingClientNamesRef.current.add(nextName);
    try {
      const created = await createClient({
        name: nextName,
        contact_email: null,
      });
      await loadCanvas();
      const ncId = created.id;
      pushUndo({
        label: `Add client "${created.name}"`,
        timestamp: Date.now(),
        undo: () => {
          deleteClient(ncId)
            .then(() => void loadCanvas())
            .catch(() => toast.show('Undo add client failed.', 'error'));
        },
      });
      centerOnClientId(ncId);
      setTimeout(() => startRename(`client_${ncId}`), 50);
    } catch (err) {
      toast.show(
        err instanceof Error ? err.message : "Couldn't add client",
        'error',
      );
    } finally {
      pendingClientNamesRef.current.delete(nextName);
    }
  }, [centerOnClientId, loadCanvas, pushUndo, startRename, toast]);

  // ── Context ───────────────────────────────────────────────────────────────

  const actions: CanvasActions = {
    toggleExpand,
    startRename,
    finishRename,
    cancelRename,
    renamingNodeId,
    startRenameArray,
    finishRenameArray,
    deleteNode,
    detachAccount,
    moveAccountToClient,
    detachLogin,
    moveLoginToClient,
    moveArrayToClient,
    getOriginClient: (cid: number) => originLookup[cid] ?? null,
    updateClient: updateClientField,
    togglePin,
    density,
  };

  // ── Render ────────────────────────────────────────────────────────────────

  const isEmpty = !loading && !loadError && nodes.length === 0;
  const topUndo = undoStack[0] ?? null;
  const topRedo = redoStack[0] ?? null;

  return (
    <CanvasActionsContext.Provider value={actions}>
      <div className="relative h-full w-full overflow-hidden bg-gradient-to-br from-[#fbf9f3] via-[#f6fbf2] to-[#f0f9ec]">
        {/* Soft sun-glow ambient flare — top-right warmth */}
        <div
          aria-hidden
          className="pointer-events-none absolute -right-32 -top-32 z-0 h-[560px] w-[560px] rounded-full opacity-60 blur-3xl"
          style={{
            background: 'radial-gradient(closest-side, rgba(250, 220, 140, 0.55), rgba(250, 220, 140, 0) 70%)',
          }}
        />
        {/* Cool meadow glow — bottom-left depth */}
        <div
          aria-hidden
          className="pointer-events-none absolute -bottom-40 -left-32 z-0 h-[620px] w-[620px] rounded-full opacity-50 blur-3xl"
          style={{
            background: 'radial-gradient(closest-side, rgba(110, 231, 183, 0.45), rgba(110, 231, 183, 0) 70%)',
          }}
        />
        <ReactFlow
          nodes={nodes}
          edges={[]}
          onNodesChange={onNodesChange}
          onNodeDragStart={onNodeDragStart}
          onNodeDrag={onNodeDrag}
          onNodeDragStop={onNodeDragStop}
          nodeTypes={NODE_TYPES}
          nodesConnectable={false}
          multiSelectionKeyCode="Shift"
          selectionKeyCode="Shift"
          selectNodesOnDrag={false}
          {...(() => {
            try {
              const raw = localStorage.getItem('so:sandbox:viewport');
              if (raw) {
                const v = JSON.parse(raw);
                if (typeof v?.x === 'number' && typeof v?.y === 'number' && typeof v?.zoom === 'number') {
                  return { defaultViewport: v };
                }
              }
            } catch {}
            return { fitView: nodes.length > 0, fitViewOptions: { padding: 0.35, maxZoom: 0.85 } };
          })()}
          onMoveEnd={(_e, v) => {
            try { localStorage.setItem('so:sandbox:viewport', JSON.stringify(v)); } catch {}
          }}
          deleteKeyCode={null}
          onPaneClick={() => setContextMenu(null)}
          onNodeClick={() => setContextMenu(null)}
          onNodeContextMenu={(e, node) => {
            e.preventDefault();
            setContextMenu({ x: e.clientX, y: e.clientY, nodeId: node.id, nodeType: node.type ?? '' });
          }}
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} color="#9caf88" gap={22} size={1.6} />
          <Controls showInteractive={false} />

          {/* SSE live indicator removed Jun 6 (Ford): the pill was confusing
              operators ("what does Re mean?") even when working correctly,
              and the upstream proxy buffering kept it stuck in
              "Reconnecting" for many users. The connection state is internal
              plumbing — the canvas re-renders when events arrive, which is
              the actual user-visible signal. If you need it back for debug,
              wrap <LiveIndicator status={sseStatus} /> in a DevPanel toggle. */}

          {/* Toolbar — top-center (matches centered top tabs). Single row,
              no-wrap so the layout/sort controls stay aligned with the action
              buttons; Panel hosts the natural content width. */}
          {!loading && !isEmpty && (
            <Panel position="top-center">
              <div className="flex flex-nowrap items-center gap-2 whitespace-nowrap">
                <button
                  type="button"
                  data-walkthrough="add-client-btn"
                  className="rounded-lg bg-primary-500 px-3 py-1.5 text-xs font-semibold text-white shadow-sm transition-colors hover:bg-primary-600 active:bg-primary-700"
                  onClick={() => {
                    clientIdsBeforeModal.current = new Set(
                      nodesRef.current
                        .filter((n) => n.type === 'client' && n.id.startsWith('client_'))
                        .map((n) => parseInt(n.id.replace('client_', ''), 10))
                        .filter((id) => !isNaN(id)),
                    );
                    setShowAddByLogin(true);
                  }}
                >
                  + Add Client
                </button>
                <ToolbarButton onClick={handleAddPlaceholderClient}>
                  + Add client manually
                </ToolbarButton>
                <LayoutModeControl layoutMode={layoutMode} onChange={handleLayoutModeChange} />
                {layoutMode === 'sorted' && (
                  <SortKeyControl sortKey={sortKey} onChange={handleSortKeyChange} />
                )}
                {layoutMode === 'free' && (
                  <ToolbarButton onClick={autoArrange}>Auto-arrange</ToolbarButton>
                )}
                {(() => {
                  const selectedClients = nodes.filter((n) => n.selected && n.type === 'client');
                  const n = selectedClients.length;
                  if (n === 0) return null;
                  return (
                    <button
                      type="button"
                      onClick={() => {
                        const names = selectedClients
                          .map((s) => (s.data as ClientNodeData).client.name)
                          .slice(0, 3)
                          .join(', ');
                        const more = n > 3 ? ` and ${n - 3} more` : '';
                        if (!confirm(`Delete ${n} client${n === 1 ? '' : 's'}? (${names}${more})\n\nCmd+Z undoes within 5 minutes.`)) return;
                        for (const s of selectedClients) deleteNode(s.id);
                      }}
                      className="rounded-md bg-red-50 px-3 py-1.5 text-xs font-semibold text-red-700 ring-1 ring-red-300 transition-colors hover:bg-red-100"
                      title={`Delete ${n} selected client${n === 1 ? '' : 's'}`}
                    >
                      🗑 Delete {n}
                    </button>
                  );
                })()}
              </div>
            </Panel>
          )}

          {/* Toolbar — bottom-right: view + history actions */}
          {!loading && !isEmpty && (
            <Panel position="bottom-right">
              <div className="flex gap-2 flex-wrap justify-end">
                <ToolbarButton onClick={() => fitView({ padding: 0.35, duration: 400, maxZoom: 0.85 })}>
                  Fit to view
                </ToolbarButton>
                <button
                  type="button"
                  disabled={!topUndo}
                  onClick={handleUndo}
                  className={[
                    'rounded-md px-3 py-1.5 text-xs font-semibold transition-colors',
                    topUndo
                      ? 'bg-amber-50 text-amber-800 hover:bg-amber-100 ring-1 ring-amber-300'
                      : 'bg-cream-bg text-zinc-400 ring-1 ring-cream-border cursor-not-allowed',
                  ].join(' ')}
                  title={topUndo ? `${topUndo.label} (⌘Z)` : 'Nothing to undo'}
                >
                  ↶ {topUndo ? `Undo: ${topUndo.label}` : 'Undo'}
                </button>
                <button
                  type="button"
                  disabled={!topRedo}
                  onClick={handleRedo}
                  className={[
                    'rounded-md px-3 py-1.5 text-xs font-semibold transition-colors',
                    topRedo
                      ? 'bg-sky-50 text-sky-800 hover:bg-sky-100 ring-1 ring-sky-300'
                      : 'bg-cream-bg text-zinc-400 ring-1 ring-cream-border cursor-not-allowed',
                  ].join(' ')}
                  title={topRedo ? `${topRedo.label} (⌘⇧Z)` : 'Nothing to redo'}
                >
                  ↷ {topRedo ? `Redo: ${topRedo.label}` : 'Redo'}
                </button>
              </div>
            </Panel>
          )}
        </ReactFlow>

        {/* Dev-only sandbox tools */}
        <DevPanel
          onChange={() => void loadCanvas()}
          clients={nodes
            .filter((n) => n.type === 'client')
            .map((n) => {
              const m = n.id.match(/^client_(\d+)$/);
              const id = m ? parseInt(m[1], 10) : -1;
              const name = (n.data as ClientNodeData).client.name;
              return { id, name };
            })
            .filter((c) => c.id > 0)}
        />

        {/* Loading overlay */}
        {loading && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-zinc-50/80">
            <Spinner className="h-8 w-8 text-primary-500" />
          </div>
        )}

        {/* Error state */}
        {!loading && loadError && (
          <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-zinc-50">
            <p className="text-sm text-zinc-500">Couldn't load canvas — {loadError}</p>
            <button
              type="button"
              className="rounded-xl bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700"
              onClick={() => void loadCanvas()}
            >
              Retry
            </button>
          </div>
        )}

        {/* Empty state — guided first-touch for fresh users */}
        {isEmpty && (
          <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-5 bg-gradient-to-br from-amber-50/40 via-zinc-50 to-emerald-50/30 px-6">
            <div className="max-w-md text-center">
              <p className="mb-1 text-[11px] font-medium uppercase tracking-wider text-primary-600">
                Step 1 of 3 · Welcome
              </p>
              <p className="mb-2 text-xl font-semibold tracking-tight text-zinc-900">
                Let's add your first client
              </p>
              <p className="text-sm leading-relaxed text-zinc-600">
                Click <strong className="text-zinc-800">+ Add Client</strong> and sign into their
                utility portal. We'll capture their accounts and arrays automatically — no
                copy-paste, no spreadsheets.
              </p>
            </div>
            <button
              type="button"
              className="rounded-xl bg-primary-600 px-6 py-3 text-sm font-semibold text-white shadow-md hover:bg-primary-700 active:bg-primary-800 transition-colors"
              onClick={() => {
                clientIdsBeforeModal.current = new Set(
                  nodesRef.current
                    .filter((n) => n.type === 'client' && n.id.startsWith('client_'))
                    .map((n) => parseInt(n.id.replace('client_', ''), 10))
                    .filter((id) => !isNaN(id)),
                );
                setShowAddByLogin(true);
              }}
            >
              + Add Client
            </button>
            <div className="flex items-center gap-2 text-[11px] text-zinc-400">
              <span>Already have a roster?</span>
              <button
                type="button"
                className="underline underline-offset-2 hover:text-zinc-600"
                onClick={handleAddPlaceholderClient}
              >
                Add a placeholder client
              </button>
              <span>or scroll down to import from a spreadsheet</span>
            </div>
          </div>
        )}

        {/* Merge dialog */}
        {mergeDialog && (
          <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/20 backdrop-blur-[2px]">
            <div className="w-80 rounded-2xl bg-white p-6 shadow-2xl">
              <p className="mb-1 text-sm font-semibold text-zinc-900">Merge clients?</p>
              <p className="mb-4 text-xs text-zinc-500">
                All utility accounts move to the surviving client.
              </p>
              <div className="space-y-2">
                <MergeOption
                  label="Merge into"
                  name={mergeDialog.targetName}
                  onClick={() => void confirmMerge(mergeDialog.targetId)}
                />
                <MergeOption
                  label="Merge into"
                  name={mergeDialog.sourceName}
                  onClick={() => void confirmMerge(mergeDialog.sourceId)}
                />
              </div>
              <button
                type="button"
                className="mt-3 w-full rounded-xl border border-zinc-200 px-4 py-2.5 text-sm text-zinc-500 transition-colors hover:bg-zinc-50"
                onClick={cancelMerge}
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Right-click context menu */}
        {contextMenu && (
          <ContextMenuPopover
            menu={contextMenu}
            onStartRename={() => startRename(contextMenu.nodeId)}
            onDelete={() => deleteNode(contextMenu.nodeId)}
          />
        )}

        {/* Add client — portal-picker first, manual entry as fallback */}
        {showAddByLogin && (
          <Suspense fallback={null}>
            <AddClientByLoginModal
              open={showAddByLogin}
              onClose={() => setShowAddByLogin(false)}
              onCaptured={async () => {
                void loadCanvas();
                try {
                  const rows = await listClients();
                  const before = clientIdsBeforeModal.current;
                  const newClients = rows.filter((r) => !before.has(r.id));
                  for (const nc of newClients) {
                    const ncId = nc.id;
                    const ncName = nc.name;
                    pushUndo({
                      label: `Add client "${ncName}"`,
                      timestamp: Date.now(),
                      undo: () => {
                        deleteClient(ncId)
                          .then(() => void loadCanvas())
                          .catch(() => toast.show('Undo add client failed.', 'error'));
                      },
                    });
                  }
                  const focus = newClients[newClients.length - 1];
                  if (focus) {
                    centerOnClientId(focus.id);
                    setLastCapturedClientId(focus.id);
                    setNodes((ns) =>
                      ns.map((n) =>
                        n.id === `client_${focus.id}`
                          ? { ...n, data: { ...(n.data as ClientNodeData), expanded: true } }
                          : n,
                      ),
                    );
                  }
                  return rows.map((r) => ({ id: r.id, name: r.name }));
                } catch {
                  return [];
                }
              }}
              onSwitchToManual={() => {
                setShowAddByLogin(false);
                void handleAddPlaceholderClient();
              }}
            />
          </Suspense>
        )}

        {/* Walkthrough */}
        {!loading && !loadError && nodes.filter((n) => n.type === 'client').length > 0 && (
          <SandboxWalkthrough
            clientCount={nodes.filter((n) => n.type === 'client').length}
            lastCapturedClientId={lastCapturedClientId}
            onOpenByLogin={() => {
              clientIdsBeforeModal.current = new Set(
                nodesRef.current
                  .filter((n) => n.type === 'client' && n.id.startsWith('client_'))
                  .map((n) => parseInt(n.id.replace('client_', ''), 10))
                  .filter((id) => !isNaN(id)),
              );
              setShowAddByLogin(true);
            }}
            onOpenManual={handleAddPlaceholderClient}
          />
        )}

        {/* Cmd+K / Ctrl+K command palette */}
        <CommandPalette
          nodes={nodes}
          setNodes={setNodes}
          onAddClient={() => setShowAddByLogin(true)}
          onAutoArrange={autoArrange}
          onFitView={() => fitView({ padding: 0.35, duration: 400, maxZoom: 0.85 })}
        />
      </div>
    </CanvasActionsContext.Provider>
  );
}

// ── Small presentational sub-components ────────────────────────────────────

function LayoutModeControl({
  layoutMode,
  onChange,
}: {
  layoutMode: LayoutMode;
  onChange: (v: LayoutMode) => void;
}) {
  return (
    <div className="flex overflow-hidden rounded-lg border border-cream-border divide-x divide-cream-border">
      <button
        type="button"
        className={[
          'px-2.5 py-1.5 text-xs font-medium transition-colors',
          layoutMode === 'sorted'
            ? 'bg-primary-50 text-primary-800'
            : 'bg-white text-zinc-600 hover:bg-zinc-50 active:bg-zinc-100',
        ].join(' ')}
        onClick={() => onChange('sorted')}
        title="Sorted layout — positions computed from sort key, no randomization"
      >
        ≡ Sorted
      </button>
      <button
        type="button"
        className={[
          'px-2.5 py-1.5 text-xs font-medium transition-colors',
          layoutMode === 'free'
            ? 'bg-primary-50 text-primary-800'
            : 'bg-white text-zinc-600 hover:bg-zinc-50 active:bg-zinc-100',
        ].join(' ')}
        onClick={() => onChange('free')}
        title="Free layout — drag cards to any position"
      >
        ✦ Free
      </button>
    </div>
  );
}

function SortKeyControl({ sortKey, onChange }: { sortKey: SortKey; onChange: (v: SortKey) => void }) {
  return (
    <select
      value={sortKey}
      onChange={(e) => onChange(e.target.value as SortKey)}
      className="rounded-lg border border-cream-border bg-white px-2 py-1.5 text-xs font-medium text-zinc-700 shadow-sm hover:border-zinc-300 cursor-pointer"
      title="Sort key for card layout"
    >
      <option value="name">Name (A→Z)</option>
      <option value="recent">Recently captured</option>
      <option value="arrays">Most arrays</option>
      <option value="pinned">Pinned first</option>
    </select>
  );
}

function ToolbarButton({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      className="rounded-lg border border-cream-border bg-white px-3 py-1.5 text-xs font-medium text-zinc-700 shadow-sm transition-colors hover:border-zinc-300 hover:bg-zinc-50 active:bg-zinc-100"
      onClick={onClick}
    >
      {children}
    </button>
  );
}

function MergeOption({ label, name, onClick }: { label: string; name: string; onClick: () => void }) {
  return (
    <button
      type="button"
      className="w-full rounded-xl border border-primary-100 bg-primary-50 px-4 py-3 text-left text-sm font-medium text-primary-800 transition-colors hover:bg-primary-100 active:bg-primary-200"
      onClick={onClick}
    >
      {label} <span className="font-semibold">&ldquo;{name}&rdquo;</span>
    </button>
  );
}

function ContextMenuPopover({
  menu,
  onStartRename,
  onDelete,
}: {
  menu: ContextMenu;
  onStartRename: () => void;
  onDelete: () => void;
}) {
  return (
    <div
      style={{ position: 'fixed', left: menu.x, top: menu.y, zIndex: 9999 }}
      className="min-w-[160px] overflow-hidden rounded-xl border border-zinc-200 bg-white py-1 shadow-xl"
    >
      {menu.nodeType === 'client' && (
        <button
          type="button"
          className="w-full px-3 py-2 text-left text-sm text-zinc-700 transition-colors hover:bg-zinc-50"
          onClick={onStartRename}
        >
          Rename
        </button>
      )}
      {(menu.nodeType === 'client' || menu.nodeType === 'unclassified') && (
        <>
          {menu.nodeType === 'client' && <div className="my-1 border-t border-zinc-100" />}
          <button
            type="button"
            className="w-full px-3 py-2 text-left text-sm text-red-600 transition-colors hover:bg-red-50"
            onClick={onDelete}
          >
            Delete
          </button>
        </>
      )}
    </div>
  );
}
