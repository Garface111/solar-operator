// Focused tests for the redesigned "Add a client" modal.
//
// The point of the redesign is two sections instead of three stacked credential
// forms, so these cover the two things that carry it:
//   1. Section 1 merges the discovery pool + unassigned portal logins into ONE
//      list (a login in both sources appears once) with a plain status line.
//   2. Section 2 is a single search over utilities AND monitoring vendors, and
//      selecting a result reveals exactly one contextual form.

import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "../ui/Toast";
import { AddClientByLoginModal } from "../components/AddClientByLoginModal";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  const seen = new Date().toISOString();
  return {
    ...actual,
    listDiscoveryCandidates: vi.fn(async () => ({
      ok: true,
      refreshed_at: seen,
      logins: [
        {
          key: "locus:partner",
          provider: "locus",
          provider_label: "Locus (SolarNOC)",
          source_kind: "vendor" as const,
          login: "partner@example.com",
          last_seen_at: seen,
          last_error: null,
          counts: { new: 2, imported: 3, ignored: 0 },
          candidates: [],
        },
        {
          // Same login the portal-access call reports as unassigned — must
          // collapse into ONE row (case-insensitive on provider+username).
          key: "gmp:ops",
          provider: "gmp",
          provider_label: "Green Mountain Power",
          source_kind: "utility" as const,
          login: "OPS@example.com",
          last_seen_at: seen,
          last_error: "Password was rejected — re-enter it.",
          counts: { new: 0, imported: 0, ignored: 0 },
          candidates: [],
        },
      ],
    })),
    getPortalAccess: vi.fn(async () => ({
      extension_alive: true,
      extension_last_seen: seen,
      clients: [],
      unassigned_logins: [
        {
          provider: "gmp",
          username: "ops@example.com",
          status: "automated",
          last_ok_at: seen,
          enabled: true,
          fails: 0,
        },
      ],
    })),
    getProviders: vi.fn(async () => [
      {
        code: "vec",
        label: "Vermont Electric Coop",
        state: "VT",
        scrape_status: "live" as const,
        smarthub_host: "vec.smarthub.coop",
        portal_url: "https://vec.smarthub.coop/",
        notes: "",
      },
    ]),
    getInverterVendors: vi.fn(async () => [
      {
        code: "solaredge",
        label: "SolarEdge",
        available: true,
        note: null,
        connect_mode: "key" as const,
        fields: [{ name: "api_key", label: "API key", secret: true }],
      },
      {
        code: "locus",
        label: "Locus Energy (SolarNOC)",
        available: true,
        note: null,
        connect_mode: "key" as const,
        fields: [{ name: "username", label: "SolarNOC username" }],
      },
      {
        code: "chint",
        label: "Chint / CPS",
        available: false,
        note: "Chint/CPS has no public API and no key to paste.",
        connect_mode: "key" as const,
        fields: [],
      },
    ]),
  };
});

vi.mock("../lib/useExtensionStatus", () => ({
  useExtensionStatus: () => ({
    status: "present-paired",
    version: "1.9.120",
    probe: vi.fn(async () => {}),
  }),
}));

function renderModal() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <AddClientByLoginModal
          open
          onClose={vi.fn()}
          onCaptured={vi.fn(async () => [])}
          onSwitchToManual={vi.fn()}
        />
      </ToastProvider>
    </MemoryRouter>,
  );
}

describe("AddClientByLoginModal", () => {
  beforeEach(() => vi.clearAllMocks());

  it("merges both login sources into one list with plain status lines", async () => {
    renderModal();

    expect((await screen.findAllByText("Locus (SolarNOC)")).length).toBeGreaterThan(0);
    expect(screen.getByText(/3 arrays in your system · 2 new to review/)).toBeTruthy();

    // The GMP login is in BOTH sources — one row, keeping the pool's error
    // line AND the unassigned action. (The catalog search below also lists
    // "Green Mountain Power", so match on the login itself.)
    expect(screen.getAllByText("OPS@example.com")).toHaveLength(1);
    expect(screen.queryByText("ops@example.com")).toBeNull();
    expect(screen.getByText(/Needs attention — Password was rejected/)).toBeTruthy();
    expect(screen.getByText("Use this login →")).toBeTruthy();
  });

  it("searches utilities and monitoring vendors in one box", async () => {
    renderModal();
    const search = await screen.findByLabelText(
      "Search for your utility or monitoring portal",
    );

    // Default list before typing: GMP + the account-level monitors.
    await waitFor(() => expect(screen.getByText("SolarEdge")).toBeTruthy());

    fireEvent.change(search, { target: { value: "vermont" } });
    await waitFor(() => expect(screen.getByText("Vermont Electric Coop")).toBeTruthy());
    expect(screen.queryByText("SolarEdge")).toBeNull();

    fireEvent.change(search, { target: { value: "solaredge" } });
    await waitFor(() => expect(screen.getByText("SolarEdge")).toBeTruthy());
    expect(screen.queryByText("Vermont Electric Coop")).toBeNull();
  });

  it("reveals one contextual form for the selected entry", async () => {
    renderModal();
    const search = await screen.findByLabelText(
      "Search for your utility or monitoring portal",
    );

    fireEvent.change(search, { target: { value: "solaredge" } });
    fireEvent.click(await screen.findByText("SolarEdge"));

    expect(await screen.findByText("Connect SolarEdge")).toBeTruthy();
    expect(screen.getByPlaceholderText("SolarEdge API key")).toBeTruthy();
    // The search list is replaced, not stacked underneath.
    expect(screen.queryByLabelText("Search for your utility or monitoring portal")).toBeNull();

    fireEvent.click(screen.getByText("← back to search"));
    expect(
      await screen.findByLabelText("Search for your utility or monitoring portal"),
    ).toBeTruthy();
  });

  it("shows the note and no form for an unavailable vendor", async () => {
    renderModal();
    const search = await screen.findByLabelText(
      "Search for your utility or monitoring portal",
    );

    fireEvent.change(search, { target: { value: "chint" } });
    fireEvent.click(await screen.findByText("Chint / CPS"));

    expect(
      await screen.findByText(/no public API and no key to paste/),
    ).toBeTruthy();
    expect(screen.queryByText(/Find my sites/)).toBeNull();
  });
});
