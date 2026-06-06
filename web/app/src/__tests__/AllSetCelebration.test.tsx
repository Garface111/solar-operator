// Tests for the AllSetCelebration modal.
//
// Covers:
//   1. Renders nothing when all_set is false.
//   2. Renders modal when all_set transitions false → true.
//   3. Does NOT show modal on next mount if localStorage flag is set.

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { AllSetCelebration } from "../components/AllSetCelebration";
import type { Account } from "../lib/api";

function makeAccount(overrides: Partial<Account> = {}): Account {
  return {
    tenant_id: "ten_test001",
    tenant_key: "sol_live_test",
    name: "Test Operator",
    email: "test@example.com",
    plan: "standard",
    active: true,
    subscription_status: "trialing",
    report_frequency: "quarterly",
    cc_on_reports: false,
    has_password: false,
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
    all_set: false,
    session: null,
    ...overrides,
  };
}

describe("AllSetCelebration", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("renders nothing when all_set is false", () => {
    const { container } = render(
      <AllSetCelebration account={makeAccount({ all_set: false })} />,
    );
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });

  it("renders modal when all_set transitions false → true", () => {
    const { rerender, container } = render(
      <AllSetCelebration account={makeAccount({ all_set: false })} />,
    );
    // No dialog while still false.
    expect(container.querySelector('[role="dialog"]')).toBeNull();

    act(() => {
      rerender(
        <AllSetCelebration account={makeAccount({ all_set: true })} />,
      );
    });

    expect(container.querySelector('[role="dialog"]')).not.toBeNull();
  });

  it("does not show modal on next mount if localStorage flag is set", () => {
    // Simulate having already seen the celebration.
    localStorage.setItem("so_all_set_seen_ten_test001", "true");

    const { rerender, container } = render(
      <AllSetCelebration account={makeAccount({ all_set: false })} />,
    );
    act(() => {
      rerender(
        <AllSetCelebration account={makeAccount({ all_set: true })} />,
      );
    });

    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });
});
