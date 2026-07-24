// Smoke tests for the Discover staging pool.
//
// Covers the curation rules that actually protect a tenant:
//   1. Candidates group under their login with counts + the last error note.
//   2. `imported` rows are badged and NOT selectable (they're already yours).
//   3. `ignored` rows hide behind the "Show ignored (N)" toggle.
//   4. Selecting a `new` row raises the action bar with a client target.

import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { ToastProvider } from "../ui/Toast";
import DiscoverTab from "../screens/DiscoverTab";

// ── Mocks ─────────────────────────────────────────────────────────────────────

// vi.mock factories are hoisted above every top-level binding, so the pool
// fixture is built inside the factory rather than referenced from module scope.
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  const seen = new Date().toISOString();
  const pool: import("../lib/api").DiscoveryPool = {
    ok: true,
    refreshed_at: seen,
    logins: [
      {
        key: "locus:johnson_hardware_and_rental",
        provider: "locus",
        provider_label: "Locus (SolarNOC)",
        source_kind: "vendor",
        login: "johnson@example.com",
        last_seen_at: seen,
        last_error:
          "Password was rejected — re-enter it to keep this login fresh.",
        counts: { new: 1, imported: 1, ignored: 1 },
        candidates: [
          {
            id: 1,
            name: "Maple Ridge Array",
            external_id: "LOC-1",
            peak_power_kw: 120,
            status: "new",
            imported_array_id: null,
            imported_client_id: null,
            imported_client_name: null,
            suggested_client: "Maple Ridge Solar",
            last_seen_at: seen,
          },
          {
            id: 2,
            name: "Birch Hollow Array",
            external_id: "LOC-2",
            peak_power_kw: null,
            status: "imported",
            imported_array_id: 44,
            imported_client_id: 7,
            imported_client_name: "Birch Hollow LLC",
            suggested_client: "Birch Hollow LLC",
            last_seen_at: seen,
          },
          {
            id: 3,
            name: "Somebody Else's Array",
            external_id: "LOC-3",
            peak_power_kw: 40,
            status: "ignored",
            imported_array_id: null,
            imported_client_id: null,
            imported_client_name: null,
            suggested_client: "",
            last_seen_at: seen,
          },
        ],
      },
    ],
  };
  return {
    ...actual,
    listDiscoveryCandidates: vi.fn().mockResolvedValue(pool),
    listClients: vi
      .fn()
      .mockResolvedValue([{ id: 7, name: "Birch Hollow LLC", active: true }]),
    refreshDiscovery: vi.fn(),
    importDiscoveryCandidates: vi.fn(),
    setDiscoveryIgnored: vi.fn(),
  };
});

vi.mock("../screens/DashboardLayout", () => ({
  useDashboardContext: () => ({
    account: null,
    failed: false,
    patchAccount: vi.fn(),
    retryLoad: vi.fn(),
  }),
}));

function renderTab() {
  return render(
    <ToastProvider>
      <DiscoverTab />
    </ToastProvider>,
  );
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("DiscoverTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("groups candidates under their login and surfaces the login's error", async () => {
    renderTab();
    expect(await screen.findByText("Locus (SolarNOC)")).toBeTruthy();
    expect(screen.getByText(/johnson@example\.com/)).toBeTruthy();
    expect(screen.getByText("1 new")).toBeTruthy();
    expect(screen.getByText(/Password was rejected/)).toBeTruthy();
  });

  it("badges imported candidates and refuses to let them be re-selected", async () => {
    renderTab();
    const box = (await screen.findByLabelText(
      "Select Birch Hollow Array",
    )) as HTMLInputElement;
    expect(box.disabled).toBe(true);
    expect(screen.getByText("In your system")).toBeTruthy();
  });

  it("hides ignored candidates until the toggle is used", async () => {
    renderTab();
    await screen.findByText("Maple Ridge Array");
    expect(screen.queryByText("Somebody Else's Array")).toBeNull();
    fireEvent.click(screen.getByText("Show ignored (1)"));
    expect(screen.getByText("Somebody Else's Array")).toBeTruthy();
  });

  it("raises the action bar with a client target once something is selected", async () => {
    renderTab();
    fireEvent.click(await screen.findByLabelText("Select Maple Ridge Array"));
    await waitFor(() => expect(screen.getByText("1 selected")).toBeTruthy());
    expect(screen.getByText("Add to my system")).toBeTruthy();
    expect(screen.getByRole("option", { name: "＋ New client…" })).toBeTruthy();
  });
});
