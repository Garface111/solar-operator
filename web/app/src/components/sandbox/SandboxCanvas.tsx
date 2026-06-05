import { useCallback, useEffect, useRef, useState } from 'react';
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
import { type ClientData, type UtilityAccount, type Utility } from './mockData';
import {
  getCanvasData,
  mergeClientInto,
  patchCanvasPositions,
  pinClient,
  reassignAccount,
  updateClient,
  deleteClient,
  undoDelete,
  listClients,
  type CanvasResponse,
} from '../../lib/api';
import { useToast } from '../../ui/Toast';
import { Spinner } from '../../ui/Spinner';
import { AddClientModal } from '../AddClientModal';
import { AddClientByLoginModal } from '../AddClientByLoginModal';
import { CommandPalette } from './CommandPalette';
import { DevPanel } from './DevPanel';
import { SandboxWalkthrough } from './SandboxWalkthrough';

// ── Node type registry (stable reference — must live outside component) ────

const NODE_TYPES: NodeTypes = {
  client: ClientNodeComponent,
  unclassified: UnclassifiedNodeComponent,
};

// ── Layout constants ────────────────────────────────────────────────────────

const COLS = 4;

type DensityOverride = 'auto' | Density;

const GRID: Record<Density, { COL_W: number; ROW_H: number }> = {
  full:    { COL_W: 330, ROW_H: 295 },
  compact: { COL_W: 250, ROW_H: 200 },
  dense:   { COL_W: 190, ROW_H:  90 },
};

const DENSITY_THRESH = { compact: 6, dense: 16 };

function deriveDensity(clientCount: number): Density {
  if (clientCount >= DENSITY_THRESH.dense) return 'dense';
  if (clientCount >= DENSITY_THRESH.compact) return 'compact';
  return 'full';
}

// ── Provider normalizer ─────────────────────────────────────────────────────

function normalizeProvider(p: string): Utility {
  const u = p.toUpperCase() as Utility;
  return (['GMP', 'VEC', 'WEC'] as Utility[]).includes(u) ? u : 'GMP';
}

// ── API → React Flow nodes ──────────────────────────────────────────────────

function buildNodesFromApi(
  data: CanvasResponse,
  colW: number,
  rowH: number,
): { nodes: Node[]; autoPositioned: { node_type: 'client' | 'account'; node_id: number; x: number; y: number }[] } {
  const nodes: Node[] = [];
  const autoPositioned: { node_type: 'client' | 'account'; node_id: number; x: number; y: number }[] = [];

  // CANONICAL grid for slot bookkeeping: always use the 'full' density grid
  // when deciding which slots are occupied and which are free. If we keyed
  // by the current colW/rowH, switching density tiers (e.g. 5→6 clients
  // flipping auto from 'full' to 'compact') would re-bucket the same saved
  // coords into different slot keys, making `findFreeSlot` think populated
  // spots were free and landing new clients on top of existing ones every
  // reload. The visual layout still uses colW/rowH for NEW slot
  // assignments, but the occupancy set is invariant.
  const CANON_W = GRID.full.COL_W;
  const CANON_H = GRID.full.ROW_H;
  const slotOccupied = new Set<string>();
  const slotKey = (x: number, y: number) => {
    const col = Math.round((x - 40) / CANON_W);
    const row = Math.round((y - 40) / CANON_H);
    return `${col},${row}`;
  };
  data.clients.forEach((c) => {
    if (c.canvas_x != null && c.canvas_y != null) {
      slotOccupied.add(slotKey(c.canvas_x, c.canvas_y));
    }
  });
  // Find next free slot (column-major, scanning row by row) — emits coords
  // in the CANONICAL grid so the next reload at any density resolves them
  // back to the same slot key.
  let nextSlotIdx = 0;
  const findFreeSlot = (): { x: number; y: number } => {
    while (true) {
      const col = nextSlotIdx % COLS;
      const row = Math.floor(nextSlotIdx / COLS);
      nextSlotIdx++;
      if (!slotOccupied.has(`${col},${row}`)) {
        slotOccupied.add(`${col},${row}`);
        return { x: col * CANON_W + 40, y: row * CANON_H + 40 };
      }
    }
  };

  // Suppress unused-var lint while preserving the signature so callers can
  // still pass colW/rowH for future use (e.g. unclassified pile spacing).
  void colW; void rowH;

  let autoAccIdx = 0;

  data.clients.forEach((client, i) => {
    const clientData: ClientData = {
      id: `client_${client.id}`,
      name: client.name,
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
            }]
          : [],
      })),
    };

    const hasPos = client.canvas_x != null && client.canvas_y != null;
    const position = hasPos
      ? { x: client.canvas_x!, y: client.canvas_y! }
      : findFreeSlot();
    if (!hasPos) {
      autoPositioned.push({ node_type: 'client', node_id: client.id, x: position.x, y: position.y });
    }

    nodes.push({
      id: `client_${client.id}`,
      type: 'client',
      position,
      data: { client: clientData, expanded: false, entryDelay: i * 30 } as ClientNodeData,
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
          }]
        : [],
    };

    const hasPos = acc.canvas_x != null && acc.canvas_y != null;
    let position: { x: number; y: number };
    if (hasPos) {
      position = { x: acc.canvas_x!, y: acc.canvas_y! };
    } else {
      const idx = autoAccIdx++;
      position = { x: COLS * colW + 80, y: idx * 240 + 40 };
      autoPositioned.push({ node_type: 'account', node_id: acc.id, x: position.x, y: position.y });
    }

    nodes.push({
      id: `account_${acc.id}`,
      type: 'unclassified',
      position,
      data: { account: accountData, entryDelay: (data.clients.length + i) * 30 } as UnclassifiedNodeData,
    });
  });

  return { nodes, autoPositioned };
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
  const [showAddModal, setShowAddModal] = useState(false);
  const [showAddByLogin, setShowAddByLogin] = useState(false);
  const [originLookup, setOriginLookup] = useState<NonNullable<CanvasResponse['clients_index']>>({});
  const [lastCapturedClientId, setLastCapturedClientId] = useState<number | null>(null);

  const [densityOverride, setDensityOverride] = useState<DensityOverride>(() => {
    try {
      const saved = localStorage.getItem('so:sandbox:density');
      if (saved === 'auto' || saved === 'full' || saved === 'compact' || saved === 'dense') return saved;
    } catch { /* ignore */ }
    return 'auto';
  });
  const densityOverrideRef = useRef<DensityOverride>(densityOverride);
  densityOverrideRef.current = densityOverride;

  const clientCount = nodes.filter((n) => n.type === 'client').length;
  const density: Density = densityOverride === 'auto' ? deriveDensity(clientCount) : densityOverride;

  const { getIntersectingNodes, fitView, setCenter } = useReactFlow();

  // Pre-drag node positions captured at drag start so we can snap a node
  // back if a merge dialog gets cancelled (otherwise the dragged card stays
  // overlapping the target).
  const dragOriginRef = useRef<Map<string, { x: number; y: number }>>(new Map());

  // Always-fresh refs used in callbacks to avoid stale closures
  const nodesRef = useRef<Node[]>(nodes);
  nodesRef.current = nodes;

  const undoStackRef = useRef<UndoEntry[]>(undoStack);
  undoStackRef.current = undoStack;
  const redoStackRef = useRef<UndoEntry[]>(redoStack);
  redoStackRef.current = redoStack;

  // Per-node debounce timers for position persistence
  const posTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  // Latest pending (x, y) per node — flushed on unload so a quick
  // drag-then-reload sequence doesn't lose positions.
  const pendingPosRef = useRef<Map<string, { x: number; y: number }>>(new Map());
  // Forward-ref to savePosition so callbacks defined above it (like
  // centerOnClientId) can call it without circular hook deps.
  const savePositionRef = useRef<((nodeId: string, x: number, y: number) => void) | null>(null);

  // Snapshot of client IDs before the portal-picker modal opens, used to
  // detect which clients were newly created so we can push undo entries.
  const clientIdsBeforeModal = useRef<Set<number>>(new Set());

  // Esc closes the context menu (Cmd+Z / Cmd+Shift+Z are handled by the
  // deep undo/redo stack below)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setContextMenu(null);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  // ── Data loading ──────────────────────────────────────────────────────────

  const loadCanvas = useCallback(async (opts: { silent?: boolean } = {}) => {
    setLoadError(null);
    if (!opts.silent) setLoading(true);
    try {
      const data = await getCanvasData();
      const loadedCount = data.clients.length;
      const effectiveDensity: Density =
        densityOverrideRef.current === 'auto' ? deriveDensity(loadedCount) : densityOverrideRef.current;
      const { COL_W: colW, ROW_H: rowH } = GRID[effectiveDensity];
      const { nodes: built, autoPositioned } = buildNodesFromApi(data, colW, rowH);
      // On silent reloads (e.g. delete-undo) zero out entryDelay so cards
      // don't stagger-fade in every time the user undoes/redoes.
      const finalNodes = opts.silent
        ? built.map((n) => (
            n.data && typeof n.data === 'object'
              ? { ...n, data: { ...n.data, entryDelay: 0 } as typeof n.data }
              : n
          ))
        : built;
      setNodes(finalNodes);
      if (autoPositioned.length > 0) {
        patchCanvasPositions(autoPositioned).catch(() => { /* best effort */ });
      }
      setOriginLookup(data.clients_index ?? {});
      // Skip fitView on silent reloads — viewport must stay exactly where
      // the user had it. (Also skip when a saved viewport is restoring.)
      const hasSavedViewport = (() => {
        try {
          const raw = localStorage.getItem('so:sandbox:viewport');
          if (!raw) return false;
          const v = JSON.parse(raw);
          return typeof v?.x === 'number' && typeof v?.y === 'number' && typeof v?.zoom === 'number';
        } catch { return false; }
      })();
      if (built.length > 0 && !hasSavedViewport && !opts.silent) {
        setTimeout(() => fitView({ padding: 0.35, duration: 300, maxZoom: 0.85 }), 80);
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Failed to load canvas');
    } finally {
      if (!opts.silent) setLoading(false);
    }
  }, [setNodes, fitView]);

  useEffect(() => { void loadCanvas(); }, [loadCanvas]);

  /** After a new client is created, scroll/zoom the canvas to focus on it
   *  so the user sees the result of their action instead of an unrelated
   *  area of the graph. Polls briefly because the node may not exist in
   *  state yet when loadCanvas is still in flight. */
  const centerOnClientId = useCallback((numericId: number) => {
    const nodeId = `client_${numericId}`;
    const tryFocus = (attempt = 0) => {
      const node = nodesRef.current.find((n) => n.id === nodeId);
      if (node) {
        // Select it so it visually pops + center it under the viewport
        setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === nodeId })));
        setCenter(node.position.x + 144, node.position.y + 110, { zoom: 1, duration: 600 });
        // Persist the auto-assigned grid slot so reloads land in the same
        // place. Routed through a ref because savePosition is declared later
        // in this component but we capture it at call time.
        savePositionRef.current?.(nodeId, node.position.x, node.position.y);
        return;
      }
      if (attempt < 20) setTimeout(() => tryFocus(attempt + 1), 100);
    };
    tryFocus();
  }, [setCenter, setNodes]);

  // Refresh the canvas when a new capture completes
  useEffect(() => {
    const onCapture = () => void loadCanvas();
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
    // Only entries with a redo fn go to the redo stack
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

  // Global Cmd/Ctrl+Z (undo) and Cmd/Ctrl+Shift+Z (redo); skip when focus is in an input
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

      // Optimistic UI: strip it now
      setNodes((ns) => ns.filter((n) => n.id !== nodeId));

      if (isClient && !isNaN(numId)) {
        // Backend DELETE returns an undo_token good for 5 minutes; we keep a
        // mutable ref to it because each undo→redo cycle consumes the old
        // token and the redo's new DELETE produces a fresh one. Without this,
        // the second Cmd+Z after a Cmd+Shift+Z would silently fail.
        let currentToken: string | null = null;
        // Suppress entry animation on the restored card so the canvas doesn't
        // flash the staggered pop-in every time the user hits undo.
        const restoreViaReload = () => {
          // Silent reload: no loading spinner, no viewport jump, no entry
          // animation re-fire. Just swap the data underneath.
          void loadCanvas({ silent: true });
        };

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
                // Strip from canvas immediately, then re-delete on backend
                // and capture the FRESH undo_token so the next Cmd+Z works.
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
        // Unclassified accounts: we don't have a hard-delete endpoint, so
        // this is a visual-only hide that undo restores from snapshot. The
        // backend row still exists; reload will re-surface it.
        toast.show(`Hidden ${removedName}. Cmd+Z to restore.`, 'info');
        pushUndo({
          label: `Hide account ${removedName}`,
          timestamp: Date.now(),
          undo: () => {
            // Strip the entry-animation delay from the restored node so it
            // doesn't pop in fresh; user just hit undo, they want it back
            // exactly where it was.
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

      // Undo is UI-only — no unmerge API exists; reload will show merged server state
      pushUndo({
        label: `Merge "${oData.client.name}" into "${sData.client.name}"`,
        timestamp: Date.now(),
        undo: () => {
          setNodes(snapshot);
          // NOTE: server state is still merged — this is a view-only revert
        },
        // No redo: can't reliably re-merge after a UI-only undo
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
        // Snap the dragged card back to where it was before the drag started
        // so it doesn't sit on top of the target after the user bails. The
        // backend position never changed (we don't savePosition on a merge
        // drop), so visual-only restore is enough.
        const { sourceId, sourceOrigin } = dlg;
        setNodes((ns) =>
          ns.map((n) => (n.id === sourceId ? { ...n, position: sourceOrigin } : n)),
        );
        dragOriginRef.current.delete(sourceId);
      }
      return null;
    });
  }, [setNodes]);

  // ── Position persistence (debounced 800 ms per node) ─────────────────────

  // Debounced position persistence. We used to wait 800ms but that meant a
  // drag → quick reload sequence would lose the position (the timer never
  // fired before the page tore down). Now we use a short 150ms debounce so
  // rapid drag bursts coalesce but a normal "drop card, reload" sequence
  // always lands the save, AND we flush every pending timer on unload.
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
    // Use sendBeacon for unload-time saves — fetch may be cancelled mid-flight.
    try {
      const blob = new Blob([JSON.stringify(updates)], { type: 'application/json' });
      const ok = navigator.sendBeacon?.('/v1/sandbox/positions', blob);
      if (ok) return;
    } catch { /* fall through to regular save */ }
    patchCanvasPositions(updates).catch(() => { /* best effort */ });
  }, []);

  // Persist any pending drag positions when the user navigates away.
  useEffect(() => {
    const handler = () => flushPositions();
    window.addEventListener('beforeunload', handler);
    window.addEventListener('pagehide', handler);
    return () => {
      window.removeEventListener('beforeunload', handler);
      window.removeEventListener('pagehide', handler);
      // Also flush on component unmount (tab close inside SPA, route change)
      flushPositions();
    };
  }, [flushPositions]);

  const savePosition = useCallback((nodeId: string, x: number, y: number) => {
    // Snap client positions to the canonical grid so they survive density
    // tier changes + look tidy. Accounts (unclassified pile) save raw coords.
    let finalX = x, finalY = y;
    if (nodeId.startsWith('client_')) {
      const CANON_W = GRID.full.COL_W;
      const CANON_H = GRID.full.ROW_H;
      const col = Math.max(0, Math.round((x - 40) / CANON_W));
      const row = Math.max(0, Math.round((y - 40) / CANON_H));
      finalX = col * CANON_W + 40;
      finalY = row * CANON_H + 40;
    }
    pendingPosRef.current.set(nodeId, { x: finalX, y: finalY });
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
      }]).catch(() => { /* position drift is acceptable; corrected on next reload */ });
    }, 150));
  }, []);
  savePositionRef.current = savePosition;

  // ── Drag: live merge-intent highlight ────────────────────────────────────

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

  // ── Drag stop: detect attach / merge, then persist position ──────────────

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

      // Not a merge / attach drop — clear any cached origin and persist.
      dragOriginRef.current.delete(node.id);
      savePosition(node.id, node.position.x, node.position.y);
    },
    [getIntersectingNodes, attachToClient, savePosition, clearMergeIntent],
  );

  // ── Auto-arrange ──────────────────────────────────────────────────────────

  const autoArrange = useCallback(() => {
    const { COL_W: colW, ROW_H: rowH } = GRID[density];
    setNodes((ns) => {
      const clientNodes = ns.filter((n) => n.type === 'client');
      const uncNodes = ns.filter((n) => n.type === 'unclassified');
      return [
        ...clientNodes.map((n, i) => ({
          ...n,
          position: { x: (i % COLS) * colW + 40, y: Math.floor(i / COLS) * rowH + 40 },
        })),
        ...uncNodes.map((n, i) => ({
          ...n,
          position: { x: COLS * colW + 80, y: i * 240 + 40 },
        })),
      ];
    });
    setTimeout(() => fitView({ padding: 0.35, duration: 400, maxZoom: 0.85 }), 80);
  }, [density, setNodes, fitView]);

  // ── Density change ────────────────────────────────────────────────────────

  // Flag is set only on explicit user toolbar action so auto-arrange fires
  // after the state update settles with the new density constants, but NOT
  // on initial load (which would stomp saved card positions).
  const userDensityActionRef = useRef(false);

  const handleDensityChange = useCallback((override: DensityOverride) => {
    userDensityActionRef.current = true;
    setDensityOverride(override);
    try { localStorage.setItem('so:sandbox:density', override); } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    if (!userDensityActionRef.current) return;
    userDensityActionRef.current = false;
    autoArrange();
  }, [density, autoArrange]);

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
                    accounts: [...dstData.client.accounts, moved],
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
      // Filter narrows to JUST this login group: same utility AND same origin
      // AND (when provided) same login id (customer_number || account_number).
      const detachedAccounts = d.client.accounts.filter((a) => {
        if (a.utility !== utility) return false;
        const aOrigin =
          a.login_origin_client_id != null && a.login_origin_client_id !== ownNumId
            ? a.login_origin_client_id
            : null;
        const want = originClientId ?? null;
        if (aOrigin !== want) return false;
        if (loginId != null) {
          const aLoginId = a.customer_number || a.account_number;
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
            // Strip only the accounts we actually detached, not every account
            // of this utility (which would nuke a sibling login group sharing
            // the same provider).
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
          const aLoginId = a.customer_number || a.account_number;
          if (aLoginId !== loginId) return false;
        }
        return true;
      });
      if (moved.length === 0) return;

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
                    accounts: [...dstData.client.accounts, ...moved],
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

      const dstNumId = parseInt(dstClientId.replace('client_', ''), 10);
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

  // ── Context ───────────────────────────────────────────────────────────────

  const actions: CanvasActions = {
    toggleExpand,
    startRename,
    finishRename,
    cancelRename,
    renamingNodeId,
    deleteNode,
    detachAccount,
    moveAccountToClient,
    detachLogin,
    moveLoginToClient,
    getOriginClient: (cid: number) => originLookup[cid] ?? null,
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
          // Shift-click adds the node to the selection; Shift-drag draws a box-select
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

          {/* Toolbar — top-right */}
          {!loading && !isEmpty && (
            <Panel position="top-right">
              <div className="flex gap-2">
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
                <ToolbarButton onClick={() => setShowAddModal(true)}>
                  + Add empty
                </ToolbarButton>
                <DensityControl
                  override={densityOverride}
                  derived={density}
                  onChange={handleDensityChange}
                />
                <ToolbarButton onClick={autoArrange}>Auto-arrange</ToolbarButton>
                <ToolbarButton onClick={() => fitView({ padding: 0.35, duration: 400, maxZoom: 0.85 })}>
                  Fit to view
                </ToolbarButton>
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
                        // Fire deletes sequentially so each gets its own
                        // undo stack entry (so Cmd+Z peels them off one by one).
                        for (const s of selectedClients) deleteNode(s.id);
                      }}
                      className="rounded-md bg-red-50 px-3 py-1.5 text-xs font-semibold text-red-700 ring-1 ring-red-300 transition-colors hover:bg-red-100"
                      title={`Delete ${n} selected client${n === 1 ? '' : 's'}`}
                    >
                      🗑 Delete {n}
                    </button>
                  );
                })()}
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

        {/* Dev-only sandbox tools — renders nothing in prod (gated by /v1/dev/status) */}
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

        {/* Empty state */}
        {isEmpty && (
          <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-4 bg-zinc-50">
            <div className="text-center">
              <p className="mb-1 text-base font-semibold text-zinc-900">No clients yet</p>
              <p className="text-sm text-zinc-500">
                Log into a utility portal to capture accounts,
                <br />
                or add your first client manually.
              </p>
            </div>
            <button
              type="button"
              className="rounded-xl bg-primary-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-primary-700 active:bg-primary-800"
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
              // Center on the last newly created client so the user actually
              // sees what they just added.
              const focus = newClients[newClients.length - 1];
              if (focus) {
                centerOnClientId(focus.id);
                setLastCapturedClientId(focus.id);
                // Auto-expand the new card so the login row is visible for
                // the walkthrough "captured" pointer.
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
            setShowAddModal(true);
          }}
        />
        <AddClientModal
          open={showAddModal}
          onClose={() => setShowAddModal(false)}
          onCreated={(client) => {
            setShowAddModal(false);
            void loadCanvas();
            const ncId = client.id;
            const ncName = client.name;
            pushUndo({
              label: `Add client "${ncName}"`,
              timestamp: Date.now(),
              undo: () => {
                deleteClient(ncId)
                  .then(() => void loadCanvas())
                  .catch(() => toast.show('Undo add client failed.', 'error'));
              },
            });
            centerOnClientId(ncId);
          }}
        />

        {/* Walkthrough — only once canvas is loaded and has at least one client */}
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
            onOpenManual={() => setShowAddModal(true)}
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

function DensityControl({
  override,
  derived,
  onChange,
}: {
  override: DensityOverride;
  derived: Density;
  onChange: (v: DensityOverride) => void;
}) {
  const opts: { key: DensityOverride; label: string }[] = [
    {
      key: 'auto',
      label: override === 'auto'
        ? `Auto · ${derived.charAt(0).toUpperCase() + derived.slice(1)}`
        : 'Auto',
    },
    { key: 'full',    label: 'Full' },
    { key: 'compact', label: 'Compact' },
    { key: 'dense',   label: 'Dense' },
  ];
  return (
    <div className="flex overflow-hidden rounded-lg border border-cream-border divide-x divide-cream-border">
      {opts.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          className={[
            'px-2.5 py-1.5 text-xs font-medium transition-colors',
            override === key
              ? 'bg-primary-50 text-primary-800'
              : 'bg-white text-zinc-600 hover:bg-zinc-50 active:bg-zinc-100',
          ].join(' ')}
          onClick={() => onChange(key)}
          title={
            key === 'auto'
              ? `Auto-select density (currently ${derived})`
              : `${key.charAt(0).toUpperCase() + key.slice(1)} cards`
          }
        >
          {label}
        </button>
      ))}
    </div>
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
