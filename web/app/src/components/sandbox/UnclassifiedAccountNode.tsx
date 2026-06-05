import { type NodeProps } from '@xyflow/react';
import { type Utility, type UtilityAccount } from './mockData';

// Extends Record<string, unknown> so it satisfies Node<NodeData> generic constraint
export interface UnclassifiedNodeData extends Record<string, unknown> {
  account: UtilityAccount;
  entryDelay: number;
}

const UTILITY_THEME: Record<
  Utility,
  { border: string; header: string; icon: string; label: string; dot: string }
> = {
  GMP: {
    border: 'border-emerald-200',
    header: 'bg-emerald-50',
    icon: '☀',
    label: 'text-emerald-700',
    dot: 'bg-emerald-400',
  },
  VEC: {
    border: 'border-blue-200',
    header: 'bg-blue-50',
    icon: '⚡',
    label: 'text-blue-700',
    dot: 'bg-blue-400',
  },
  WEC: {
    border: 'border-amber-200',
    header: 'bg-amber-50',
    icon: '⊕',
    label: 'text-amber-700',
    dot: 'bg-amber-400',
  },
};

export function UnclassifiedNodeComponent({ data: rawData, selected }: NodeProps) {
  const data = rawData as unknown as UnclassifiedNodeData;
  const { account, entryDelay } = data;
  const th = UTILITY_THEME[account.utility];

  return (
    <div
      className={[
        'so-node-enter w-60 rounded-2xl border-2 border-dashed bg-white/90 transition-shadow',
        selected ? `${th.border} shadow-md` : `${th.border} shadow-sm hover:shadow`,
      ].join(' ')}
      style={{ animationDelay: `${entryDelay}ms` }}
    >
      {/* Header */}
      <div className={`flex items-center gap-2 rounded-t-xl px-3 pt-3 pb-2.5 ${th.header}`}>
        <span className={`shrink-0 text-sm font-medium ${th.label}`} aria-hidden>
          {th.icon}
        </span>
        <div className="min-w-0 flex-1">
          <p className={`text-xs font-semibold ${th.label}`}>
            {account.utility} · {account.account_number}
          </p>
          <p className="truncate text-[10px] text-zinc-400">{account.owner_name}</p>
        </div>
      </div>

      {/* Arrays */}
      <div className="space-y-1 px-3 py-2.5">
        {account.arrays.map((arr) => (
          <div key={arr.id} className="flex items-baseline justify-between gap-2">
            <span className="truncate text-[11px] text-zinc-700">{arr.name}</span>
            <span className="shrink-0 text-[10px] tabular-nums text-zinc-400">
              {arr.mwh_per_qtr} MWh
            </span>
          </div>
        ))}
      </div>

      {/* Drag hint */}
      <div className="border-t border-dashed border-zinc-100 px-3 py-2">
        <p className="text-center text-[10px] leading-snug text-zinc-400">
          Drag onto a client card to attach
        </p>
      </div>
    </div>
  );
}
