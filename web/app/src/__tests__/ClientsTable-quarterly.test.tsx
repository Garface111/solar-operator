// Tests for QuarterlyProgressChip + inline accounts inside ClientsTable's
// expanded row. The table starts every row expanded by default, so the panel
// is immediately visible without a click.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { ClientsTable } from "../components/ClientsTable";
import type { ClientRow, ArrayRow, QuarterlyProgress } from "../lib/api";

// ── mocks ─────────────────────────────────────────────────────────────────────

const mockListArrays = vi.fn<[number], Promise<ArrayRow[]>>();
const mockGetQuarterlyProgress = vi.fn<[number], Promise<QuarterlyProgress>>();

vi.mock("../lib/api", () => ({
  updateClient: vi.fn().mockResolvedValue({}),
  deleteClient: vi.fn().mockResolvedValue({ undo_token: "tok" }),
  sendClientReportToMe: vi.fn().mockResolvedValue({}),
  downloadClientReport: vi.fn().mockResolvedValue({}),
  mergeClientInto: vi.fn().mockResolvedValue({}),
  refreshCapture: vi.fn().mockResolvedValue({}),
  listArrays: (...args: Parameters<typeof mockListArrays>) => mockListArrays(...args),
  getQuarterlyProgress: (...args: Parameters<typeof mockGetQuarterlyProgress>) =>
    mockGetQuarterlyProgress(...args),
}));

vi.mock("../ui/Toast", () => ({
  useToast: () => ({ show: vi.fn(), error: vi.fn(), warning: vi.fn(), success: vi.fn() }),
}));

vi.mock("../components/ArrayList", () => ({
  ArrayList: () => <div data-testid="array-list">arrays</div>,
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

// ── helpers ───────────────────────────────────────────────────────────────────

function makeClient(overrides: Partial<ClientRow> = {}): ClientRow {
  return {
    id: 7,
    name: "Starlake Solar",
    contact_email: "starlake@example.com",
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
    gmp_email: "owner@starlake.com",
    gmp_username: null,
    gmp_autopopulate: true,
    gmp_last_sync_at: null,
    vec_email: null,
    vec_username: null,
    vec_autopopulate: false,
    vec_last_sync_at: null,
    ...overrides,
  } as ClientRow;
}

function makeArrayRows(): ArrayRow[] {
  return [
    {
      id: 10,
      name: "Starlake North",
      nepool_gis_id: "SN-001",
      region: null,
      bill_offset_months: 1,
      notes: null,
      excluded: false,
      solaredge_connected: false,
      solaredge_site_id: null,
      accounts: [
        {
          id: 201,
          provider: "gmp",
          provider_label: "GMP",
          account_number: "7770001",
          nickname: "North meter",
        },
      ],
    },
    {
      id: 11,
      name: "Starlake South",
      nepool_gis_id: null,
      region: null,
      bill_offset_months: 1,
      notes: null,
      excluded: false,
      solaredge_connected: false,
      solaredge_site_id: null,
      accounts: [
        {
          id: 202,
          provider: "gmp",
          provider_label: "GMP",
          account_number: "7770002",
          nickname: null,
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
    ready_arrays: [{ id: 11, name: "Starlake South" }],
    missing_arrays: [
      { id: 10, name: "Starlake North", missing_months: ["2026-06"] },
    ],
    total_arrays: 2,
    all_ready: false,
    ...overrides,
  };
}

function renderTable(client: ClientRow = makeClient()) {
  return render(
    <ClientsTable
      clients={[client]}
      operatorEmail="operator@example.com"
      selectMode={false}
      selectedIds={new Set()}
      onToggleSelect={vi.fn()}
      onChange={vi.fn()}
      onDeleted={vi.fn()}
      onUndo={vi.fn()}
      onOpenAddByLogin={vi.fn()}
      allClients={[]}
      onMerged={vi.fn()}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockListArrays.mockResolvedValue(makeArrayRows());
  mockGetQuarterlyProgress.mockResolvedValue(makeProgress());
});

// ── tests ─────────────────────────────────────────────────────────────────────

describe("ClientsTable quarterly progress (expanded row)", () => {
  it("renders QuarterlyProgressChip in the expanded panel", async () => {
    renderTable();
    await waitFor(() => {
      expect(screen.getByTestId("quarterly-progress-chip")).toBeTruthy();
    });
  });

  it("shows in-progress state with count and missing array name", async () => {
    renderTable();
    await waitFor(() => {
      expect(screen.getByText(/1 of 2/)).toBeTruthy();
      // "Starlake North" appears in both the chip and the accounts list — that's correct
      expect(screen.getAllByText(/Starlake North/).length).toBeGreaterThan(0);
    });
  });

  it("shows all-ready celebration when every array has data", async () => {
    mockGetQuarterlyProgress.mockResolvedValue(
      makeProgress({
        all_ready: true,
        ready_arrays: [
          { id: 10, name: "Starlake North" },
          { id: 11, name: "Starlake South" },
        ],
        missing_arrays: [],
      }),
    );
    renderTable();
    await waitFor(() => {
      expect(screen.getByText(/Reports ready to ship/i)).toBeTruthy();
    });
  });

  it("renders inline accounts list from listArrays in expanded panel", async () => {
    renderTable();
    await waitFor(() => {
      // account_number rendered inside LoginAccountList
      expect(screen.getByText(/7770001/)).toBeTruthy();
    });
  });

  it("demo tenant with no contact email still shows chip", async () => {
    renderTable(makeClient({ contact_email: null }));
    await waitFor(() => {
      expect(screen.getByTestId("quarterly-progress-chip")).toBeTruthy();
    });
  });

  it("chip lists missing months from the API response", async () => {
    mockGetQuarterlyProgress.mockResolvedValue(
      makeProgress({
        missing_arrays: [
          { id: 10, name: "Starlake North", missing_months: ["2026-04", "2026-05"] },
        ],
      }),
    );
    renderTable();
    await waitFor(() => {
      // shortMonth("2026-04") === "Apr"
      expect(screen.getByText(/Apr/)).toBeTruthy();
    });
  });
});
