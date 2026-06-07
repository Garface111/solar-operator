import { useEffect, useRef, useState, useCallback } from 'react';
import { type NodeProps } from '@xyflow/react';
import { useCanvasActions } from './canvasContext';
import {
  type ClientData,
  type SolarArray,
  type Utility,
  type UtilityAccount,
} from './mockData';
import { restoreArray } from '../../lib/api';
import { useToast } from '../../ui/Toast';

// Lookup for card width class per density (also applies to dense-expanded state)
const CARD_WIDTH: Record<string, string> = {
  full: 'w-72',
  compact: 'w-56',
  dense: 'w-44',
  'dense-expanded': 'w-72',
};

// Extends Record<string, unknown> so it satisfies Node<NodeData> generic constraint
export interface ClientNodeData extends Record<string, unknown> {
  client: ClientData;
  expanded: boolean;
  entryDelay: number;
  // Transient drag state — set during onNodeDrag when this node is hovering
  // over another client (source) or being hovered (target). Used purely for
  // styling; never persisted.
  mergeIntent?: 'source' | 'target' | null;
}

const UTILITY_THEME: Record<Utility, { pill: string; dot: string; row: string; rowText: string; rowDot: string }> = {
  GMP: {
    pill: 'bg-emerald-100 text-emerald-600',
    dot: 'bg-emerald-400',
    row: 'bg-emerald-50 border-emerald-100',
    rowText: 'text-emerald-600',
    rowDot: 'bg-emerald-400',
  },
  VEC: {
    pill: 'bg-blue-100 text-blue-800',
    dot: 'bg-blue-400',
    row: 'bg-blue-50 border-blue-100',
    rowText: 'text-blue-700',
    rowDot: 'bg-blue-400',
  },
  WEC: {
    pill: 'bg-amber-100 text-amber-800',
    dot: 'bg-amber-400',
    row: 'bg-amber-50 border-amber-100',
    rowText: 'text-amber-700',
    rowDot: 'bg-amber-400',
  },
};

// Box-shadow values for the utility-colored drop indicator ring.
// Inline style avoids Tailwind purging dynamic class names.
const UTILITY_RING_CSS: Record<Utility, string> = {
  GMP: '0 0 0 2px rgba(52,211,153,0.85), 0 10px 28px -8px rgba(52,211,153,0.4)',
  VEC: '0 0 0 2px rgba(96,165,250,0.85), 0 10px 28px -8px rgba(96,165,250,0.4)',
  WEC: '0 0 0 2px rgba(251,191,36,0.85), 0 10px 28px -8px rgba(251,191,36,0.4)',
};

// Module-level tracker: which login utility is currently being dragged.
// Set on dragstart of the six-dot handle, cleared on dragend.
// Used by cards to determine which ring color to show on hover.
let _activeDragLoginUtility: Utility | null = null;

function getInitials(name: string): string {
  return name
    .split(/\s+/)
    .filter((w) => w.length > 0)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join('');
}

function utilityChips(accounts: UtilityAccount[]): Array<{ util: Utility; count: number }> {
  const counts: Record<Utility, number> = { GMP: 0, VEC: 0, WEC: 0 };
  for (const acc of accounts) counts[acc.utility]++;
  return (['GMP', 'VEC', 'WEC'] as Utility[])
    .filter((u) => counts[u] > 0)
    .map((u) => ({ util: u, count: counts[u] }));
}

export function ClientNodeComponent({ id, data: rawData, selected }: NodeProps) {
  const data = rawData as unknown as ClientNodeData;
  const { client, expanded, entryDelay, mergeIntent } = data;
  const actions = useCanvasActions();
  const { density } = actions;
  const [localName, setLocalName] = useState(client.name);
  const inputRef = useRef<HTMLInputElement>(null);
  const isRenaming = actions.renamingNodeId === id;
  // Dense mode: local expand toggle (shows full card inline, others stay dense)
  const [denseExpanded, setDenseExpanded] = useState(false);
  useEffect(() => { if (density !== 'dense') setDenseExpanded(false); }, [density]);

  useEffect(() => {
    if (!isRenaming) setLocalName(client.name);
  }, [client.name, isRenaming]);

  useEffect(() => {
    if (isRenaming && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isRenaming]);

  const chips = utilityChips(client.accounts);
  const ownClientNumId = (() => {
    const m = id.match(/^client_(\d+)$/);
    return m ? parseInt(m[1], 10) : null;
  })();
  // arrayCount + totalMwh were used by the footer indicator (removed Jun 6).

  const isMergeTarget = mergeIntent === 'target';
  const isMergeSource = mergeIntent === 'source';

  // ── Native drop target for cross-client drags ────────────────────────────
  const [dropHoverType, setDropHoverType] = useState<'account' | 'login' | null>(null);
  const [dropHoverUtility, setDropHoverUtility] = useState<Utility | null>(null);

  const onDragOver = (e: React.DragEvent) => {
    const isAccount = e.dataTransfer.types.includes('application/x-so-account');
    const isLogin = e.dataTransfer.types.includes('application/x-so-login');
    if (!isAccount && !isLogin) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    if (isLogin) {
      setDropHoverType('login');
      setDropHoverUtility(_activeDragLoginUtility);
    } else {
      setDropHoverType('account');
      setDropHoverUtility(null);
    }
  };
  const onDragLeave = (e: React.DragEvent) => {
    if (e.currentTarget === e.target) {
      setDropHoverType(null);
      setDropHoverUtility(null);
    }
  };
  const onDrop = (e: React.DragEvent) => {
    const accountRaw = e.dataTransfer.getData('application/x-so-account');
    const loginRaw = e.dataTransfer.getData('application/x-so-login');
    if (!accountRaw && !loginRaw) return;
    e.preventDefault();
    e.stopPropagation();
    setDropHoverType(null);
    setDropHoverUtility(null);
    try {
      if (accountRaw) {
        const { srcClientId, accountId } = JSON.parse(accountRaw) as { srcClientId: string; accountId: string };
        if (srcClientId === id) return;
        actions.moveAccountToClient(srcClientId, accountId, id);
      } else if (loginRaw) {
        const { srcClientId, utility, originClientId, loginId } = JSON.parse(loginRaw) as {
          srcClientId: string;
          utility: 'GMP' | 'VEC' | 'WEC';
          originClientId?: number | null;
          loginId?: string | null;
        };
        if (srcClientId === id) return;
        actions.moveLoginToClient(srcClientId, utility, id, originClientId, loginId);
      }
    } catch {
      /* malformed payload — ignore */
    }
  };

  // Auto-expand the card after 400ms of hovering with a login drag
  const autoExpandTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (dropHoverType !== 'login' || expanded || density === 'dense') {
      if (autoExpandTimerRef.current !== null) {
        clearTimeout(autoExpandTimerRef.current);
        autoExpandTimerRef.current = null;
      }
      return;
    }
    autoExpandTimerRef.current = setTimeout(() => {
      autoExpandTimerRef.current = null;
      actions.toggleExpand(id);
    }, 400);
    return () => {
      if (autoExpandTimerRef.current !== null) {
        clearTimeout(autoExpandTimerRef.current);
        autoExpandTimerRef.current = null;
      }
    };
  }, [dropHoverType, expanded, density, id, actions]);

  // Effective width key for the card
  const widthKey = density === 'dense' ? (denseExpanded ? 'dense-expanded' : 'dense') : density;
  const cardWidth = CARD_WIDTH[widthKey];
  const dropHover = dropHoverType !== null;

  const baseCardClass = [
    'so-node-enter rounded-2xl border-[1.5px] bg-white transition-all duration-150',
    cardWidth,
    client.pinned && !isMergeTarget && !isMergeSource && !dropHover
      ? 'ring-2 ring-amber-300/60 shadow-[0_0_0_2px_rgba(251,191,36,0.18)]'
      : '',
    isMergeTarget
      ? 'scale-[1.03] border-amber-400 bg-amber-50 shadow-[0_0_0_4px_rgba(251,191,36,0.25),0_12px_32px_-8px_rgba(217,119,6,0.4)] ring-2 ring-amber-300'
      : isMergeSource
        ? 'border-amber-300 opacity-90 shadow-[0_0_0_3px_rgba(251,191,36,0.2)]'
        : dropHover
          ? dropHoverType === 'login'
            ? 'scale-[1.02] border-zinc-300'
            : 'scale-[1.02] border-primary-400 bg-primary-50/40 shadow-[0_0_0_3px_rgba(132,204,22,0.25),0_10px_28px_-8px_rgba(132,204,22,0.4)] ring-2 ring-primary-300'
          : selected
            ? 'border-primary-400 shadow-md ring-2 ring-primary-300/40'
            : 'border-zinc-300 shadow-[0_4px_14px_-2px_rgba(15,23,42,0.12),0_2px_4px_-1px_rgba(15,23,42,0.06)] hover:shadow-[0_8px_24px_-4px_rgba(15,23,42,0.16),0_3px_6px_-1px_rgba(15,23,42,0.08)] hover:border-zinc-400',
  ].join(' ');

  // Inline box-shadow for login-drop utility color (can't use Tailwind for dynamic values)
  const dropInlineStyle: React.CSSProperties =
    dropHoverType === 'login' && dropHoverUtility
      ? { boxShadow: UTILITY_RING_CSS[dropHoverUtility] }
      : {};

  // ── Dense card (collapsed) ─────────────────────────────────────────────────
  if (density === 'dense' && !denseExpanded) {
    return (
      <div
        className={baseCardClass}
        style={{ animationDelay: `${entryDelay}ms`, ...dropInlineStyle }}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        <div className="flex items-center gap-1.5 px-2 py-2">
          <button
            type="button"
            className={[
              'nodrag nopan flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[10px] font-bold select-none transition-colors',
              client.pinned
                ? 'bg-amber-100 text-amber-700 hover:bg-amber-200'
                : 'bg-primary-50 text-primary-700 hover:bg-amber-50 hover:text-amber-600',
            ].join(' ')}
            title={client.pinned ? 'Pinned — click to unpin' : 'Click to pin to top'}
            onClick={(e) => { e.stopPropagation(); actions.togglePin(id); }}
          >
            {client.pinned ? '★' : getInitials(client.name)}
          </button>
          <p
            className="nodrag min-w-0 flex-1 truncate text-[11px] font-semibold text-zinc-900 select-none cursor-text hover:text-primary-700"
            title={client.name}
            onDoubleClick={() => actions.startRename(id)}
          >
            {client.name}
          </p>
          {chips.length > 0 && (
            <span className="shrink-0 text-[10px] font-medium text-zinc-500 tabular-nums" title={chips.map(c => `${c.util}: ${c.count}`).join(', ')}>
              {chips.map(c => `${c.util}:${c.count}`).join(' ')}
            </span>
          )}
          <button
            type="button"
            className="nodrag shrink-0 rounded p-0.5 text-zinc-400 transition-colors hover:text-zinc-700"
            onClick={() => setDenseExpanded(true)}
            aria-label="Expand"
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
            </svg>
          </button>
        </div>
      </div>
    );
  }

  // ── Full card (used for full, compact, and dense-expanded) ─────────────────
  const isCompact = density === 'compact';
  const isDenseExpanded = density === 'dense' && denseExpanded;
  const headerPad = isCompact ? 'px-3 pt-3 pb-2' : 'px-4 pt-4 pb-3';
  const avatarSize = isCompact ? 'h-8 w-8 text-[10px]' : 'h-9 w-9 text-xs';
  const labelText = isCompact ? 'text-[9px]' : 'text-[10px]';
  const nameText  = isCompact ? 'text-xs' : 'text-sm';
  const chipGap   = isCompact ? 'gap-1 px-3 pb-2' : 'gap-1.5 px-4 pb-3';
  const loginPad  = isCompact ? 'px-2 pb-2 space-y-1.5' : 'px-3 pb-3 space-y-2';

  return (
    <div
      data-walkthrough="client-card"
      data-walkthrough-client-id={id}
      className={baseCardClass}
      style={{ animationDelay: `${entryDelay}ms`, ...dropInlineStyle }}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      {/* Header */}
      <div className={`flex items-center gap-3 ${headerPad}`}>
        <button
          type="button"
          className={[
            `nodrag nopan flex ${avatarSize} shrink-0 items-center justify-center rounded-full font-bold select-none transition-colors`,
            client.pinned
              ? 'bg-amber-100 text-amber-700 hover:bg-amber-200'
              : 'bg-primary-50 text-primary-700 hover:bg-amber-50 hover:text-amber-600',
          ].join(' ')}
          title={client.pinned ? 'Pinned — click to unpin' : 'Click to pin to top'}
          onClick={(e) => { e.stopPropagation(); actions.togglePin(id); }}
        >
          {client.pinned ? '★' : getInitials(client.name)}
        </button>

        <div className="min-w-0 flex-1">
          <span className={`block ${labelText} font-semibold uppercase tracking-wider text-primary-600/80 select-none`}>
            Client
          </span>
          {isRenaming ? (
            <input
              ref={inputRef}
              className={`nodrag nopan w-full rounded border border-primary-300 bg-primary-50 px-1.5 py-0.5 ${nameText} font-semibold text-zinc-900 outline-none focus:ring-2 focus:ring-primary-400/40`}
              value={localName}
              onChange={(e) => setLocalName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') actions.finishRename(id, localName.trim() || client.name);
                if (e.key === 'Escape') actions.cancelRename();
              }}
              onBlur={() => actions.finishRename(id, localName.trim() || client.name)}
            />
          ) : (
            <p
              className={`nodrag cursor-text truncate ${nameText} font-semibold text-zinc-900 select-none hover:text-primary-700 hover:underline hover:underline-offset-2 decoration-primary-300`}
              onClick={(e) => { e.stopPropagation(); actions.startRename(id); }}
              title={`${client.name} — click to rename`}
            >
              {client.name}
            </p>
          )}
          {/* Contact email — click to edit inline. Always rendered so it
              shows as a tappable placeholder when empty. */}
          <ClientEmailInline
            value={(client as { contact_email?: string | null }).contact_email ?? null}
            onSave={(v) => void actions.updateClient(id, { contact_email: v })}
            isCompact={isCompact}
          />
        </div>

        <button
          type="button"
          className="nodrag shrink-0 rounded p-0.5 text-zinc-400 transition-colors hover:text-zinc-700"
          onClick={() => isDenseExpanded ? setDenseExpanded(false) : actions.toggleExpand(id)}
          aria-label={expanded || isDenseExpanded ? 'Collapse' : 'Expand'}
        >
          <svg
            className={`h-4 w-4 transition-transform duration-200 ${(expanded || isDenseExpanded) ? 'rotate-180' : ''}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </button>
      </div>

      {/* Collapsed: utility chip row */}
      {!expanded && !isDenseExpanded && chips.length > 0 && (
        <div className={`flex flex-wrap ${chipGap}`}>
          {chips.map(({ util, count }) => {
            const th = UTILITY_THEME[util];
            return (
              <span
                key={util}
                className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${th.pill}`}
              >
                <span className={`h-1.5 w-1.5 rounded-full ${th.dot}`} />
                {util} · {count}
              </span>
            );
          })}
        </div>
      )}

      {/* Expanded: one row per utility login */}
      {(expanded || isDenseExpanded) && (
        <div
          className={`nowheel max-h-[420px] overflow-y-auto ${loginPad} overscroll-contain`}
          style={{ scrollbarGutter: 'stable' }}
        >
          {groupAccountsByLogin(client.accounts, ownClientNumId).map((group) => (
            <LoginGroupRow
              key={group.key}
              clientId={id}
              utility={group.utility}
              accounts={group.accounts}
              loginCredential={
                group.originClientId != null
                  ? actions.getOriginClient(group.originClientId)?.logins?.[group.utility] ?? null
                  : client.logins?.[group.utility] ?? null
              }
              onDetach={(accountId) => actions.detachAccount(id, accountId)}
              onDetachLogin={() => actions.detachLogin(id, group.utility, group.originClientId, group.loginId)}
              originClientId={group.originClientId}
              loginId={group.loginId}
            />
          ))}
        </div>
      )}

      {/* Footer removed (MWh/qtr indicator was visual noise — Jun 6) */}
    </div>
  );
}

function groupAccountsByLogin(
  accounts: UtilityAccount[],
  ownClientNumId: number | null,
): { utility: Utility; originClientId: number | null; loginId: string; accounts: UtilityAccount[]; key: string }[] {
  const groups = new Map<string, { utility: Utility; originClientId: number | null; loginId: string; accounts: UtilityAccount[] }>();
  for (const acc of accounts) {
    const origin =
      acc.login_origin_client_id != null && acc.login_origin_client_id !== ownClientNumId
        ? acc.login_origin_client_id
        : null;
    const key = `${acc.utility}::${origin ?? 'home'}`;
    const entry = groups.get(key) ?? {
      utility: acc.utility,
      originClientId: origin,
      loginId: `${acc.utility}-${origin ?? 'home'}`,
      accounts: [],
    };
    entry.accounts.push(acc);
    groups.set(key, entry);
  }
  return Array.from(groups.entries())
    .map(([key, v]) => ({ ...v, key }))
    .sort((a, b) => {
      const ord = (['GMP', 'VEC', 'WEC'] as Utility[]).indexOf(a.utility) -
                  (['GMP', 'VEC', 'WEC'] as Utility[]).indexOf(b.utility);
      if (ord !== 0) return ord;
      if (a.originClientId == null && b.originClientId != null) return -1;
      if (a.originClientId != null && b.originClientId == null) return 1;
      return (a.originClientId ?? 0) - (b.originClientId ?? 0);
    });
}

/** Shows "Moved just now" for up to 10 s after the server-stamped reassigned_at. */
function MovedBadge({ reassignedAt }: { reassignedAt: string }) {
  const [visible, setVisible] = useState(() => Date.now() - new Date(reassignedAt).getTime() < 10000);
  useEffect(() => {
    const age = Date.now() - new Date(reassignedAt).getTime();
    if (age >= 10000) { setVisible(false); return; }
    const t = setTimeout(() => setVisible(false), 10000 - age);
    return () => clearTimeout(t);
  }, [reassignedAt]);
  if (!visible) return null;
  return (
    <span className="shrink-0 rounded bg-emerald-100 px-1.5 py-0.5 text-[9px] font-semibold text-emerald-600">
      Moved just now
    </span>
  );
}

function LoginGroupRow({
  clientId,
  utility,
  accounts,
  loginCredential,
  originClientId,
  loginId,
  onDetach,
  onDetachLogin,
}: {
  clientId: string;
  utility: Utility;
  accounts: UtilityAccount[];
  loginCredential: string | null;
  originClientId: number | null;
  loginId: string;
  onDetach: (accountId: string) => void;
  onDetachLogin: () => void;
}) {
  // Bruce Jun 6: keep login row expanded by default. Two pain points solved
  // at once: (1) dragging sub-arrays to another client used to close the
  // dropdown after each drop so he had to keep re-clicking LOGIN GMP;
  // (2) clicking into a client card forced an extra "LOGIN GMP" click to
  // even see the arrays. The login row is the *only* thing inside the card
  // body, so defaulting to expanded is sublime — arrays are visible the
  // instant the card opens, and dragging across multiple sub-arrays keeps
  // them in view the whole time.
  const [expanded, setExpanded] = useState(true);
  const [dragging, setDragging] = useState(false);
  const [draggingArrayId, setDraggingArrayId] = useState<string | null>(null);
  const th = UTILITY_THEME[utility];
  const accountCount = accounts.length;
  const arrayTotal = accounts.reduce((n, a) => n + a.arrays.length, 0);

  // Count how many accounts in this login group share each array id (sub-meter detection).
  const subMeterCounts = new Map<string, number>();
  accounts.forEach((a) => {
    a.arrays.forEach((ar) => {
      subMeterCounts.set(ar.id, (subMeterCounts.get(ar.id) ?? 0) + 1);
    });
  });

  // Ref to the row body so we can use it as the drag image (so the whole
  // login row drags visually, not just the six-dot handle).
  const rowRef = useRef<HTMLDivElement | null>(null);

  const onDragStart = (e: React.DragEvent) => {
    e.stopPropagation();
    _activeDragLoginUtility = utility;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData(
      'application/x-so-login',
      JSON.stringify({ srcClientId: clientId, utility, originClientId, loginId }),
    );
    e.dataTransfer.setData('text/plain', `${utility} login`);
    // Use the row body as the drag image so the user sees the whole login
    // following the cursor, not just the tiny handle icon.
    if (rowRef.current) {
      const rect = rowRef.current.getBoundingClientRect();
      e.dataTransfer.setDragImage(rowRef.current, e.clientX - rect.left, e.clientY - rect.top);
    }
    setDragging(true);
  };

  const onDragEnd = () => {
    _activeDragLoginUtility = null;
    setDragging(false);
  };

  // Array-level drag handlers
  const onArrayDragStart = useCallback((e: React.DragEvent, accId: string, arrId: string, arrName: string, accNumber: string, subMeterCount: number) => {
    e.stopPropagation();
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData(
      'application/x-so-array',
      JSON.stringify({ srcClientId: clientId, arrayId: arrId, accountId: accId, accountNumber: accNumber, arrayName: arrName, subMeterCount }),
    );
    e.dataTransfer.setData('text/plain', arrName);
    setDraggingArrayId(arrId);
  }, [clientId]);

  const onArrayDragEnd = useCallback(() => {
    setDraggingArrayId(null);
  }, []);

  // Prevent ReactFlow from starting a node drag when interacting with login rows
  const onMouseDown = (e: React.MouseEvent) => {
    e.stopPropagation();
  };

  return (
    <div
      data-walkthrough="login-row"
      ref={rowRef}
      className={[
        'nodrag group/login rounded-xl border transition-opacity',
        th.row,
        dragging ? 'opacity-40' : '',
      ].join(' ')}
      onMouseDown={onMouseDown}
    >
      {/* Login header */}
      <div className="flex items-center gap-1.5 px-3 py-2.5">
        {/* Six-dot drag handle — the ONLY draggable surface on the login row */}
        <span
          className={`nodrag nopan shrink-0 cursor-grab active:cursor-grabbing rounded p-0.5 opacity-40 hover:opacity-80 transition-opacity ${th.rowText}`}
          draggable
          onDragStart={onDragStart}
          onDragEnd={onDragEnd}
          onMouseDown={(e) => { e.stopPropagation(); }}
          onPointerDown={(e) => { e.stopPropagation(); }}
          onPointerDownCapture={(e) => { e.stopPropagation(); }}
          title="Drag to move this login to another client"
          aria-label="Drag handle"
        >
          <svg width="10" height="14" viewBox="0 0 10 14" fill="currentColor" aria-hidden>
            <circle cx="2" cy="2" r="1.2"/>
            <circle cx="8" cy="2" r="1.2"/>
            <circle cx="2" cy="7" r="1.2"/>
            <circle cx="8" cy="7" r="1.2"/>
            <circle cx="2" cy="12" r="1.2"/>
            <circle cx="8" cy="12" r="1.2"/>
          </svg>
        </span>

        {/* Click anywhere on the content area to toggle expand — row body is click-only */}
        <div
          role="button"
          tabIndex={0}
          className="flex flex-1 items-center gap-1.5 text-left cursor-pointer"
          onClick={() => setExpanded((v) => !v)}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded((v) => !v); } }}
          aria-expanded={expanded}
          aria-label={expanded ? 'Collapse login' : 'Expand login'}
        >
          <svg
            className={`h-3 w-3 shrink-0 transition-transform duration-200 ${expanded ? 'rotate-90' : ''} ${th.rowText}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2.5}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
          <span className={`h-2 w-2 rounded-full ${th.rowDot}`} />
          <span className={`text-[10px] font-semibold uppercase tracking-wider ${th.rowText}`}>
            Login
          </span>
          <span className={`text-xs font-semibold ${th.rowText}`}>
            {utility}
          </span>
          <span className={`ml-auto text-[10px] font-medium opacity-60 ${th.rowText}`}>
            {accountCount} {accountCount === 1 ? 'account' : 'accounts'} · {arrayTotal} {arrayTotal === 1 ? 'array' : 'arrays'}
          </span>
        </div>
        <button
          type="button"
          className={`nodrag invisible shrink-0 rounded p-0.5 text-sm opacity-60 transition-all hover:opacity-100 group-hover/login:visible ${th.rowText}`}
          onClick={onDetachLogin}
          title={`Detach entire ${utility} login`}
          aria-label={`Detach entire ${utility} login`}
        >
          ×
        </button>
      </div>
      {expanded && (
        <div className="nowheel max-h-72 overflow-y-auto mx-3 mb-2.5 space-y-1 border-t border-current/10 pt-2 overscroll-contain">
          {loginCredential && (
            <div className={`flex items-center gap-1.5 rounded-md bg-white/70 px-2 py-1 text-[11px] ${th.rowText}`}>
              <span className="text-[10px] font-semibold uppercase tracking-wider opacity-60">
                Signed in as
              </span>
              <span className="truncate font-medium text-zinc-800" title={loginCredential}>
                {loginCredential}
              </span>
            </div>
          )}
          {accounts.flatMap((acc) =>
            acc.arrays.length > 0
              ? acc.arrays.map((arr) => arr.deleted_at
                  ? (
                      <DeletedArrayRow
                        key={`${acc.id}-${arr.id}-deleted`}
                        arr={arr}
                        clientId={clientId}
                        onRestored={() => {
                          // Mark as optimistically restored — canvas will refresh
                          // via so:arrays-changed; the ghost row disappears immediately.
                        }}
                      />
                    )
                  : (
                  <div
                    key={`${acc.id}-${arr.id}`}
                    className={[
                      'group/arr flex items-center gap-1.5 rounded-md bg-white/70 px-1.5 py-1.5 transition-opacity',
                      draggingArrayId === arr.id ? 'opacity-50' : '',
                    ].join(' ')}
                  >
                    {/* Six-dot drag handle — only draggable surface for the array row */}
                    <span
                      className={`nodrag nopan shrink-0 cursor-grab active:cursor-grabbing rounded p-0.5 opacity-30 hover:opacity-70 transition-opacity ${th.rowText}`}
                      draggable
                      onDragStart={(e) => onArrayDragStart(e, acc.id, arr.id, arr.name, acc.account_number, subMeterCounts.get(arr.id) ?? 1)}
                      onDragEnd={onArrayDragEnd}
                      onMouseDown={(e) => e.stopPropagation()}
                      onPointerDown={(e) => e.stopPropagation()}
                      title="Drag to move this array to another client"
                      aria-label="Drag array"
                    >
                      <svg width="10" height="14" viewBox="0 0 10 14" fill="currentColor" aria-hidden>
                        <circle cx="2" cy="2" r="1.2"/>
                        <circle cx="8" cy="2" r="1.2"/>
                        <circle cx="2" cy="7" r="1.2"/>
                        <circle cx="8" cy="7" r="1.2"/>
                        <circle cx="2" cy="12" r="1.2"/>
                        <circle cx="8" cy="12" r="1.2"/>
                      </svg>
                    </span>
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${th.rowDot} opacity-60`} />
                    <ArrayNameCell arr={arr} />
                    {arr.reassigned_at && <MovedBadge reassignedAt={arr.reassigned_at} />}
                    <button
                      type="button"
                      className={`nodrag invisible ml-auto shrink-0 rounded p-0.5 text-xs opacity-60 transition-all hover:opacity-100 group-hover/arr:visible ${th.rowText}`}
                      onClick={() => onDetach(acc.id)}
                      title="Detach this account from the client"
                      aria-label="Detach account"
                    >
                      ×
                    </button>
                  </div>
                  ))
              : [
                  <div
                    key={`${acc.id}-empty`}
                    className="group/arr flex items-center gap-2 rounded-md bg-white/50 px-2 py-1.5 text-zinc-500"
                  >
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${th.rowDot} opacity-30`} />
                    <span className="truncate text-xs italic">
                      {acc.account_number} · no arrays yet
                    </span>
                    <button
                      type="button"
                      className={`nodrag invisible ml-auto shrink-0 rounded p-0.5 text-xs opacity-60 transition-all hover:opacity-100 group-hover/arr:visible ${th.rowText}`}
                      onClick={() => onDetach(acc.id)}
                      title="Detach this account from the client"
                      aria-label="Detach account"
                    >
                      ×
                    </button>
                  </div>,
                ],
          )}
          {/* Account-number ID strip removed — visual noise (Jun 6) */}
        </div>
      )}
    </div>
  );
}


const PURGE_GRACE_MS = 30 * 24 * 60 * 60 * 1000;

/** Days remaining before the array is permanently purged (ceil so "1" means less than 2 full days). */
function daysUntilPurge(deletedAt: string): number {
  const purgeAt = new Date(deletedAt).getTime() + PURGE_GRACE_MS;
  return Math.ceil((purgeAt - Date.now()) / (24 * 60 * 60 * 1000));
}

/** Extracts numeric array id from strings like "arr_42" or "arr_u_7". Returns NaN if not parseable. */
function numericArrayId(arrId: string): number {
  return parseInt(arrId.replace(/^arr_/, ''), 10);
}

/** Extracts numeric client id from strings like "client_42". Returns NaN if not parseable. */
function numericClientId(clientId: string): number {
  return parseInt(clientId.replace(/^client_/, ''), 10);
}

/** Ghost row for a soft-deleted array — shows name struck-through, purge countdown chip, and Restore button. */
function DeletedArrayRow({
  arr,
  clientId,
  onRestored,
}: {
  arr: SolarArray;
  clientId: string;
  onRestored: (arr: SolarArray) => void;
}) {
  const toast = useToast();
  const [optimisticRestored, setOptimisticRestored] = useState(false);
  const days = arr.deleted_at ? daysUntilPurge(arr.deleted_at) : 0;

  if (optimisticRestored) return null;

  const isLastChance = days <= 1;
  const chipCls = isLastChance
    ? 'bg-red-50 text-red-600 border border-red-200'
    : 'bg-wood-100 text-wood-700 border border-wood-300';
  const chipLabel = days <= 0 ? 'Purging soon' : days === 1 ? 'Purges tomorrow' : `Purges in ${days}d`;

  async function handleRestore(e: React.MouseEvent) {
    e.stopPropagation();
    const cid = numericClientId(clientId);
    const aid = numericArrayId(arr.id);
    if (isNaN(cid) || isNaN(aid)) return;

    // Optimistic: hide the ghost row immediately
    setOptimisticRestored(true);
    onRestored(arr);
    try {
      await restoreArray(cid, aid);
      window.dispatchEvent(new CustomEvent('so:arrays-changed'));
    } catch (err: unknown) {
      // Revert optimistic state
      setOptimisticRestored(false);
      const status = (err as { status?: number })?.status;
      if (status === 410) {
        toast.error('This array was already purged. Re-add it manually.');
      } else {
        toast.error("Couldn't restore — try again.");
      }
    }
  }

  return (
    <div
      className="group/arr flex items-center gap-1.5 rounded-md border border-dashed border-zinc-300 bg-zinc-50 px-1.5 py-1.5 opacity-45 transition-opacity hover:opacity-70"
      title={`Deleted array — purges ${days <= 0 ? 'soon' : `in ${days} day${days === 1 ? '' : 's'}`}`}
    >
      {/* Spacer matching the six-dot drag handle width — no drag for deleted rows */}
      <span className="shrink-0 w-[18px]" />
      <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-zinc-400 opacity-60" />
      <span className="min-w-0 flex-1 truncate text-xs font-medium text-zinc-500 line-through">
        {arr.name}
      </span>
      <span className={`nodrag shrink-0 rounded px-1.5 py-0.5 text-[9px] font-semibold ${chipCls}`}>
        {chipLabel}
      </span>
      <button
        type="button"
        className="nodrag shrink-0 rounded border border-zinc-300 bg-white px-1.5 py-0.5 text-[9px] font-semibold text-zinc-600 opacity-80 transition-all hover:border-primary-400 hover:bg-primary-50 hover:text-primary-700 hover:opacity-100"
        onClick={handleRestore}
        title="Restore this array"
        aria-label="Restore array"
      >
        Restore
      </button>
    </div>
  );
}

/** Inline-editable array name. Click to rename; Enter/blur commits; Escape cancels. */
function ArrayNameCell({ arr }: { arr: SolarArray }) {
  const actions = useCanvasActions();
  const numericId = parseInt(arr.id.replace('arr_', ''), 10);
  const isRenamingThis = !isNaN(numericId) && actions.renamingNodeId === `array_${numericId}`;
  const [draft, setDraft] = useState(arr.name);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!isRenamingThis) setDraft(arr.name);
  }, [arr.name, isRenamingThis]);

  useEffect(() => {
    if (isRenamingThis && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isRenamingThis]);

  if (isRenamingThis) {
    return (
      <input
        ref={inputRef}
        className="nodrag nopan min-w-0 flex-1 truncate rounded border border-primary-300 bg-primary-50 px-1 py-0.5 text-xs font-medium text-zinc-800 outline-none focus:ring-2 focus:ring-primary-400/40"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { e.preventDefault(); actions.finishRenameArray(numericId, draft.trim() || arr.name); }
          if (e.key === 'Escape') actions.cancelRename();
        }}
        onBlur={() => actions.finishRenameArray(numericId, draft.trim() || arr.name)}
      />
    );
  }

  return (
    <button
      type="button"
      className="nodrag cursor-text min-w-0 flex-1 truncate text-left text-xs font-medium text-zinc-800 hover:text-primary-700 hover:underline hover:underline-offset-2 decoration-primary-300"
      title={arr.nepool_gis_id ? `${arr.name} · ${arr.nepool_gis_id}` : arr.name}
      onClick={(e) => { e.stopPropagation(); if (!isNaN(numericId)) actions.startRenameArray(numericId); }}
    >
      {arr.name}
    </button>
  );
}

/** Inline-editable contact email shown in the client card header.
 *  Click to edit; Enter or blur commits; Escape cancels. Empty state
 *  reads as a soft amber "+ add email" prompt. */
function ClientEmailInline({
  value,
  onSave,
  isCompact,
}: {
  value: string | null;
  onSave: (v: string) => void;
  isCompact: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!editing) setDraft(value ?? "");
  }, [value, editing]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const sizeText = isCompact ? "text-[10px]" : "text-[11px]";

  function commit() {
    const v = draft.trim();
    setEditing(false);
    if (v === (value ?? "")) return; // no-op
    onSave(v);
  }

  if (editing) {
    return (
      <input
        ref={inputRef}
        type="email"
        className={`nodrag nopan mt-0.5 w-full rounded border border-primary-300 bg-primary-50 px-1.5 py-0.5 ${sizeText} text-zinc-800 outline-none focus:ring-2 focus:ring-primary-400/40`}
        value={draft}
        placeholder="client@example.com"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); commit(); }
          if (e.key === "Escape") { setDraft(value ?? ""); setEditing(false); }
        }}
        onBlur={commit}
      />
    );
  }

  return value ? (
    <button
      type="button"
      className={`nodrag mt-0.5 block max-w-full truncate text-left ${sizeText} text-zinc-500 hover:text-primary-700 hover:underline hover:underline-offset-2`}
      onClick={(e) => { e.stopPropagation(); setEditing(true); }}
      title={`${value} — click to edit`}
    >
      {value}
    </button>
  ) : (
    <button
      type="button"
      className={`nodrag mt-0.5 block text-left ${sizeText} font-medium text-amber-600 hover:text-amber-700 hover:underline hover:underline-offset-2`}
      onClick={(e) => { e.stopPropagation(); setEditing(true); }}
      title="Click to add a contact email"
    >
      + add email
    </button>
  );
}
