// Smoke tests for the revamped ClientCard expanded panel.
//
// Covers:
//   1. Renders the "Import data" dropdown button (unified trigger).
//   2. Expanding the card shows the arrays section.

import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import React from "react";
import { ClientCard } from "../components/ClientCard";
import type { ClientRow } from "../lib/api";

// Stub heavy dependencies that require network or complex context.
vi.mock("../lib/api", () => ({
  updateClient: vi.fn().mockResolvedValue({}),
  deleteClient: vi.fn().mockResolvedValue({ undo_token: "tok" }),
  sendClientReportToMe: vi.fn().mockResolvedValue({}),
  downloadClientReport: vi.fn().mockResolvedValue({}),
  listArrays: vi.fn().mockResolvedValue([]),
  getQuarterlyProgress: vi.fn().mockResolvedValue({
    quarter: "Q2-2026",
    quarter_start: "2026-04-01",
    quarter_end: "2026-06-30",
    ready_arrays: [],
    missing_arrays: [],
    total_arrays: 0,
    all_ready: false,
  }),
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

function makeClient(overrides: Partial<ClientRow> = {}): ClientRow {
  return {
    id: 1,
    name: "Test Client",
    contact_email: "test@example.com",
    cc_emails: null,
    notes: null,
    active: true,
    is_placeholder: false,
    report_frequency: "quarterly",
    array_count: 3,
    last_delivery_at: null,
    last_delivered_at: null,
    last_bounced_at: null,
    last_bounce_reason: null,
    ...overrides,
  } as ClientRow;
}

describe("ClientCard", () => {
  it("shows the Import data dropdown button when expanded", () => {
    const { getByRole, getByText } = render(
      <ClientCard
        client={makeClient()}
        operatorEmail="operator@example.com"
        defaultExpanded={true}
        onChange={vi.fn()}
      />,
    );
    // The unified import button should be visible in the expanded panel.
    expect(getByText("Import data")).toBeTruthy();
  });

  it("clicking expand reveals the arrays section", () => {
    const { getByTestId, container } = render(
      <ClientCard
        client={makeClient()}
        operatorEmail="operator@example.com"
        defaultExpanded={false}
        onChange={vi.fn()}
      />,
    );
    // Arrays not visible yet.
    expect(container.querySelector('[data-tour-step="7"]')).toBeNull();

    // Click the header row to expand.
    const header = container.querySelector('[data-tour-step="2"]') as HTMLElement;
    fireEvent.click(header);

    // Arrays section should now be visible.
    expect(getByTestId("array-list")).toBeTruthy();
    expect(container.querySelector('[data-tour-step="7"]')).not.toBeNull();
  });

  it("clicking Import data opens the dropdown menu", () => {
    const { getByText } = render(
      <ClientCard
        client={makeClient()}
        operatorEmail="operator@example.com"
        defaultExpanded={true}
        onChange={vi.fn()}
      />,
    );
    const importBtn = getByText("Import data").closest("button") as HTMLElement;
    fireEvent.click(importBtn);

    // Both sub-options should appear.
    expect(getByText("Import arrays")).toBeTruthy();
    expect(getByText("Import NEPOOL IDs")).toBeTruthy();
  });
});
