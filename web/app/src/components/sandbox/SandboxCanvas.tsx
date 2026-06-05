import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Panel,
  ReactFlow,
  useNodesState,
  useReactFlow,
  type Node,
  type NodeTypes,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { CanvasActionsContext, type CanvasActions } from './canvasContext';
import { ClientNodeComponent, type ClientNodeData } from './ClientNode';
import { UnclassifiedNodeComponent, type UnclassifiedNodeData } from './UnclassifiedAccountNode';
import { type ClientData, type UtilityAccount, type Utility } from './mockData';
import {
  getCanvasData,
  mergeClientInto,
  patchCanvasPositions,
  pinClient,
  reassignAccount,
  type CanvasResponse,
} from '../../lib/api';
import { useToast } from '../../ui/Toast';
import { Spinner } from '../../ui/Spinner';
import { AddClientModal } from '../AddClientModal';
import { AddClientByLoginModal } from '../AddClientByLoginModal';
import { listClients } from '../../lib/api';

// ── Node type registry (stable reference — must live outside component) ────

const NODE_TYPES: NodeTypes = {
  client: ClientNodeComponent,
  unclassified: UnclassifiedNodeComponent,
};

// ── Layout constants ────────────────────────────────────────────────────────

const COLS = 4;
const COL_W = 330;
const ROW_H = 295;

// ── Provider normalizer ─────────────────────────────────────────────────────

function normalizeProvider(p: string): Utility {
  const u = p.toUpperCase() as Utility;
  return (['GMP', 'VEC', 'WEC'] as Utility[]).includes(u) ? u : 'GMP';
}

// ── API → React Flow nodes ──────────────────────────────────────────────────

function buildNodesFromApi(data: CanvasResponse): Node[] {
  const nodes: Node[] = [];
  let autoClientIdx = 0;
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
    let position: { x: number; y: number };
    if (hasPos) {
      position = { x: client.canvas_x!, y: client.canvas_y! };
    } else {
      const idx = autoClientIdx++;
      position = { x: (idx % COLS) * COL_W + 40, y: Math.floor(idx / COLS) * ROW_H + 40 };
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
      position = { x: COLS * COL_W + 80, y: idx * 240 + 40 };
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
}

interface ContextMenu {
  x: number;
  y: number;
  nodeId: string;
  nodeType: string;
}

// ── Component ───────────────────────────────────────────────────────────────

export default function SandboxCanvas() {
  const toast = useToast();
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [mergeDialog, setMergeDialog] = useState<MergeDialog | null>(null);
  const [mergeUndo, setMergeUndo] = useState<{ label: string; snapshot: Node[] } | null>(null);
  const [renamingNodeId, setRenamingNodeId] = useState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<ContextMenu | null>(null);
  const [showAddModal, setShowAddModal] = useState(false);
  // The toolbar Add Client opens the portal-picker first (same flow as the
  // list view's primary CTA). Manual entry is the fallback.
  const [showAddByLogin, setShowAddByLogin] = useState(false);
  // Lookup map for origin clients (populated from /v1/sandbox/canvas
  // response.clients_index). Used by LoginGroupRow to label moved logins.
  const [originLookup, setOriginLookup] = useState<NonNullable<CanvasResponse['clients_index']>>({});

  const { getIntersectingNodes, fitView } = useReactFlow();

  // Always-fresh ref used in callbacks to avoid stale closures
  const nodesRef = useRef<Node[]>(nodes);
  nodesRef.current = nodes;

  // Per-node debounce timers for position persistence
  const posTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  // Auto-dismiss merge undo after 5 s
  useEffect(() => {
    if (!mergeUndo) return;
    const t = setTimeout(() => setMergeUndo(null), 5000);
    return () => clearTimeout(t);
  }, [mergeUndo]);

  // ── Data loading ──────────────────────────────────────────────────────────

  const loadCanvas = useCallback(async () => {
    setLoadError(null);
    setLoading(true);
    try {
      const data = await getCanvasData();
      const built = buildNodesFromApi(data);
      setNodes(built);
      setOriginLookup(data.clients_index ?? {});
      if (built.length > 0) {
        setTimeout(() => fitView({ padding: 0.35, duration: 300, maxZoom: 0.85 }), 80);
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Failed to load canvas');
    } finally {
      setLoading(false);
    }
  }, [setNodes, fitView]);

  useEffect(() => { void loadCanvas(); }, [loadCanvas]);

  // Refresh the canvas when a new capture completes
  useEffect(() => {
    const onCapture = () => void loadCanvas();
    window.addEventListener('so:capture-cleared', onCapture);
    return () => window.removeEventListener('so:capture-cleared', onCapture);
  }, [loadCanvas]);

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
      setNodes((ns) =>
        ns.map((n) => {
          if (n.id !== nodeId || n.type !== 'client') return n;
          const d = n.data as ClientNodeData;
          return { ...n, data: { ...d, client: { ...d.client, name } } };
        }),
      );
    },
    [setNodes],
  );

  const cancelRename = useCallback(() => setRenamingNodeId(null), []);

  const deleteNode = useCallback(
    (nodeId: string) => {
      setContextMenu(null);
      setNodes((ns) => ns.filter((n) => n.id !== nodeId));
      toast.show('Removed from canvas.', 'info');
    },
    [setNodes, toast],
  );

  const detachAccount = useCallback(
    (clientId: string, accountId: string) => {
      const current = nodesRef.current;
      const clientNode = current.find((n) => n.id === clientId && n.type === 'client');
      if (!clientNode) return;
      const d = clientNode.data as ClientNodeData;
      const detached = d.client.accounts.find((a) => a.id === accountId);
      if (!detached) return;

      // Optimistic UI: pop the account out as an unclassified node next to the client.
      const snapshot = current;
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

      toast.show(`${detached.utility} · ${detached.account_number} detached.`, 'info');

      // Persist if this is a real backend account
      const numId = parseInt(accountId.replace('account_', ''), 10);
      if (!isNaN(numId)) {
        reassignAccount(numId, null).catch(() => {
          setNodes(snapshot);
          toast.show('Detach failed — reverted.', 'error');
        });
      }
    },
    [setNodes, toast],
  );

  const attachToClient = useCallback(
    (unclassifiedId: string, targetClientId: string) => {
      const current = nodesRef.current;
      const unclNode = current.find((n) => n.id === unclassifiedId && n.type === 'unclassified');
      const clientNode = current.find((n) => n.id === targetClientId && n.type === 'client');
      if (!unclNode || !clientNode) return;

      const uData = unclNode.data as UnclassifiedNodeData;
      const cData = clientNode.data as ClientNodeData;

      // Optimistic UI
      const snapshot = current;
      setNodes((ns) => {
        const updatedClient: ClientData = {
          ...cData.client,
          accounts: [...cData.client.accounts, uData.account],
        };
        return ns
          .filter((n) => n.id !== unclassifiedId)
          .map((n) => n.id === targetClientId ? { ...n, data: { ...cData, client: updatedClient } } : n);
      });

      toast.show(
        `${uData.account.utility} · ${uData.account.account_number} added to ${cData.client.name}.`,
        'success',
      );

      // Persist
      const accNumId = parseInt(unclassifiedId.replace('account_', ''), 10);
      const clientNumId = parseInt(targetClientId.replace('client_', ''), 10);
      if (!isNaN(accNumId) && !isNaN(clientNumId)) {
        reassignAccount(accNumId, clientNumId)
          .then(() => { void loadCanvas(); })  // resync to get the new array_id
          .catch(() => {
            setNodes(snapshot);
            toast.show('Move failed — reverted.', 'error');
          });
      }
    },
    [setNodes, toast, loadCanvas],
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

      // Optimistic update so the UI feels instant
      setNodes((ns) =>
        ns
          .filter((n) => n.id !== otherId)
          .map((n) => n.id === survivorId ? { ...n, data: { ...sData, client: merged } } : n),
      );
      setMergeUndo({ label: `Merged into "${sData.client.name}".`, snapshot });
      setMergeDialog(null);

      // Persist if both are real backend-persisted client nodes
      if (survivorId.startsWith('client_') && otherId.startsWith('client_')) {
        const srcId = parseInt(otherId.replace('client_', ''), 10);
        const dstId = parseInt(survivorId.replace('client_', ''), 10);
        try {
          await mergeClientInto(srcId, dstId);
          // Re-sync from server to capture credential-merge effects
          void loadCanvas();
        } catch (_err) {
          setNodes(snapshot);
          setMergeUndo(null);
          toast.show('Merge failed — changes reverted.', 'error');
        }
      }
    },
    [mergeDialog, setNodes, loadCanvas, toast],
  );

  const cancelMerge = useCallback(() => setMergeDialog(null), []);

  // ── Position persistence (debounced 800 ms per node) ─────────────────────

  const savePosition = useCallback((nodeId: string, x: number, y: number) => {
    const existing = posTimers.current.get(nodeId);
    if (existing) clearTimeout(existing);
    posTimers.current.set(nodeId, setTimeout(() => {
      posTimers.current.delete(nodeId);
      const isClient = nodeId.startsWith('client_');
      const isAccount = nodeId.startsWith('account_');
      if (!isClient && !isAccount) return;
      const numId = parseInt(nodeId.replace(isClient ? 'client_' : 'account_', ''), 10);
      if (isNaN(numId)) return;
      patchCanvasPositions([{
        node_type: isClient ? 'client' : 'account',
        node_id: numId,
        x,
        y,
      }]).catch(() => { /* position drift is acceptable; corrected on next reload */ });
    }, 800));
  }, []);

  // ── Drag: live merge-intent highlight ────────────────────────────────────

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
          setMergeDialog({
            sourceId: node.id,
            targetId: target.id,
            sourceName: nData.client.name,
            targetName: tData.client.name,
          });
          return;
        }
      }

      savePosition(node.id, node.position.x, node.position.y);
    },
    [getIntersectingNodes, attachToClient, savePosition, clearMergeIntent],
  );

  // ── Auto-arrange ──────────────────────────────────────────────────────────

  const autoArrange = useCallback(() => {
    setNodes((ns) => {
      const clientNodes = ns.filter((n) => n.type === 'client');
      const uncNodes = ns.filter((n) => n.type === 'unclassified');
      return [
        ...clientNodes.map((n, i) => ({
          ...n,
          position: { x: (i % COLS) * COL_W + 40, y: Math.floor(i / COLS) * ROW_H + 40 },
        })),
        ...uncNodes.map((n, i) => ({
          ...n,
          position: { x: COLS * COL_W + 80, y: i * 240 + 40 },
        })),
      ];
    });
    setTimeout(() => fitView({ padding: 0.35, duration: 400, maxZoom: 0.85 }), 80);
  }, [setNodes, fitView]);

  // ── Context ───────────────────────────────────────────────────────────────

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

      // Optimistic UI: swap the account from src to dst
      const snapshot = current;
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

      toast.show(
        `${moved.utility} · ${moved.account_number} → ${dstData.client.name}.`,
        'success',
      );

      const accNumId = parseInt(accountId.replace('account_', ''), 10);
      const dstNumId = parseInt(dstClientId.replace('client_', ''), 10);
      if (!isNaN(accNumId) && !isNaN(dstNumId)) {
        reassignAccount(accNumId, dstNumId)
          .then(() => { void loadCanvas(); })  // resync so arr_id/holder array reflect server truth
          .catch(() => {
            setNodes(snapshot);
            toast.show('Move failed — reverted.', 'error');
          });
      }
    },
    [setNodes, toast, loadCanvas],
  );

  // ── Login-level (group) actions ──────────────────────────────────────────

  const detachLogin = useCallback(
    (clientId: string, utility: 'GMP' | 'VEC' | 'WEC', originClientId?: number | null) => {
      const current = nodesRef.current;
      const clientNode = current.find((n) => n.id === clientId && n.type === 'client');
      if (!clientNode) return;
      const d = clientNode.data as ClientNodeData;
      const ownNumId = (() => {
        const m = clientId.match(/^client_(\d+)$/);
        return m ? parseInt(m[1], 10) : null;
      })();
      // Filter narrows to JUST this login group: same utility AND same origin
      // (where origin == own id means "home" group).
      const detachedAccounts = d.client.accounts.filter((a) => {
        if (a.utility !== utility) return false;
        const aOrigin =
          a.login_origin_client_id != null && a.login_origin_client_id !== ownNumId
            ? a.login_origin_client_id
            : null;
        const want = originClientId ?? null;
        return aOrigin === want;
      });
      if (detachedAccounts.length === 0) return;

      const snapshot = current;
      // Optimistic UI: strip the whole login from the client; pop accounts
      // out as unclassified nodes fanned to the right of the client card.
      setNodes((ns) => {
        const updatedClient: ClientData = {
          ...d.client,
          accounts: d.client.accounts.filter((a) => a.utility !== utility),
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

      toast.show(
        `${utility} login (${detachedAccounts.length} ${detachedAccounts.length === 1 ? 'account' : 'accounts'}) detached.`,
        'info',
      );

      // Persist — fan out reassignAccount(null) per account; collect failures
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
    },
    [setNodes, toast],
  );

  const moveLoginToClient = useCallback(
    (srcClientId: string, utility: 'GMP' | 'VEC' | 'WEC', dstClientId: string, originClientId?: number | null) => {
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
      // Same narrowing as detachLogin: only THIS group's accounts move.
      const moved = srcData.client.accounts.filter((a) => {
        if (a.utility !== utility) return false;
        const aOrigin =
          a.login_origin_client_id != null && a.login_origin_client_id !== srcOwnNumId
            ? a.login_origin_client_id
            : null;
        const want = originClientId ?? null;
        return aOrigin === want;
      });
      if (moved.length === 0) return;

      const snapshot = current;
      setNodes((ns) =>
        ns.map((n) => {
          if (n.id === srcClientId) {
            return {
              ...n,
              data: {
                ...srcData,
                client: {
                  ...srcData.client,
                  accounts: srcData.client.accounts.filter((a) => a.utility !== utility),
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
    },
    [setNodes, toast, loadCanvas],
  );

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
    togglePin: (clientId: string) => {
      const numId = parseInt(clientId.replace('client_', ''), 10);
      if (isNaN(numId)) return;
      const current = nodesRef.current;
      const node = current.find((n) => n.id === clientId && n.type === 'client');
      if (!node) return;
      const wasPinned = !!(node.data as ClientNodeData).client.pinned;
      const next = !wasPinned;
      // Optimistic UI
      setNodes((ns) =>
        ns.map((n) => {
          if (n.id !== clientId) return n;
          const d = n.data as ClientNodeData;
          return { ...n, data: { ...d, client: { ...d.client, pinned: next } } };
        }),
      );
      toast.show(next ? 'Pinned to top.' : 'Unpinned.', 'info');
      pinClient(numId, next).catch(() => {
        // Revert on failure
        setNodes((ns) =>
          ns.map((n) => {
            if (n.id !== clientId) return n;
            const d = n.data as ClientNodeData;
            return { ...n, data: { ...d, client: { ...d.client, pinned: wasPinned } } };
          }),
        );
        toast.show('Pin failed — reverted.', 'error');
      });
    },
  };

  // ── Render ────────────────────────────────────────────────────────────────

  const isEmpty = !loading && !loadError && nodes.length === 0;

  return (
    <CanvasActionsContext.Provider value={actions}>
      <div className="relative h-full w-full">
        <ReactFlow
          nodes={nodes}
          edges={[]}
          onNodesChange={onNodesChange}
          onNodeDrag={onNodeDrag}
          onNodeDragStop={onNodeDragStop}
          nodeTypes={NODE_TYPES}
          nodesConnectable={false}
          {...(() => {
            // Restore viewport from localStorage if present; otherwise fitView once.
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
          <Background variant={BackgroundVariant.Dots} color="#d4d4d8" gap={22} size={1.5} />

          <MiniMap
            nodeColor={(n) => (n.type === 'client' ? '#047857' : '#a1a1aa')}
            maskColor="rgba(250, 248, 245, 0.75)"
            style={{
              borderRadius: 12,
              border: '1px solid #e8e2d9',
              boxShadow: '0 1px 3px 0 rgb(0 0 0 / 0.06)',
            }}
          />

          <Controls showInteractive={false} />

          {/* Toolbar — top-right */}
          {!loading && !isEmpty && (
            <Panel position="top-right">
              <div className="flex gap-2">
                <ToolbarButton onClick={() => setShowAddByLogin(true)}>+ Add Client</ToolbarButton>
                <ToolbarButton onClick={autoArrange}>Auto-arrange</ToolbarButton>
                <ToolbarButton onClick={() => fitView({ padding: 0.35, duration: 400, maxZoom: 0.85 })}>
                  Fit to view
                </ToolbarButton>
                <button
                  type="button"
                  disabled={!mergeUndo}
                  onClick={() => { if (mergeUndo) { setNodes(mergeUndo.snapshot); setMergeUndo(null); } }}
                  className={[
                    'rounded-md px-3 py-1.5 text-xs font-semibold transition-colors',
                    mergeUndo
                      ? 'bg-amber-50 text-amber-800 hover:bg-amber-100 ring-1 ring-amber-300'
                      : 'bg-cream-bg text-zinc-400 ring-1 ring-cream-border cursor-not-allowed',
                  ].join(' ')}
                  title={mergeUndo ? mergeUndo.label + ' (⌘Z)' : 'Nothing to undo'}
                >
                  ↶ Undo
                </button>
              </div>
            </Panel>
          )}

          {/* Merge undo banner — bottom-center */}
          {mergeUndo && (
            <Panel position="bottom-center">
              <div className="mb-2 flex items-center gap-4 rounded-xl border border-zinc-200 bg-white px-5 py-3 text-sm shadow-md">
                <span className="text-zinc-700">{mergeUndo.label}</span>
                <button
                  type="button"
                  className="font-semibold text-primary-600 transition-colors hover:text-primary-800"
                  onClick={() => { setNodes(mergeUndo.snapshot); setMergeUndo(null); }}
                >
                  Undo
                </button>
              </div>
            </Panel>
          )}
        </ReactFlow>

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
              onClick={() => setShowAddByLogin(true)}
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
            onClose={() => setContextMenu(null)}
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
          onCreated={(_client) => { setShowAddModal(false); void loadCanvas(); }}
        />
      </div>
    </CanvasActionsContext.Provider>
  );
}

// ── Small presentational sub-components ────────────────────────────────────

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
  onClose,
}: {
  menu: ContextMenu;
  onStartRename: () => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  return (
    <div
      style={{ position: 'fixed', left: menu.x, top: menu.y, zIndex: 9999 }}
      className="min-w-[160px] overflow-hidden rounded-xl border border-zinc-200 bg-white py-1 shadow-xl"
      onMouseLeave={onClose}
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
