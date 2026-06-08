// Tests for the MindButton (Talk to OCICBB) feature.
//
// Covers:
//   1. Does NOT render for a non-allow-listed email.
//   2. DOES render the floating launcher for the allow-listed email.
//   3. Clicking the launcher opens the chat panel.

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";
import { MindButton, MIND_BUTTON_ALLOWED_EMAILS } from "./MindButton";
import type { Account } from "../lib/api";

function makeAccount(overrides: Partial<Account> = {}): Account {
  return {
    tenant_id: "ten_test001",
    tenant_key: "sol_live_test",
    name: "Test Operator",
    email: "test@example.com",
    plan: "standard",
    active: true,
    is_demo: false,
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
    has_payment_method: false,
    accounts_count: 0,
    bills_count: 0,
    clients_count: 0,
    onboarding_array_estimate: null,
    all_set: false,
    session: null,
    ...overrides,
  };
}

const ALLOWED_EMAIL = MIND_BUTTON_ALLOWED_EMAILS[0];

describe("MindButton", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("does not render when there is no account email", () => {
    // Email-based allow-listing was removed Jun 6'26 (beta: show to everyone
    // with an authenticated email). The only remaining gate is "has an email".
    const { queryByLabelText } = render(
      <MindButton account={makeAccount({ email: null })} />,
    );
    expect(queryByLabelText("Talk to OCICBB")).toBeNull();
  });

  it("renders the launcher for the allow-listed email", () => {
    const { queryByLabelText } = render(
      <MindButton account={makeAccount({ email: ALLOWED_EMAIL })} />,
    );
    expect(queryByLabelText("Talk to OCICBB")).not.toBeNull();
  });

  it("does not render on the read-only demo tenant", () => {
    const { queryByLabelText } = render(
      <MindButton account={makeAccount({ email: ALLOWED_EMAIL, is_demo: true })} />,
    );
    expect(queryByLabelText("Talk to OCICBB")).toBeNull();
  });

  it("opens the chat panel when the launcher is clicked", () => {
    const { getByLabelText, queryByLabelText } = render(
      <MindButton account={makeAccount({ email: ALLOWED_EMAIL })} />,
    );
    // Panel is closed initially.
    expect(queryByLabelText("OCICBB chat")).toBeNull();

    fireEvent.click(getByLabelText("Talk to OCICBB"));

    // Panel (dialog) is now present.
    expect(queryByLabelText("OCICBB chat")).not.toBeNull();
  });
});
