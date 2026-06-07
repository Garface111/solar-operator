// Tests for the inline LOGINS & ACCOUNTS card and QuarterlyProgressChip
// integration inside the expanded ClientCard.
//
// Verifies:
//   1. Logins & accounts card is rendered when expanded
//   2. All accounts from listArrays appear inline (no "N more" truncation)
//   3. max-height + scroll container present for large account sets
//   4. QuarterlyProgressChip is rendered when card is expanded
//   5. Card is NOT rendered when collapsed

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import { ClientCard } from "../components/ClientCard";
import type { ClientRow, ArrayRow, QuarterlyProgress } from "../lib/api";

// ── mocks ─────────────────────────────────────────────────────────────────────

const mockListArrays = vi.fn<[number], Promise<ArrayRow[]>>();
const mockGetQuarterlyProgress = vi.fn<[number], Promise<QuarterlyProgress>>();

vi.mock("../lib/api", () => ({
  updateClient: vi.fn().mockResolvedValue({}),
  deleteClient: vi.fn().mockResolvedValue({ undo_token: "tok" }),
  sendClientReportToMe: vi.fn().mockResolvedValue({}),
  downloadClientReport: vi.fn().mockResolvedValue({}),
  listArrays: (...args: Parameters<typeof mockListArrays>) => mockListArrays(...args),
  getQuarterlyProgress: (...args: Parameters<typeof mockGetQuarterlyProgress>) =>
    mockGetQuarterlyProgress(...args),
}));

vi.mock("../ui/Toast", () => ({
  useToast: () => ({ show: vi.fn(), error: vi.fn(), warning: vi.fn(), success: vi.fn() }),
}));

vi.mock("../components/WelcomeReveal", () => ({
  useReveal: () => ({ active: false, delayFor: () => 0 }),
}));

vi.mock("../components/ArrayList", () => ({
  ArrayList: () => <div data-testid="array-list">arrays</div>,
}));

vi.mock("../components/CaptureFreshnessHeatmap", () => ({
  CaptureFreshnessHeatmap: () => <div data-testid="freshness-heatmap">freshness</div>,
}));

vi.mock("../components/MergeSuggestionBanner", () => ({
  MergeSuggestionBanner: () => null,
}));

vi.mock("../components/AssignNepoolFromSpreadsheetModal", () => ({
  AssignNepoolFromSpreadsheetModal: () => null,
}));

vi.mock("../components/ImportSpreadsheetModal", () => ({
  ImportSpreadsheetModal: () => null,
}));

vi.mock("../ui/EditableField", () => ({
  EditableField: ({ value, emptyText }: { value: string | null; emptyText: string }) => (
    <span>{value ?? emptyText}</span>
  ),
}));

vi.mock("../ui/RevealNumber", () => ({
  RevealNumber: ({ value }: { value: number }) => <span>{value}</span>,
}));

// ── helpers ───────────────────────────────────────────────────────────────────

function makeClient(overrides: Partial<ClientRow> = {}): ClientRow {
  return {
    id: 42,
    name: "Catamount Solar",
    contact_email: "catamount@example.com",
    cc_emails: null,
    notes: null,
    active: true,
    is_placeholder: false,
    report_frequency: "quarterly",
    array_count: 2,
    last_delivery_at: null,
    last_delivered_at: null,
    last_bounced_at: null,
    last_bounce_reason: null,
    gmp_email: null,
    gmp_username: null,
    gmp_autopopulate: true,
    gmp_last_sync_at: null,
    vec_email: null,
    vec_username: null,
    vec_autopopulate: true,
    vec_last_sync_at: null,
    ...overrides,
  } as ClientRow;
}

function makeArrayRows(): ArrayRow[] {
  return [
    {
      id: 1,
      name: "Catamount North",
      nepool_gis_id: "53984",
      region: null,
      bill_offset_months: 1,
      notes: null,
      excluded: false,
      solaredge_connected: false,
      solaredge_site_id: null,
      accounts: [
        {
          id: 101,
          provider: "gmp",
          provider_label: "GMP",
          account_number: "0001234567",
          nickname: "North Meter",
        },
        {
          id: 102,
          provider: "gmp",
          provider_label: "GMP",
          account_number: "0001234568",
          nickname: null,
        },
      ],
    },
    {
      id: 2,
      name: "Catamount South",
      nepool_gis_id: null,
      region: null,
      bill_offset_months: 1,
      notes: null,
      excluded: false,
      solaredge_connected: false,
      solaredge_site_id: null,
      accounts: [
        {
          id: 103,
          provider: "vec",
          provider_label: "VEC",
          account_number: "9997001",
          nickname: "South VEC",
        },
      ],
    },
  ];
}

function makeProgress(overrides: Partial<QuarterlyProgress> = {}): QuarterlyProgress {
  return {
    quarter: "Q2-2026",
    quarter_start: "2026-04-01",
    quarter_end: "2026-06-30",
    ready_arrays: [],
    missing_arrays: [{ id: 1, name: "Catamount North", missing_months: ["2026-06"] }],
    total_arrays: 1,
    all_ready: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockListArrays.mockResolvedValue(makeArrayRows());
  mockGetQuarterlyProgress.mockResolvedValue(makeProgress());
});

// ── tests ─────────────────────────────────────────────────────────────────────

describe("ClientCard inline accounts (expanded)", () => {
  it("renders the Logins & accounts card when expanded", async () => {
    render(
      <ClientCard
        client={makeClient()}
        operatorEmail="op@example.com"
        defaultExpanded={true}
        onChange={vi.fn()}
      />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("logins-accounts-card")).toBeTruthy();
    });
  });

  it("renders ALL accounts from listArrays inline (no truncation)", async () => {
    render(
      <ClientCard
        client={makeClient()}
        operatorEmail="op@example.com"
        defaultExpanded={true}
        onChange={vi.fn()}
      />,
    );
    await waitFor(() => {
      // Three accounts: North Meter, GMP ••4568, South VEC
      expect(screen.getByText("North Meter")).toBeTruthy();
      expect(screen.getByText("South VEC")).toBeTruthy();
    });
  });

  it("scroll container has max-height style (overflow control)", async () => {
    render(
      <ClientCard
        client={makeClient()}
        operatorEmail="op@example.com"
        defaultExpanded={true}
        onChange={vi.fn()}
      />,
    );
    await waitFor(() => {
      const scrollList = screen.getByTestId("accounts-scroll-list");
      const style = scrollList.style.maxHeight;
      expect(style).toBe("320px");
    });
  });

  it("renders the QuarterlyProgressChip when expanded", async () => {
    render(
      <ClientCard
        client={makeClient()}
        operatorEmail="op@example.com"
        defaultExpanded={true}
        onChange={vi.fn()}
      />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("quarterly-progress-chip")).toBeTruthy();
    });
  });

  it("Logins & accounts card is NOT visible when card is collapsed", () => {
    render(
      <ClientCard
        client={makeClient()}
        operatorEmail="op@example.com"
        defaultExpanded={false}
        onChange={vi.fn()}
      />,
    );
    // Should not be rendered at all in collapsed state
    expect(screen.queryByTestId("logins-accounts-card")).toBeNull();
  });
});
