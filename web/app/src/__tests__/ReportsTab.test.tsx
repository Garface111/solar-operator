// NEPOOL Operator SPA isolation tests for /reports.
//
// This bundle is served at nepooloperator.com/accounts. The Reports tab MUST
// always be Automatic Reports (NepoolReportsTab) — never Array Operator
// offtaker Billing, even if account.product is mis-tagged array_operator.

import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "../ui/Toast";

vi.mock("../screens/NepoolReportsTab", () => ({
  default: () => <div data-testid="nepool-surface">NEPOOL quarterly reports</div>,
}));
vi.mock("../screens/BillingReportsTab", () => ({
  default: () => <div data-testid="billing-surface">Array Operator billing run</div>,
}));

vi.mock("../screens/DashboardLayout", () => ({
  useDashboardContext: vi.fn(),
}));

import ReportsTab from "../screens/ReportsTab";
import * as dashboardModule from "../screens/DashboardLayout";

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <MemoryRouter>
      <ToastProvider>{children}</ToastProvider>
    </MemoryRouter>
  );
}

function mockProduct(product: string | null) {
  vi.mocked(dashboardModule.useDashboardContext).mockReturnValue({
    account: { product } as any,
    failed: false,
    patchAccount: vi.fn(),
    retryLoad: vi.fn(),
  } as any);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ReportsTab — NEPOOL SPA isolation", () => {
  it("renders Automatic Reports for a nepool tenant", async () => {
    mockProduct("nepool");
    render(<ReportsTab />, { wrapper: Wrapper });
    await waitFor(() =>
      expect(screen.getByTestId("nepool-surface")).toBeTruthy(),
    );
    expect(screen.queryByTestId("billing-surface")).toBeNull();
  });

  it("defaults to Automatic Reports when product is unset", async () => {
    mockProduct(null);
    render(<ReportsTab />, { wrapper: Wrapper });
    await waitFor(() =>
      expect(screen.getByTestId("nepool-surface")).toBeTruthy(),
    );
    expect(screen.queryByTestId("billing-surface")).toBeNull();
  });

  it("never swaps in offtaker Billing for a mis-tagged array_operator tenant", async () => {
    mockProduct("array_operator");
    render(<ReportsTab />, { wrapper: Wrapper });
    await waitFor(() =>
      expect(screen.getByTestId("nepool-surface")).toBeTruthy(),
    );
    expect(screen.queryByTestId("billing-surface")).toBeNull();
  });
});
