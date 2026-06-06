// Tests for the compact array list (feat/array-list-compact).
//
// Covers the at-a-glance NEPOOL scan that the redesign is built around:
//   1. An empty NEPOOL row renders the orange "needs an ID" dot.
//   2. A filled NEPOOL row renders the value + emerald check (no dot).
//   3. "Show only missing NEPOOL" hides the filled rows.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import type { ArrayRow } from "../lib/api";

const listArrays = vi.fn();

vi.mock("../lib/api", () => ({
  listArrays: (...a: unknown[]) => listArrays(...a),
  createArray: vi.fn(),
  updateArray: vi.fn(),
  deleteArray: vi.fn(),
  bulkDeleteArrays: vi.fn(),
  addUtilityAccount: vi.fn(),
  removeUtilityAccount: vi.fn(),
  listProviders: vi.fn().mockResolvedValue([]),
  uploadDailyCsv: vi.fn(),
  setupSolarEdge: vi.fn(),
  previewSolarEdge: vi.fn(),
  disconnectSolarEdge: vi.fn(),
}));

vi.mock("../ui/Toast", () => ({
  useToast: () => ({ show: vi.fn(), error: vi.fn(), warning: vi.fn(), success: vi.fn() }),
}));

vi.mock("../components/ArrayMergeSuggestionBanner", () => ({
  ArrayMergeSuggestionBanner: () => null,
}));

import { ArrayList } from "../components/ArrayList";

function makeArray(overrides: Partial<ArrayRow> = {}): ArrayRow {
  return {
    id: 1,
    name: "Test Array",
    nepool_gis_id: null,
    region: null,
    bill_offset_months: null,
    notes: null,
    excluded: false,
    accounts: [],
    solaredge_connected: false,
    solaredge_site_id: null,
    ...overrides,
  };
}

describe("ArrayList (compact)", () => {
  beforeEach(() => {
    listArrays.mockReset();
  });

  it("renders the orange dot for an array missing its NEPOOL ID", async () => {
    listArrays.mockResolvedValue([makeArray({ id: 1, name: "Tannery Brook", nepool_gis_id: null })]);
    const { container, findByText } = render(<ArrayList clientId={1} />);

    await findByText("Tannery Brook");
    expect(container.querySelector("[data-nepool-dot]")).not.toBeNull();
    // Ghost "Add ID" affordance, no emerald check.
    expect(await findByText("Add ID")).toBeTruthy();
    expect(container.textContent).not.toContain("✓");
  });

  it("renders the value + emerald check for a filled NEPOOL ID", async () => {
    listArrays.mockResolvedValue([makeArray({ id: 2, name: "Chester Solar", nepool_gis_id: "53984" })]);
    const { container, findByText } = render(<ArrayList clientId={1} />);

    await findByText("Chester Solar");
    expect(await findByText("53984")).toBeTruthy();
    expect(container.textContent).toContain("✓");
    // No "needs attention" dot on a complete row.
    expect(container.querySelector("[data-nepool-dot]")).toBeNull();
  });

  it("'Show only missing NEPOOL' hides the filled rows", async () => {
    listArrays.mockResolvedValue([
      makeArray({ id: 1, name: "Filled Array", nepool_gis_id: "11111" }),
      makeArray({ id: 2, name: "Empty Array", nepool_gis_id: null }),
    ]);
    const { findByText, queryByText, getByLabelText } = render(<ArrayList clientId={1} />);

    // Both visible by default.
    await findByText("Filled Array");
    await findByText("Empty Array");

    const toggle = getByLabelText(/Show only missing NEPOOL/i);
    fireEvent.click(toggle);

    await waitFor(() => expect(queryByText("Filled Array")).toBeNull());
    expect(queryByText("Empty Array")).toBeTruthy();
  });
});
