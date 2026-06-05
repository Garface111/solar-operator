import { useEffect, useRef, useState } from 'react';
import { type NodeProps } from '@xyflow/react';
import { useCanvasActions } from './canvasContext';
import {
  clientArrayCount,
  clientTotalMwh,
  type ClientData,
  type Utility,
  type UtilityAccount,
} from './mockData';

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
    pill: 'bg-emerald-100 text-emerald-800',
    dot: 'bg-emerald-400',
    row: 'bg-emerald-50 border-emerald-100',
    rowText: 'text-emerald-700',
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
  const [localName, setLocalName] = useState(client.name);
  const inputRef = useRef<HTMLInputElement>(null);
  const isRenaming = actions.renamingNodeId === id;

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
  const arrayCount = clientArrayCount(client);
  // The numeric DB id of this client — used to tell groupAccountsByLogin
  // which login_origin tags are "this is your home" (clear) vs. "moved in
  // from elsewhere" (separate group).
  const ownClientNumId = (() => {
    const m = id.match(/^client_(\d+)$/);
    return m ? parseInt(m[1], 10) : null;
  })();
  const totalMwh = clientTotalMwh(client);

  const isMergeTarget = mergeIntent === 'target';
  const isMergeSource = mergeIntent === 'source';

  // ── Native drop target for cross-client account drags ────────────────────
  const [dropHover, setDropHover] = useState(false);
  const onDragOver = (e: React.DragEvent) => {
    if (
      !e.dataTransfer.types.includes('application/x-so-account') &&
      !e.dataTransfer.types.includes('application/x-so-login')
    ) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    if (!dropHover) setDropHover(true);
  };
  const onDragLeave = (e: React.DragEvent) => {
    // Only clear when leaving the card itself, not crossing a child boundary
    if (e.currentTarget === e.target) setDropHover(false);
  };
  const onDrop = (e: React.DragEvent) => {
    const accountRaw = e.dataTransfer.getData('application/x-so-account');
    const loginRaw = e.dataTransfer.getData('application/x-so-login');
    if (!accountRaw && !loginRaw) return;
    e.preventDefault();
    e.stopPropagation();
    setDropHover(false);
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

  return (
    <div
      data-walkthrough="client-card"
      data-walkthrough-client-id={id}
      className={[
        'so-node-enter w-72 rounded-2xl border-[1.5px] bg-white transition-all duration-150',
        client.pinned && !isMergeTarget && !isMergeSource && !dropHover
          ? 'ring-2 ring-amber-300/60 shadow-[0_0_0_2px_rgba(251,191,36,0.18)]'
          : '',
        isMergeTarget
          ? 'scale-[1.03] border-amber-400 bg-amber-50 shadow-[0_0_0_4px_rgba(251,191,36,0.25),0_12px_32px_-8px_rgba(217,119,6,0.4)] ring-2 ring-amber-300'
          : isMergeSource
            ? 'border-amber-300 opacity-90 shadow-[0_0_0_3px_rgba(251,191,36,0.2)]'
            : dropHover
              ? 'scale-[1.02] border-primary-400 bg-primary-50/40 shadow-[0_0_0_3px_rgba(132,204,22,0.25),0_10px_28px_-8px_rgba(132,204,22,0.4)] ring-2 ring-primary-300'
              : selected
                ? 'border-primary-400 shadow-md ring-2 ring-primary-300/40'
                : 'border-zinc-300 shadow-[0_4px_14px_-2px_rgba(15,23,42,0.12),0_2px_4px_-1px_rgba(15,23,42,0.06)] hover:shadow-[0_8px_24px_-4px_rgba(15,23,42,0.16),0_3px_6px_-1px_rgba(15,23,42,0.08)] hover:border-zinc-400',
      ].join(' ')}
      style={{ animationDelay: `${entryDelay}ms` }}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      {/* Header */}
      <div className="flex items-center gap-3 px-4 pt-4 pb-3">
        <button
          type="button"
          className={[
            'nodrag nopan flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-xs font-bold select-none transition-colors',
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
          <span className="block text-[10px] font-semibold uppercase tracking-wider text-primary-600/80 select-none">
            Client
          </span>
          {isRenaming ? (
            <input
              ref={inputRef}
              className="nodrag nopan w-full rounded border border-primary-300 bg-primary-50 px-1.5 py-0.5 text-sm font-semibold text-zinc-900 outline-none focus:ring-2 focus:ring-primary-400/40"
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
              className="nodrag cursor-text truncate text-sm font-semibold text-zinc-900 select-none hover:text-primary-700 hover:underline hover:underline-offset-2 decoration-primary-300"
              onDoubleClick={() => actions.startRename(id)}
              title={`${client.name} — double-click to rename`}
            >
              {client.name}
            </p>
          )}
        </div>

        <button
          type="button"
          className="nodrag shrink-0 rounded p-0.5 text-zinc-400 transition-colors hover:text-zinc-700"
          onClick={() => actions.toggleExpand(id)}
          aria-label={expanded ? 'Collapse' : 'Expand'}
        >
          <svg
            className={`h-4 w-4 transition-transform duration-200 ${expanded ? 'rotate-180' : ''}`}
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
      {!expanded && chips.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-4 pb-3">
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

      {/* Expanded: account rows */}
      {/* Expanded: one row per utility login (groups accounts by provider).
          `nowheel` tells React Flow to ignore wheel events targeting this
          subtree — so the operator can scroll a long login list with the
          wheel instead of the canvas zooming out underneath them. */}
      {expanded && (
        <div
          className="nowheel max-h-[420px] overflow-y-auto space-y-2 px-3 pb-3 overscroll-contain"
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
              originClient={
                group.originClientId != null
                  ? actions.getOriginClient(group.originClientId)
                  : null
              }
              onDetach={(accountId) => actions.detachAccount(id, accountId)}
              onDetachLogin={() => actions.detachLogin(id, group.utility, group.originClientId, group.loginId)}
              originClientId={group.originClientId}
              loginId={group.loginId}
            />
          ))}
        </div>
      )}

      {/* Footer */}
      <div className="border-t border-cream-border px-4 py-2.5">
        <p className="text-xs text-zinc-400">
          {arrayCount} {arrayCount === 1 ? 'array' : 'arrays'} · {totalMwh} MWh/qtr
        </p>
      </div>
    </div>
  );
}

function groupAccountsByLogin(
  accounts: UtilityAccount[],
  ownClientNumId: number | null,
): { utility: Utility; originClientId: number | null; loginId: string; accounts: UtilityAccount[]; key: string }[] {
  // A "login" = one credential at the utility portal (e.g. one GMP web
  // account). The utility assigns DIFFERENT customer_numbers / account_numbers
  // to each metered account under that login, so we MUST NOT split by them —
  // doing so creates one row per array instead of one row per credential.
  //
  // Key by (utility, login_origin_client_id || own) only. Two same-utility
  // logins under one client (a real but rare case — would require a separate
  // Login table to model properly) currently collapse here; that's an
  // intentional regression vs the customer_number split, because the user
  // flow is "one capture = one login = one batch under the client", and a
  // future PR can split when the data model supports it.
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
      // loginId is now a stable label for the group (utility + origin), used
      // for drag payload narrowing. It's NOT a per-account discriminator.
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

function LoginGroupRow({
  clientId,
  utility,
  accounts,
  loginCredential,
  originClient,
  originClientId,
  loginId,
  onDetach,
  onDetachLogin,
}: {
  clientId: string;
  utility: Utility;
  accounts: UtilityAccount[];
  loginCredential: string | null;
  originClient: { id: number; name: string; deleted: boolean } | null;
  originClientId: number | null;
  loginId: string;
  onDetach: (accountId: string) => void;
  onDetachLogin: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [dragging, setDragging] = useState(false);
  const th = UTILITY_THEME[utility];
  const accountCount = accounts.length;
  const arrayTotal = accounts.reduce((n, a) => n + a.arrays.length, 0);

  const onDragStart = (e: React.DragEvent) => {
    e.stopPropagation();
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData(
      'application/x-so-login',
      JSON.stringify({ srcClientId: clientId, utility, originClientId, loginId }),
    );
    e.dataTransfer.setData('text/plain', `${utility} login`);
    setDragging(true);
  };
  const onDragEnd = () => setDragging(false);

  return (
    <div
      data-walkthrough="login-row"
      className={[
        'group/login rounded-xl border px-3 py-2.5 transition-opacity',
        th.row,
        dragging ? 'opacity-40' : '',
      ].join(' ')}
      draggable
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      title="Drag to move all accounts under this login to another client"
    >
      {/* Login header — click to reveal individual accounts */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="nodrag flex flex-1 items-center gap-1.5 text-left"
          onClick={() => setExpanded((v) => !v)}
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
          {originClient && (
            <span
              className={`rounded bg-white/70 px-1.5 py-0.5 text-[10px] font-medium text-zinc-700`}
              title={`Moved here from ${originClient.name}${originClient.deleted ? ' (deleted)' : ''}`}
            >
              from {originClient.name}{originClient.deleted ? ' (deleted)' : ''}
            </span>
          )}
          <span className={`ml-auto text-[10px] font-medium opacity-60 ${th.rowText}`}>
            {accountCount} {accountCount === 1 ? 'account' : 'accounts'} · {arrayTotal} {arrayTotal === 1 ? 'array' : 'arrays'}
          </span>
        </button>
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
        <div className="nowheel max-h-72 overflow-y-auto mt-2 space-y-1 border-t border-current/10 pt-2 overscroll-contain">
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
          {/* Flat list of arrays under this login — Client → Login → Arrays */}
          {accounts.flatMap((acc) =>
            acc.arrays.length > 0
              ? acc.arrays.map((arr) => (
                  <div
                    key={`${acc.id}-${arr.id}`}
                    className="group/arr flex items-center gap-2 rounded-md bg-white/70 px-2 py-1.5"
                  >
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${th.rowDot} opacity-60`} />
                    <span
                      className="truncate text-xs font-medium text-zinc-800"
                      title={arr.nepool_gis_id ? `${arr.name} · ${arr.nepool_gis_id}` : arr.name}
                    >
                      {arr.name}
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
          <div className={`pt-1 text-[10px] font-mono opacity-40 ${th.rowText}`}>
            {accounts.map((a) => `${a.account_number}`).join(' · ')}
          </div>
        </div>
      )}
    </div>
  );
}

