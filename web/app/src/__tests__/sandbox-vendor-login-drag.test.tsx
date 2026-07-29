// Monitoring logins (SolarEdge / Fronius / Locus / AlsoEnergy) are draggable
// in the sandbox, exactly like the GMP / VEC utility logins.
//
// Before this, ClientNode rendered them "render-only: monitor arrays re-home via
// the clients table, not canvas drag" — so on the surface built for customising
// clients, everything that was not GMP or VEC simply could not be moved (Ford,
// 2026-07-29). The two are genuinely different objects: a utility login is a set
// of UtilityAccounts, while a vendor login owns Arrays directly and has NO
// UtilityAccount, which is why neither of the existing movers could see it.
//
// What matters here is that the handle is not decorative: it must publish a
// payload the receiving card can act on, and the receiving card must call the
// vendor mover — not silently do nothing.

import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import React from "react";
import { CanvasActionsContext, type CanvasActions } from "../components/sandbox/canvasContext";
import { ClientNodeComponent, type ClientNodeData } from "../components/sandbox/ClientNode";
import type { ClientData } from "../components/sandbox/mockData";

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
    moveVendorLoginToClient: vi.fn(),
    moveVendorArrayToClient: vi.fn(),
    getOriginClient: () => null,
    updateClient: vi.fn().mockResolvedValue(undefined),
    togglePin: vi.fn(),
    ...overrides,
  };
}

const CLIENT: ClientData = {
  id: "client_7",
  name: "GMCS Producing Accounts",
  accounts: [],
  vendorLogins: [
    {
      vendor: "solaredge",
      login: "key ••••2W82",
      arrays: [{ id: 501, name: "Waterford", fuel_type: "solar", mwh_per_qtr: 67.221 }],
    },
    {
      vendor: "fronius",
      login: "fronius",
      arrays: [
        { id: 502, name: "Chester", fuel_type: "solar", mwh_per_qtr: 81.583 },
        { id: 503, name: "Tannery Brook", fuel_type: "solar", mwh_per_qtr: 12.0 },
      ],
    },
  ],
};

function renderNode(actions: CanvasActions, client: ClientData = CLIENT) {
  const data: ClientNodeData = { client, expanded: true } as ClientNodeData;
  return render(
    <CanvasActionsContext.Provider value={actions}>
      <ClientNodeComponent id={client.id} data={data as never} selected={false} />
    </CanvasActionsContext.Provider>,
  );
}

/** A DataTransfer good enough for jsdom (which ships none). */
function makeDataTransfer() {
  const store: Record<string, string> = {};
  return {
    data: store,
    effectAllowed: "",
    dropEffect: "",
    types: [] as string[],
    setData(type: string, val: string) {
      store[type] = val;
      this.types.push(type);
    },
    getData(type: string) {
      return store[type] ?? "";
    },
    setDragImage() {},
  };
}

describe("sandbox vendor login drag", () => {
  it("renders a drag handle for every vendor login AND every vendor array", () => {
    const { container } = renderNode(makeActions());
    const handles = container.querySelectorAll('[aria-label="Drag handle"]');
    // 2 logins + 3 arrays across them
    expect(handles.length).toBe(5);
    handles.forEach((h) => expect(h.getAttribute("draggable")).toBe("true"));
  });

  it("dragging a login publishes the vendor + login it belongs to", () => {
    const { container } = renderNode(makeActions());
    const handle = container.querySelectorAll('[aria-label="Drag handle"]')[0];
    const dt = makeDataTransfer();
    fireEvent.dragStart(handle, { dataTransfer: dt });

    const raw = dt.getData("application/x-so-vendor-login");
    expect(raw, "login handle published no payload — it would be decorative").toBeTruthy();
    expect(JSON.parse(raw)).toEqual({
      srcClientId: "client_7",
      vendor: "solaredge",
      login: "key ••••2W82",
    });
  });

  it("dragging one array publishes that array alone, with its parent login", () => {
    const { container } = renderNode(makeActions());
    // handles are [login1, arr501, login2, arr502, arr503] in DOM order
    const handles = container.querySelectorAll('[aria-label="Drag handle"]');
    const dt = makeDataTransfer();
    fireEvent.dragStart(handles[4], { dataTransfer: dt });

    expect(JSON.parse(dt.getData("application/x-so-vendor-array"))).toEqual({
      srcClientId: "client_7",
      vendor: "fronius",
      login: "fronius",
      arrayId: 503,
    });
  });

  it("dropping a vendor login on another client calls the vendor mover", () => {
    const moveVendorLoginToClient = vi.fn();
    const other: ClientData = { id: "client_9", name: "Pbozuwa", accounts: [] };
    const { container } = renderNode(makeActions({ moveVendorLoginToClient }), other);

    const dt = makeDataTransfer();
    dt.setData(
      "application/x-so-vendor-login",
      JSON.stringify({ srcClientId: "client_7", vendor: "locus", login: "johnson_hardware" }),
    );
    fireEvent.drop(container.firstChild as Element, { dataTransfer: dt });

    expect(moveVendorLoginToClient).toHaveBeenCalledWith(
      "client_7", "locus", "johnson_hardware", "client_9",
    );
  });

  it("dropping a vendor array on another client calls the array mover", () => {
    const moveVendorArrayToClient = vi.fn();
    const other: ClientData = { id: "client_9", name: "Pbozuwa", accounts: [] };
    const { container } = renderNode(makeActions({ moveVendorArrayToClient }), other);

    const dt = makeDataTransfer();
    dt.setData(
      "application/x-so-vendor-array",
      JSON.stringify({ srcClientId: "client_7", vendor: "fronius", login: "fronius", arrayId: 502 }),
    );
    fireEvent.drop(container.firstChild as Element, { dataTransfer: dt });

    expect(moveVendorArrayToClient).toHaveBeenCalledWith(
      "client_7", "fronius", "fronius", 502, "client_9",
    );
  });

  it("does not move anything when dropped on its own card", () => {
    const moveVendorLoginToClient = vi.fn();
    const { container } = renderNode(makeActions({ moveVendorLoginToClient }));
    const dt = makeDataTransfer();
    dt.setData(
      "application/x-so-vendor-login",
      JSON.stringify({ srcClientId: "client_7", vendor: "solaredge", login: "key ••••2W82" }),
    );
    fireEvent.drop(container.firstChild as Element, { dataTransfer: dt });
    expect(moveVendorLoginToClient).not.toHaveBeenCalled();
  });

  it("a client with no vendor logins renders no vendor handles", () => {
    const bare: ClientData = { id: "client_9", name: "Bare", accounts: [] };
    const { container } = renderNode(makeActions(), bare);
    expect(container.querySelectorAll('[aria-label="Drag handle"]').length).toBe(0);
  });
});
