// Tests for the purge-countdown ghost row in the sandbox ClientNode.
//
// Covers:
//   1. Active array renders no ghost/chip.
//   2. Deleted array renders dashed-border class + "Purges in" text.
//   3. Clicking Restore calls restoreArray with the correct ids.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import React from "react";
import { CanvasActionsContext, type CanvasActions } from "../components/sandbox/canvasContext";
import { ClientNodeComponent, type ClientNodeData } from "../components/sandbox/ClientNode";
import type { ClientData } from "../components/sandbox/mockData";

// @xyflow/react uses ResizeObserver which jsdom doesn't provide.
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// Mock restoreArray so tests don't hit the network.
vi.mock("../lib/api", () => ({
  restoreArray: vi.fn().mockResolvedValue({ ok: true, array: {} }),
}));

// Mock useToast so components that call it don't blow up without a provider.
vi.mock("../ui/Toast", () => ({
  useToast: () => ({
    show: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    success: vi.fn(),
  }),
}));

import { restoreArray } from "../lib/api";

function makeActions(overrides: Partial<CanvasActions> = {}): CanvasActions {
  return {
    density: "full",
    toggleExpand: vi.fn(),
    startRename: vi.fn(),
    finishRename: vi.fn(),
    cancelRename: vi.fn(),
    renamingNodeId: null,
    startRenameArray: vi.fn(),
    finishRenameArray: vi.fn(),
    deleteNode: vi.fn(),
    detachAccount: vi.fn(),
    moveAccountToClient: vi.fn(),
    detachLogin: vi.fn(),
    moveLoginToClient: vi.fn(),
    moveArrayToClient: vi.fn(),
    getOriginClient: () => null,
    updateClient: vi.fn().mockResolvedValue(undefined),
    togglePin: vi.fn(),
    ...overrides,
  };
}

const DELETED_AT = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(); // 3 days ago

function makeClient(arrayDeletedAt: string | null = null): ClientData {
  return {
    id: 55,
    name: "Ghost Client",
    pinned: false,
    accounts: [
      {
        id: "account_10",
        utility: "GMP",
        account_number: "5555-0001",
        owner_name: "Ghost Client",
        arrays: [
          {
            id: "arr_77",
            name: "Spooky Array",
            nepool_gis_id: "NE-VT-0000",
            mwh_per_qtr: 0,
            deleted_at: arrayDeletedAt,
          },
        ],
      },
    ],
  };
}

function makeNodeProps(client: ClientData, id = "client_55") {
  const data: ClientNodeData = { client, expanded: true, entryDelay: 0 };
  return { id, data: data as unknown as Record<string, unknown>, selected: false } as Parameters<typeof ClientNodeComponent>[0];
}

describe("sandbox deleted array ghost row", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("active array renders no purge chip", () => {
    const actions = makeActions();
    const { queryByText } = render(
      <CanvasActionsContext.Provider value={actions}>
        <ClientNodeComponent {...makeNodeProps(makeClient(null))} />
      </CanvasActionsContext.Provider>,
    );
    expect(queryByText(/Purges/i)).toBeNull();
    expect(queryByText("Restore")).toBeNull();
  });

  it("deleted array renders dashed-border row with purge chip", () => {
    const actions = makeActions();
    const { getByText, container } = render(
      <CanvasActionsContext.Provider value={actions}>
        <ClientNodeComponent {...makeNodeProps(makeClient(DELETED_AT))} />
      </CanvasActionsContext.Provider>,
    );
    // Chip text should include "Purges in"
    expect(getByText(/Purges in \d+d/)).not.toBeNull();
    // The ghost row should have border-dashed class
    const dashedEl = container.querySelector('.border-dashed');
    expect(dashedEl).not.toBeNull();
  });

  it("clicking Restore calls restoreArray with numeric client and array ids", async () => {
    const actions = makeActions();
    const { getByText } = render(
      <CanvasActionsContext.Provider value={actions}>
        <ClientNodeComponent {...makeNodeProps(makeClient(DELETED_AT))} />
      </CanvasActionsContext.Provider>,
    );
    fireEvent.click(getByText("Restore"));
    // restoreArray should be called with (55, 77) — parsed from "client_55" and "arr_77"
    expect(restoreArray).toHaveBeenCalledWith(55, 77);
  });
});
