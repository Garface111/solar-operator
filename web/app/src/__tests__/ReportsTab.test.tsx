// Smoke tests for the ReportsTab history timeline redesign.
//
// Covers:
//   1. Timeline rail renders when reports exist.
//   2. Each QuarterCard shows a stat line.
//   3. Bounce strip appears only when a client has an unresolved bounce.

import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "../ui/Toast";
import type { ClientRow, QuarterReport } from "../lib/api";

// ── Mocks ─────────────────────────────────────────────────────────────────────

// All vi.mock calls are hoisted to module init — order here doesn't matter.

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    listClients: vi.fn().mockResolvedValue([]),
    getReports: vi.fn().mockResolvedValue([]),
  };
});

vi.mock("../screens/DashboardLayout", () => ({
  useDashboardContext: vi.fn(),
}));

vi.mock("../components/reports/AutoReportsSettingsCard", () => ({
  AutoReportsSettingsCard: () => null,
}));

vi.mock("../components/reports/NextRunCard", () => ({
  NextRunCard: () => null,
}));

vi.mock("../components/reports/EmailTemplateStudio", () => ({
  EmailTemplateStudio: () => null,
}));

// FailureStrip uses react-router-dom Link; avoid needing a Router in tests.
vi.mock("../components/reports/FailureStrip", () => ({
  FailureStrip: () => null,
}));

vi.mock("../components/reports/StatusPill", () => ({
  StatusPill: () => null,
}));

// ── Providers ─────────────────────────────────────────────────────────────────

function Wrapper({ children }: { children: React.ReactNode }) {
  // QuarterCard's per-client "View trends" link uses react-router, so the
  // smoke test needs a Router in context (the real app always has one).
  return (
    <MemoryRouter>
      <ToastProvider>{children}</ToastProvider>
    </MemoryRouter>
  );
}

// ── Fixtures ──────────────────────────────────────────────────────────────────

function makeAccount() {
  return {
    tenant_id: "ten_test001",
    tenant_key: "sol_live_test",
    name: "Test Operator",
    email: "test@example.com",
    plan: "standard",
    active: true,
    is_demo: false,
    subscription_status: "active",
    report_frequency: "quarterly",
    cc_on_reports: false,
    has_password: true,
    send_from_email: null,
    send_from_name: null,
    email_subject_template: null,
    email_body_template: null,
    send_mode: "to_client",
    default_email_subject: "",
    default_email_body: "",
    merge_tags: [],
    last_pull_at: null,
    last_delivery_at: null,
    extension_heartbeat_at: null,
    created_at: null,
    trial_ends_at: null,
    accounts_count: 3,
    bills_count: 0,
    clients_count: 2,
    onboarding_array_estimate: 3,
    all_set: true,
    session: null,
  };
}

function makeClient(overrides: Partial<ClientRow> = {}): ClientRow {
  return {
    id: 1,
    name: "Acme Solar",
    contact_email: "acme@example.com",
    cc_emails: null,
    report_frequency: "quarterly",
    active: true,
    array_count: 5,
    last_delivery_at: "2025-04-01T12:00:00Z",
    notes: null,
    gmp_email: null,
    gmp_username: null,
    gmp_autopopulate: false,
    gmp_last_sync_at: null,
    vec_email: null,
    vec_username: null,
    vec_autopopulate: false,
    vec_last_sync_at: null,
    last_delivered_at: "2025-04-01T12:00:00Z",
    last_bounced_at: null,
    last_bounce_reason: null,
    ...overrides,
  };
}

function makeReport(overrides: Partial<QuarterReport> = {}): QuarterReport {
  return {
    quarter: "2025Q1",
    year: 2025,
    quarter_num: 1,
    status: "sent",
    array_count: 5,
    last_generated_at: "2025-04-01T10:00:00Z",
    last_delivered_at: "2025-04-01T12:00:00Z",
    mwh_total: 142.3,
    ...overrides,
  };
}

// ── Setup ─────────────────────────────────────────────────────────────────────

// Import ReportsTab at module level after mocks are registered.
// Note: vi.mock is hoisted so all mocks are active by the time this runs.
import ReportsTab from "../screens/ReportsTab";
import * as apiModule from "../lib/api";
import * as dashboardModule from "../screens/DashboardLayout";

async function setup({
  clients = [makeClient()],
  reports = [makeReport()],
}: {
  clients?: ClientRow[];
  reports?: QuarterReport[];
} = {}) {
  vi.mocked(apiModule.listClients).mockResolvedValue(clients);
  vi.mocked(apiModule.getReports).mockResolvedValue(reports);
  vi.mocked(dashboardModule.useDashboardContext).mockReturnValue({
    account: makeAccount() as any,
    failed: false,
    patchAccount: vi.fn(),
    retryLoad: vi.fn(),
  } as any);

  render(<ReportsTab />, { wrapper: Wrapper });
  await waitFor(() =>
    expect(screen.queryByLabelText("Loading history")).toBeNull(),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("ReportsTab history timeline", () => {
  it("renders the timeline rail when reports exist", async () => {
    await setup({
      reports: [
        makeReport({ quarter: "2025Q1", quarter_num: 1 }),
        makeReport({ quarter: "2024Q4", quarter_num: 4, year: 2024 }),
      ],
    });

    expect(screen.getByTestId("reports-timeline")).toBeTruthy();
  });

  it("shows the stat line for each quarter card", async () => {
    await setup({
      clients: [makeClient({ array_count: 5 })],
      reports: [
        makeReport({
          quarter: "2025Q1",
          quarter_num: 1,
          mwh_total: 142.3,
          array_count: 5,
        }),
      ],
    });

    expect(screen.getByText(/5 arrays/)).toBeTruthy();
    expect(screen.getByText(/142\.30 MWh/)).toBeTruthy();
  });

  it("does not show bounce strip when no client has bounced", async () => {
    await setup({
      clients: [makeClient({ last_bounced_at: null })],
      reports: [makeReport()],
    });

    expect(screen.queryByText(/⚠/)).toBeNull();
  });

  it("shows bounce strip when a client has an unresolved bounce", async () => {
    await setup({
      clients: [
        makeClient({
          last_bounced_at: "2025-04-02T00:00:00Z",
          last_delivered_at: "2025-04-01T12:00:00Z",
          last_bounce_reason: "Mailbox full",
        }),
      ],
      reports: [makeReport()],
    });

    // Bounce strip renders a ⚠ warning and the reason string.
    expect(screen.getByText(/⚠/)).toBeTruthy();
    // "Mailbox full" appears in both the strip and the expanded table row.
    expect(screen.getAllByText(/Mailbox full/).length).toBeGreaterThan(0);
  });
});
