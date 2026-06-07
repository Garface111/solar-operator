import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { QuarterlyProgressChip } from "../components/QuarterlyProgressChip";
import type { QuarterlyProgress } from "../lib/api";

// ── mocks ────────────────────────────────────────────────────────────────────

const mockGetQuarterlyProgress = vi.fn<[number], Promise<QuarterlyProgress>>();

vi.mock("../lib/api", () => ({
  getQuarterlyProgress: (...args: Parameters<typeof mockGetQuarterlyProgress>) =>
    mockGetQuarterlyProgress(...args),
}));

function makeProgress(overrides: Partial<QuarterlyProgress> = {}): QuarterlyProgress {
  return {
    quarter: "Q2-2026",
    quarter_start: "2026-04-01",
    quarter_end: "2026-06-30",
    ready_arrays: [],
    missing_arrays: [],
    total_arrays: 0,
    all_ready: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ── tests ─────────────────────────────────────────────────────────────────────

describe("QuarterlyProgressChip", () => {
  it("renders loading skeleton before data arrives", () => {
    // Never resolves during this test
    mockGetQuarterlyProgress.mockReturnValue(new Promise(() => {}));
    const { container } = render(<QuarterlyProgressChip clientId={1} />);
    // The loading skeleton should be present (animated pulse elements)
    const pulseEls = container.querySelectorAll(".animate-pulse");
    expect(pulseEls.length).toBeGreaterThan(0);
  });

  it("shows all-ready state when all_ready is true", async () => {
    mockGetQuarterlyProgress.mockResolvedValue(
      makeProgress({
        all_ready: true,
        total_arrays: 3,
        ready_arrays: [
          { id: 1, name: "Catamount" },
          { id: 2, name: "Starlake" },
          { id: 3, name: "Pittsfield" },
        ],
        missing_arrays: [],
      }),
    );

    render(<QuarterlyProgressChip clientId={1} />);
    await waitFor(() => {
      expect(screen.getByTestId("quarterly-progress-chip")).toBeTruthy();
    });
    expect(screen.getByText(/Reports ready to ship/i)).toBeTruthy();
    expect(screen.getByText(/Q2-2026/i)).toBeTruthy();
  });

  it("shows missing arrays when not all ready", async () => {
    mockGetQuarterlyProgress.mockResolvedValue(
      makeProgress({
        quarter: "Q3-2026",
        all_ready: false,
        total_arrays: 2,
        ready_arrays: [{ id: 1, name: "Catamount" }],
        missing_arrays: [
          { id: 2, name: "Starlake", missing_months: ["2026-09"] },
        ],
      }),
    );

    render(<QuarterlyProgressChip clientId={1} />);
    await waitFor(() => {
      expect(screen.getByTestId("quarterly-progress-chip")).toBeTruthy();
    });
    expect(screen.getByText(/1 of 2/)).toBeTruthy();
    expect(screen.getByText(/Starlake/)).toBeTruthy();
    expect(screen.getByText(/Sep/i)).toBeTruthy(); // shortMonth("2026-09") === "Sep"
  });

  it("calls onSendReports when Send button clicked in all-ready state", async () => {
    mockGetQuarterlyProgress.mockResolvedValue(
      makeProgress({ all_ready: true, total_arrays: 1, ready_arrays: [{ id: 1, name: "A" }] }),
    );
    const onSend = vi.fn();
    render(<QuarterlyProgressChip clientId={1} onSendReports={onSend} />);

    await waitFor(() => screen.getByText("Send"));
    screen.getByText("Send").click();
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("hides Send button when onSendReports is not provided", async () => {
    mockGetQuarterlyProgress.mockResolvedValue(
      makeProgress({ all_ready: true, total_arrays: 1, ready_arrays: [{ id: 1, name: "A" }] }),
    );
    render(<QuarterlyProgressChip clientId={1} />);

    await waitFor(() => screen.getByText(/Reports ready to ship/i));
    expect(screen.queryByText("Send")).toBeNull();
  });

  it("renders nothing on network error", async () => {
    mockGetQuarterlyProgress.mockRejectedValue(new Error("Network error"));
    const { container } = render(<QuarterlyProgressChip clientId={1} />);
    await waitFor(() => {
      // After rejection, the component should render nothing (null)
      expect(container.querySelector('[data-testid="quarterly-progress-chip"]')).toBeNull();
    });
  });
});
