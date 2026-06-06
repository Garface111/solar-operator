// Test for the Capture Freshness Heatmap — one dot per utility account,
// colored by how recently each account was captured.

import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { ArrayRow } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    listArrays: vi.fn(),
  };
});

import { CaptureFreshnessHeatmap } from "../components/CaptureFreshnessHeatmap";
import * as apiModule from "../lib/api";

const DAY_MS = 86_400_000;
const iso = (daysAgo: number) => new Date(Date.now() - daysAgo * DAY_MS).toISOString();

function makeArray(id: number, accounts: { id: number; last_synced_at: string | null }[]): ArrayRow {
  return {
    id,
    name: `Array ${id}`,
    nepool_gis_id: null,
    region: null,
    bill_offset_months: null,
    notes: null,
    excluded: false,
    solaredge_connected: false,
    solaredge_site_id: null,
    accounts: accounts.map((a) => ({
      id: a.id,
      provider: "gmp",
      provider_label: "Green Mountain Power (GMP)",
      account_number: `100000000${a.id}`,
      nickname: null,
      last_synced_at: a.last_synced_at,
    })),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CaptureFreshnessHeatmap", () => {
  it("renders one dot per account with correct freshness color", async () => {
    // One account in each freshness bucket, plus a never-synced (unknown) one.
    vi.mocked(apiModule.listArrays).mockResolvedValue([
      makeArray(1, [
        { id: 11, last_synced_at: iso(2) }, // fresh  ≤7d
        { id: 12, last_synced_at: iso(20) }, // recent 8–30d
      ]),
      makeArray(2, [
        { id: 21, last_synced_at: iso(60) }, // stale  31–90d
        { id: 22, last_synced_at: iso(200) }, // cold   >90d
        { id: 23, last_synced_at: null }, // unknown
      ]),
    ]);

    render(<CaptureFreshnessHeatmap clientId={1} accountCount={5} />);

    await waitFor(() =>
      expect(document.querySelectorAll("[data-account-id]").length).toBe(5),
    );

    const dot = (id: number) =>
      document.querySelector(`[data-account-id="${id}"]`) as HTMLElement;

    expect(dot(11).className).toContain("bg-emerald-500"); // fresh
    expect(dot(12).className).toContain("bg-emerald-200"); // recent
    expect(dot(21).className).toContain("bg-wood-300"); // stale
    expect(dot(22).className).toContain("bg-red-300"); // cold
    expect(dot(23).className).toContain("bg-zinc-200"); // unknown

    // Worst status present is cold → chip surfaces it.
    expect(screen.getByText("1 cold")).toBeTruthy();
  });

  it("shows the rewarding 'All fresh' chip when every account is fresh", async () => {
    vi.mocked(apiModule.listArrays).mockResolvedValue([
      makeArray(1, [
        { id: 11, last_synced_at: iso(0) },
        { id: 12, last_synced_at: iso(1) },
      ]),
    ]);

    render(<CaptureFreshnessHeatmap clientId={1} accountCount={2} />);

    await waitFor(() => expect(screen.getByText("All fresh")).toBeTruthy());
  });
});
