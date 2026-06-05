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
  const { client, expanded, entryDelay } = data;
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

  return (
    <div
      className={[
        'so-node-enter w-72 rounded-2xl border bg-white transition-shadow',
        selected
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
  const th = UTILITY_THEME[account.utility];
  return (
    <div className={`group/acc rounded-xl border px-3 py-2.5 ${th.row}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <span className={`h-2 w-2 rounded-full ${th.rowDot}`} />
          <span className={`text-xs font-semibold ${th.rowText}`}>
            {account.utility} · {account.account_number}
          </span>
        </div>
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
      <div className="mt-1.5 flex flex-wrap gap-1">
        {account.arrays.map((arr) => (
          <span
            key={arr.id}
            className="rounded bg-white/60 px-1.5 py-0.5 text-[10px] text-zinc-600"
            title={arr.nepool_gis_id}
          >
            {arr.name}
          </span>
        ))}
      </div>
    </div>
  );
}
