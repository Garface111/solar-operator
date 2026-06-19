// Dispatcher tests for the shared /reports route.
//
// /reports is rendered for BOTH products on a shared backend. The ReportsTab
// dispatcher must show:
//   • the NEPOOL quarterly surface for a "nepool" (or product-less) tenant, and
//   • the Array Operator "Billing Run" for an "array_operator" tenant,
// never the wrong one. This guards against a repeat of the Jun-17 regression
// where the billing redesign replaced the NEPOOL surface outright.

import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "../ui/Toast";

// Stub both concrete surfaces so the test asserts ONLY the routing decision,
// not their internals (which have their own tests).
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

describe("ReportsTab product dispatcher", () => {
  it("renders the NEPOOL surface for a nepool tenant", async () => {
    mockProduct("nepool");
    render(<ReportsTab />, { wrapper: Wrapper });
    await waitFor(() =>
      expect(screen.getByTestId("nepool-surface")).toBeTruthy(),
    );
    expect(screen.queryByTestId("billing-surface")).toBeNull();
  });

  it("defaults to the NEPOOL surface when product is unset", async () => {
    mockProduct(null);
    render(<ReportsTab />, { wrapper: Wrapper });
    await waitFor(() =>
      expect(screen.getByTestId("nepool-surface")).toBeTruthy(),
    );
    expect(screen.queryByTestId("billing-surface")).toBeNull();
  });

  it("renders the Array Operator billing surface for an array_operator tenant", async () => {
    mockProduct("array_operator");
    render(<ReportsTab />, { wrapper: Wrapper });
    await waitFor(() =>
      expect(screen.getByTestId("billing-surface")).toBeTruthy(),
    );
    expect(screen.queryByTestId("nepool-surface")).toBeNull();
  });
});
