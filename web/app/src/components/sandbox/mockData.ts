export type Utility = 'GMP' | 'VEC' | 'WEC';

export interface SolarArray {
  id: string;
  name: string;
  nepool_gis_id: string;
  mwh_per_qtr: number;
}

export interface UtilityAccount {
  id: string;
  utility: Utility;
  account_number: string;
  /** Per-utility customer id distinguishing two logins of the same provider
   *  under the same client (e.g. two GMP accounts under separate web logins).
   *  Used purely as a grouping discriminator in ClientNode. */
  customer_number?: string | null;
  owner_name: string;
  arrays: SolarArray[];
  /** Numeric id of the client this account's login originated from. NULL
   *  means it's currently at its original home. Used by the sandbox to
   *  keep moved logins visually separate. */
  login_origin_client_id?: number | null;
}

export interface ClientData {
  id: string;
  name: string;
  /** Contact email — surfaced in the sandbox header for inline edit. */
  contact_email?: string | null;
  accounts: UtilityAccount[];
  /** Per-utility login credential surface (optional; populated from API). */
  logins?: Partial<Record<Utility, string | null>>;
  /** Pinned/starred — sorts to top of any list, renders with a gold star. */
  pinned?: boolean;
}

export function clientTotalMwh(c: ClientData): number {
  return Math.round(
    c.accounts.flatMap((a) => a.arrays).reduce((sum, arr) => sum + arr.mwh_per_qtr, 0),
  );
}

export function clientArrayCount(c: ClientData): number {
  return c.accounts.flatMap((a) => a.arrays).length;
}

function tokenize(s: string): string[] {
  return s
    .toLowerCase()
    .split(/[\s,._-]+/)
    .filter((t) => t.length > 2);
}

function tokenOverlap(a: string, b: string): number {
  const sa = new Set(tokenize(a));
  const sb = new Set(tokenize(b));
  const intersection = [...sa].filter((t) => sb.has(t)).length;
  const union = new Set([...sa, ...sb]).size;
  return union === 0 ? 0 : intersection / union;
}

export interface AutoclusterResult {
  clients: ClientData[];
  remaining: UtilityAccount[];
  clusteredCount: number;
}

export function autocluster(
  clients: ClientData[],
  unclassified: UtilityAccount[],
  threshold = 0.5,
): AutoclusterResult {
  const updated = clients.map((c) => ({ ...c, accounts: [...c.accounts] }));
  const remaining: UtilityAccount[] = [];
  let clusteredCount = 0;

  for (const acc of unclassified) {
    let bestScore = 0;
    let bestIdx = -1;
    for (let i = 0; i < updated.length; i++) {
      const score = tokenOverlap(acc.owner_name, updated[i].name);
      if (score > bestScore) {
        bestScore = score;
        bestIdx = i;
      }
    }
    if (bestScore >= threshold && bestIdx >= 0) {
      updated[bestIdx].accounts.push({ ...acc });
      clusteredCount++;
    } else {
      remaining.push(acc);
    }
  }

  return { clients: updated, remaining, clusteredCount };
}

export const SEED_CLIENTS: ClientData[] = [
  {
    id: 'cli_01',
    name: 'Green Valley Farm',
    accounts: [
      {
        id: 'acc_01a', utility: 'GMP', account_number: '4512-8901', owner_name: 'Green Valley Farm',
        arrays: [
          { id: 'arr_01a1', name: 'East Field Array', nepool_gis_id: 'NE-VT-2201', mwh_per_qtr: 32.4 },
          { id: 'arr_01a2', name: 'Barn Roof Array', nepool_gis_id: 'NE-VT-2202', mwh_per_qtr: 18.7 },
        ],
      },
      {
        id: 'acc_01b', utility: 'GMP', account_number: '4512-8902', owner_name: 'Green Valley Farm',
        arrays: [
          { id: 'arr_01b1', name: 'West Pasture Array', nepool_gis_id: 'NE-VT-2203', mwh_per_qtr: 28.1 },
        ],
      },
      {
        id: 'acc_01c', utility: 'VEC', account_number: '7823-1004', owner_name: 'Green Valley Farm',
        arrays: [
          { id: 'arr_01c1', name: 'Hillside Array', nepool_gis_id: 'NE-VT-2204', mwh_per_qtr: 41.2 },
          { id: 'arr_01c2', name: 'Creek Side Array', nepool_gis_id: 'NE-VT-2205', mwh_per_qtr: 22.9 },
        ],
      },
    ],
  },
  {
    id: 'cli_02',
    name: 'Chester Solar LLC',
    accounts: [
      {
        id: 'acc_02a', utility: 'GMP', account_number: '6234-0012', owner_name: 'Chester Solar LLC',
        arrays: [
          { id: 'arr_02a1', name: 'Chester Array 1', nepool_gis_id: 'NE-VT-3301', mwh_per_qtr: 55.0 },
          { id: 'arr_02a2', name: 'Chester Array 2', nepool_gis_id: 'NE-VT-3302', mwh_per_qtr: 55.0 },
          { id: 'arr_02a3', name: 'Chester Array 3', nepool_gis_id: 'NE-VT-3303', mwh_per_qtr: 53.8 },
        ],
      },
      {
        id: 'acc_02b', utility: 'WEC', account_number: '0812-7734', owner_name: 'Chester Solar LLC',
        arrays: [
          { id: 'arr_02b1', name: 'Ridge Array', nepool_gis_id: 'NE-VT-3304', mwh_per_qtr: 38.6 },
        ],
      },
    ],
  },
  {
    id: 'cli_03',
    name: 'Tannery Brook Holdings',
    accounts: [
      {
        id: 'acc_03a', utility: 'GMP', account_number: '3341-9087', owner_name: 'Tannery Brook Holdings',
        arrays: [
          { id: 'arr_03a1', name: 'Brook Side A', nepool_gis_id: 'NE-VT-4401', mwh_per_qtr: 19.3 },
          { id: 'arr_03a2', name: 'Brook Side B', nepool_gis_id: 'NE-VT-4402', mwh_per_qtr: 19.3 },
        ],
      },
      {
        id: 'acc_03b', utility: 'GMP', account_number: '3341-9088', owner_name: 'Tannery Brook Holdings',
        arrays: [
          { id: 'arr_03b1', name: 'Tannery Main', nepool_gis_id: 'NE-VT-4403', mwh_per_qtr: 47.5 },
        ],
      },
      {
        id: 'acc_03c', utility: 'GMP', account_number: '3341-9089', owner_name: 'Tannery Brook Holdings',
        arrays: [
          { id: 'arr_03c1', name: 'Upper Field Array', nepool_gis_id: 'NE-VT-4404', mwh_per_qtr: 29.8 },
          { id: 'arr_03c2', name: 'Lower Field Array', nepool_gis_id: 'NE-VT-4405', mwh_per_qtr: 31.2 },
          { id: 'arr_03c3', name: 'Valley Array', nepool_gis_id: 'NE-VT-4406', mwh_per_qtr: 24.6 },
        ],
      },
    ],
  },
  {
    id: 'cli_04',
    name: 'Norwich Town Hall',
    accounts: [
      {
        id: 'acc_04a', utility: 'VEC', account_number: '8834-2201', owner_name: 'Norwich Town Hall',
        arrays: [
          { id: 'arr_04a1', name: 'Town Hall Roof', nepool_gis_id: 'NE-VT-5501', mwh_per_qtr: 14.8 },
        ],
      },
      {
        id: 'acc_04b', utility: 'GMP', account_number: '5512-3390', owner_name: 'Norwich Town Hall',
        arrays: [
          { id: 'arr_04b1', name: 'Municipal Lot Array', nepool_gis_id: 'NE-VT-5502', mwh_per_qtr: 38.9 },
          { id: 'arr_04b2', name: 'Recreation Center', nepool_gis_id: 'NE-VT-5503', mwh_per_qtr: 22.1 },
        ],
      },
    ],
  },
  {
    id: 'cli_05',
    name: 'Waterford Properties',
    accounts: [
      {
        id: 'acc_05a', utility: 'GMP', account_number: '7723-4450', owner_name: 'Waterford Properties',
        arrays: [
          { id: 'arr_05a1', name: 'River View Array', nepool_gis_id: 'NE-VT-6601', mwh_per_qtr: 62.3 },
          { id: 'arr_05a2', name: 'Bridge Field Array', nepool_gis_id: 'NE-VT-6602', mwh_per_qtr: 58.7 },
        ],
      },
      {
        id: 'acc_05b', utility: 'GMP', account_number: '7723-4451', owner_name: 'Waterford Properties',
        arrays: [
          { id: 'arr_05b1', name: 'North Industrial', nepool_gis_id: 'NE-VT-6603', mwh_per_qtr: 44.0 },
        ],
      },
    ],
  },
  {
    id: 'cli_06',
    name: 'Pittsfield Co-op',
    accounts: [
      {
        id: 'acc_06a', utility: 'VEC', account_number: '9901-7712', owner_name: 'Pittsfield Co-op',
        arrays: [
          { id: 'arr_06a1', name: 'Co-op Main Array', nepool_gis_id: 'NE-VT-7701', mwh_per_qtr: 33.6 },
          { id: 'arr_06a2', name: 'Warehouse Roof', nepool_gis_id: 'NE-VT-7702', mwh_per_qtr: 21.4 },
        ],
      },
    ],
  },
  {
    id: 'cli_07',
    name: 'Londonderry Schools',
    accounts: [
      {
        id: 'acc_07a', utility: 'GMP', account_number: '2234-6678', owner_name: 'Londonderry Schools',
        arrays: [
          { id: 'arr_07a1', name: 'Elementary School', nepool_gis_id: 'NE-VT-8801', mwh_per_qtr: 28.5 },
          { id: 'arr_07a2', name: 'Middle School Array', nepool_gis_id: 'NE-VT-8802', mwh_per_qtr: 31.9 },
        ],
      },
      {
        id: 'acc_07b', utility: 'WEC', account_number: '1122-9934', owner_name: 'Londonderry Schools',
        arrays: [
          { id: 'arr_07b1', name: 'High School Array', nepool_gis_id: 'NE-VT-8803', mwh_per_qtr: 52.3 },
          { id: 'arr_07b2', name: 'Athletic Field Array', nepool_gis_id: 'NE-VT-8804', mwh_per_qtr: 38.1 },
        ],
      },
    ],
  },
  {
    id: 'cli_08',
    name: 'Timberworks Inc.',
    accounts: [
      {
        id: 'acc_08a', utility: 'GMP', account_number: '4490-2231', owner_name: 'Timberworks Inc.',
        arrays: [
          { id: 'arr_08a1', name: 'Mill Yard Array', nepool_gis_id: 'NE-VT-9901', mwh_per_qtr: 76.4 },
          { id: 'arr_08a2', name: 'Lumber Yard Array', nepool_gis_id: 'NE-VT-9902', mwh_per_qtr: 71.8 },
          { id: 'arr_08a3', name: 'Office Roof Array', nepool_gis_id: 'NE-VT-9903', mwh_per_qtr: 15.2 },
        ],
      },
      {
        id: 'acc_08b', utility: 'GMP', account_number: '4490-2232', owner_name: 'Timberworks Inc.',
        arrays: [
          { id: 'arr_08b1', name: 'Storage Yard Array', nepool_gis_id: 'NE-VT-9904', mwh_per_qtr: 43.7 },
          { id: 'arr_08b2', name: 'Workshop Array', nepool_gis_id: 'NE-VT-9905', mwh_per_qtr: 38.2 },
        ],
      },
    ],
  },
];

// "Chester Solar" fuzzy-matches "Chester Solar LLC" (token overlap ≈ 0.67 > 0.5 threshold).
// It will be auto-clustered; Riverside Holdings and VTEC Dairy Farm stay floating.
export const SEED_UNCLASSIFIED: UtilityAccount[] = [
  {
    id: 'unc_01', utility: 'GMP', account_number: '6234-0099', owner_name: 'Chester Solar',
    arrays: [
      { id: 'unc_01a1', name: 'Chester Array 4', nepool_gis_id: 'NE-VT-3305', mwh_per_qtr: 51.2 },
      { id: 'unc_01a2', name: 'Chester Array 5', nepool_gis_id: 'NE-VT-3306', mwh_per_qtr: 49.8 },
    ],
  },
  {
    id: 'unc_02', utility: 'VEC', account_number: '7823-5566', owner_name: 'Riverside Holdings',
    arrays: [
      { id: 'unc_02a1', name: 'River East Array', nepool_gis_id: 'NE-VT-1101', mwh_per_qtr: 44.5 },
    ],
  },
  {
    id: 'unc_03', utility: 'WEC', account_number: '0812-3311', owner_name: 'VTEC Dairy Farm',
    arrays: [
      { id: 'unc_03a1', name: 'Dairy Barn Array', nepool_gis_id: 'NE-VT-1102', mwh_per_qtr: 27.8 },
      { id: 'unc_03a2', name: 'Pasture Array', nepool_gis_id: 'NE-VT-1103', mwh_per_qtr: 19.3 },
    ],
  },
];
