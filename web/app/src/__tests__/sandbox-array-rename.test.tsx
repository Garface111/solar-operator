// Minimal smoke test for inline array rename in the sandbox ClientNode.
//
// Verifies:
//   1. Array name renders as a button (click-to-rename).
//   2. Clicking it calls startRenameArray with the numeric array ID.
//   3. When renamingNodeId matches, an input renders instead.
//   4. Pressing Enter calls finishRenameArray with the updated name.

import { describe, it, expect, vi } from "vitest";
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

const TEST_CLIENT: ClientData = {
  id: 99,
  name: "Test Client",
  pinned: false,
  accounts: [
    {
      id: "account_77",
      utility: "GMP",
      account_number: "0000-0001",
      owner_name: "Test Client",
      arrays: [
        { id: "arr_42", name: "Old Array Name", nepool_gis_id: "NE-VT-9999" },
      ],
    },
  ],
};

const NODE_DATA: ClientNodeData = {
  client: TEST_CLIENT,
  expanded: true,
  entryDelay: 0,
};

// NodeProps-compatible shim (only fields ClientNodeComponent actually reads)
function makeNodeProps(data: ClientNodeData, id = "client_99") {
  return { id, data: data as unknown as Record<string, unknown>, selected: false } as Parameters<typeof ClientNodeComponent>[0];
}

describe("sandbox array rename", () => {
  it("renders array name as a button", () => {
    const actions = makeActions();
    const { getByText } = render(
      <CanvasActionsContext.Provider value={actions}>
        <ClientNodeComponent {...makeNodeProps(NODE_DATA)} />
      </CanvasActionsContext.Provider>,
    );
    const btn = getByText("Old Array Name");
    expect(btn.tagName).toBe("BUTTON");
  });

  it("calls startRenameArray(42) when the array name button is clicked", () => {
    const actions = makeActions();
    const { getByText } = render(
      <CanvasActionsContext.Provider value={actions}>
        <ClientNodeComponent {...makeNodeProps(NODE_DATA)} />
      </CanvasActionsContext.Provider>,
    );
    fireEvent.click(getByText("Old Array Name"));
    expect(actions.startRenameArray).toHaveBeenCalledWith(42);
  });

  it("renders an input when renamingNodeId matches the array", () => {
    const actions = makeActions({ renamingNodeId: "array_42" });
    const { container } = render(
      <CanvasActionsContext.Provider value={actions}>
        <ClientNodeComponent {...makeNodeProps(NODE_DATA)} />
      </CanvasActionsContext.Provider>,
    );
    const input = container.querySelector("input[value='Old Array Name']");
    expect(input).not.toBeNull();
  });

  it("calls finishRenameArray with updated name on Enter", () => {
    const actions = makeActions({ renamingNodeId: "array_42" });
    const { container } = render(
      <CanvasActionsContext.Provider value={actions}>
        <ClientNodeComponent {...makeNodeProps(NODE_DATA)} />
      </CanvasActionsContext.Provider>,
    );
    const input = container.querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "New Array Name" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(actions.finishRenameArray).toHaveBeenCalledWith(42, "New Array Name");
  });
});
