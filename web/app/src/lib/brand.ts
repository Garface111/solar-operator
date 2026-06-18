// Product-aware shell branding. One source of truth so the dashboard chrome
// (wordmark, tab labels, footer, marketing link) matches the tenant's product
// instead of hardcoding "NEPOOL Operator" everywhere. Keying off Account.product
// (returned by GET /v1/account) means an Array Operator owner like Paul never
// sees NEPOOL chrome, while real NEPOOL tenants are unchanged.

export type ProductKey = "nepool" | "array_operator";

export interface Brand {
  /** Wordmark first word, rendered in the primary accent color. */
  wordmarkAccent: string;
  /** Wordmark second word, rendered in zinc-900. */
  wordmarkRest: string;
  /** Full brand name for the footer / aria labels. */
  fullName: string;
  /** Marketing landing page the wordmark links to. */
  marketingUrl: string;
  /** Label for the account tab ("Master account" vs "Account"). */
  accountTabLabel: string;
  /** Label for the reports tab ("Automatic Reports" vs "Reports"). */
  reportsTabLabel: string;
  /** Label for the clients tab ("Clients" vs "Customers"). */
  clientsTabLabel: string;
}

const BRANDS: Record<ProductKey, Brand> = {
  nepool: {
    wordmarkAccent: "NEPOOL",
    wordmarkRest: "Operator",
    fullName: "NEPOOL Operator",
    marketingUrl: "https://nepooloperator.com",
    accountTabLabel: "Master account",
    reportsTabLabel: "Automatic Reports",
    clientsTabLabel: "Clients",
  },
  array_operator: {
    wordmarkAccent: "Array",
    wordmarkRest: "Operator",
    fullName: "Array Operator",
    marketingUrl: "https://arrayoperator.com",
    // Array Operator owners think in "account / customers / billing", not the
    // NEPOOL-filing vocabulary.
    accountTabLabel: "Account",
    reportsTabLabel: "Billing",
    clientsTabLabel: "Customers",
  },
};

export function brandFor(product: string | null | undefined): Brand {
  return product === "array_operator" ? BRANDS.array_operator : BRANDS.nepool;
}
