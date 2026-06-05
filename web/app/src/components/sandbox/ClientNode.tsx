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
  const totalMwh = clientTotalMwh(client);

  const isMergeTarget = mergeIntent === 'target';
  const isMergeSource = mergeIntent === 'source';

  return (
    <div
      className={[
        'so-node-enter w-72 rounded-2xl border bg-white transition-all duration-150',
        isMergeTarget
          ? 'scale-[1.03] border-amber-400 bg-amber-50 shadow-[0_0_0_4px_rgba(251,191,36,0.25),0_12px_32px_-8px_rgba(217,119,6,0.4)] ring-2 ring-amber-300'
          : isMergeSource
            ? 'border-amber-300 opacity-90 shadow-[0_0_0_3px_rgba(251,191,36,0.2)]'
            : selected
              ? 'border-primary-400 shadow-md ring-2 ring-primary-300/40'
              : 'border-cream-border shadow hover:shadow-md',
      ].join(' ')}
      style={{ animationDelay: `${entryDelay}ms` }}
    >
      {/* Header */}
      <div className="flex items-center gap-3 px-4 pt-4 pb-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary-50 text-xs font-bold text-primary-700 select-none">
          {getInitials(client.name)}
        </div>

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
              className="nodrag cursor-text truncate text-sm font-semibold text-zinc-900 select-none"
              onDoubleClick={() => actions.startRename(id)}
              title={client.name}
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
      {expanded && (
        <div className="space-y-2 px-3 pb-3">
          {client.accounts.map((acc) => (
            <AccountRow
              key={acc.id}
              account={acc}
              onDetach={() => actions.detachAccount(id, acc.id)}
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

function AccountRow({
  account,
  onDetach,
}: {
  account: UtilityAccount;
  onDetach: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const th = UTILITY_THEME[account.utility];
  const arrayCount = account.arrays.length;
  const hasArrays = arrayCount > 0;
  return (
    <div className={`group/acc rounded-xl border px-3 py-2.5 ${th.row}`}>
      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          className="nodrag flex flex-1 items-center gap-1.5 text-left"
          onClick={() => hasArrays && setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-label={expanded ? 'Collapse arrays' : 'Expand arrays'}
          disabled={!hasArrays}
        >
          {hasArrays && (
            <svg
              className={`h-3 w-3 shrink-0 transition-transform duration-200 ${expanded ? 'rotate-90' : ''} ${th.rowText}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2.5}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
            </svg>
          )}
          <span className={`h-2 w-2 rounded-full ${th.rowDot}`} />
          <span className={`text-xs font-semibold ${th.rowText}`}>
            {account.utility} · {account.account_number}
          </span>
          <span className={`text-[10px] font-medium opacity-60 ${th.rowText}`}>
            {arrayCount} {arrayCount === 1 ? 'array' : 'arrays'}
          </span>
        </button>
        <button
          type="button"
          className={`nodrag invisible shrink-0 rounded p-0.5 text-sm opacity-60 transition-all hover:opacity-100 group-hover/acc:visible ${th.rowText}`}
          onClick={onDetach}
          title="Detach account"
          aria-label="Detach account"
        >
          ×
        </button>
      </div>
      {expanded && hasArrays && (
        <div className="mt-2 space-y-1 border-t border-current/10 pt-2">
          {account.arrays.map((arr) => (
            <div
              key={arr.id}
              className="flex items-center justify-between gap-2 rounded-md bg-white/70 px-2 py-1"
            >
              <div className="flex min-w-0 items-center gap-1.5">
                <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                  Array
                </span>
                <span className="truncate text-xs font-medium text-zinc-800" title={arr.name}>
                  {arr.name}
                </span>
              </div>
              {arr.nepool_gis_id && (
                <span className="shrink-0 rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[10px] text-zinc-600">
                  {arr.nepool_gis_id}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
