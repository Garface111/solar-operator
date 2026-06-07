// Tests for the masonry-pack sorted layout in SandboxCanvas.
//
// Verifies that computeSortedPositionsFromApiClients (via the exported helpers)
// produces gap-free bounding boxes — i.e. no two client cards overlap — for
// realistic client shapes including tall cards (2+ login groups or many arrays).

import { describe, it, expect } from "vitest";
import {
  estimateCardHeight,
  cardSpanForApi,
} from "../components/sandbox/SandboxCanvas";
import type { CanvasClientData } from "../lib/api";

// Re-implement the layout function here using the exported helpers so we can
// test position math without importing the full React component tree.
// This is the same logic as computeSortedPositionsFromApiClients in SandboxCanvas.tsx.

const GRID = {
  full:    { COL_W: 330, ROW_H: 295 },
  compact: { COL_W: 250, ROW_H: 200 },
  dense:   { COL_W: 190, ROW_H:  90 },
} as const;
type Density = keyof typeof GRID;

const DENSITY_COLS: Record<Density, number> = { full: 4, compact: 5, dense: 7 };

function layoutClients(
  clients: CanvasClientData[],
  density: Density,
): Map<number, { x: number; y: number }> {
  const cols = DENSITY_COLS[density];
  const { COL_W, ROW_H } = GRID[density];
  const colCursors: number[] = new Array(cols).fill(40);
  const map = new Map<number, { x: number; y: number }>();
  for (const client of clients) {
    let bestCol = 0;
    for (let c = 1; c < cols; c++) {
      if (colCursors[c] < colCursors[bestCol]) bestCol = c;
    }
    map.set(client.id, { x: bestCol * COL_W + 40, y: colCursors[bestCol] });
    colCursors[bestCol] += cardSpanForApi(client, density) * ROW_H;
  }
  return map;
}

function boundingBox(
  pos: { x: number; y: number },
  client: CanvasClientData,
  density: Density,
): { x1: number; y1: number; x2: number; y2: number } {
  const { COL_W, ROW_H } = GRID[density];
  const span = cardSpanForApi(client, density);
  return {
    x1: pos.x,
    y1: pos.y,
    x2: pos.x + COL_W - 20, // subtract gutter
    y2: pos.y + span * ROW_H - 16, // subtract inter-card gap
  };
}

function overlaps(
  a: { x1: number; y1: number; x2: number; y2: number },
  b: { x1: number; y1: number; x2: number; y2: number },
): boolean {
  // Two rectangles overlap if they are NOT separated on either axis.
  return !(a.x2 <= b.x1 || b.x2 <= a.x1 || a.y2 <= b.y1 || b.y2 <= a.y1);
}

// ── Fixtures ─────────────────────────────────────────────────────────────────

function makeAccount(arrayName: string | null, provider = "GMP", origin: number | null = null) {
  return {
    id: Math.random() * 1e9 | 0,
    provider,
    account_number: "0000-0000",
    service_address: null,
    canvas_x: null,
    canvas_y: null,
    canvas_pinned: false,
    array_id: arrayName ? 1 : null,
    array_name: arrayName,
    nepool_gis_id: null,
    login_origin_client_id: origin,
  };
}

function makeClient(
  id: number,
  accounts: ReturnType<typeof makeAccount>[],
): CanvasClientData {
  return {
    id,
    name: `Client ${id}`,
    canvas_x: null,
    canvas_y: null,
    canvas_pinned: false,
    accounts,
  };
}

// Single-array client — small card, fits in 1 slot.
const singleArrayClient = (id: number) =>
  makeClient(id, [makeAccount("Array 1")]);

// Client with 2 GMP login groups (different login origins) — tall card.
const twoLoginGroupClient = (id: number) =>
  makeClient(id, [
    makeAccount("Array A", "GMP", null),   // home login
    makeAccount("Array B", "GMP", 99),     // borrowed login from client 99
    makeAccount("Array C", "VEC", null),   // VEC login
  ]);

// Client with 4 arrays in one login group — overflows 295px slot.
const fourArrayClient = (id: number) =>
  makeClient(id, [
    makeAccount("Array 1"),
    makeAccount("Array 2"),
    makeAccount("Array 3"),
    makeAccount("Array 4"),
  ]);

// Green Valley Farm shape from demo: GMP×2 accounts (4 arrays) + VEC (2 arrays).
const greenValleyFarm = makeClient(1, [
  makeAccount("East Field Array", "GMP", null),
  makeAccount("Barn Roof Array",  "GMP", null),
  makeAccount("West Pasture Array", "GMP", null),
  makeAccount("Hillside Array", "VEC", null),
  makeAccount("Creek Side Array", "VEC", null),
]);

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("estimateCardHeight", () => {
  it("single login group, 1 array fits in full ROW_H (295)", () => {
    expect(estimateCardHeight(1, 1, "full")).toBeLessThan(295);
  });

  it("single login group, 3 arrays fits in full ROW_H", () => {
    expect(estimateCardHeight(1, 3, "full")).toBeLessThan(295);
  });

  it("single login group, 4 arrays overflows full ROW_H", () => {
    expect(estimateCardHeight(1, 4, "full")).toBeGreaterThan(295);
  });

  it("2 login groups, 1 array each overflows full ROW_H", () => {
    expect(estimateCardHeight(2, 2, "full")).toBeGreaterThan(295);
  });

  it("dense mode always returns collapsed height (40)", () => {
    expect(estimateCardHeight(5, 20, "dense")).toBe(40);
  });
});

describe("cardSpanForApi", () => {
  it("single-array client gets span=1", () => {
    expect(cardSpanForApi(singleArrayClient(1), "full")).toBe(1);
  });

  it("client with 4 arrays in one login group gets span=2", () => {
    expect(cardSpanForApi(fourArrayClient(1), "full")).toBe(2);
  });

  it("client with 2 login groups gets span=2", () => {
    expect(cardSpanForApi(twoLoginGroupClient(1), "full")).toBe(2);
  });

  it("dense mode always gives span=1 regardless of complexity", () => {
    expect(cardSpanForApi(twoLoginGroupClient(1), "dense")).toBe(1);
    expect(cardSpanForApi(fourArrayClient(1), "dense")).toBe(1);
  });

  it("client with no accounts gets span=1 (empty card)", () => {
    expect(cardSpanForApi(makeClient(1, []), "full")).toBe(1);
  });
});

describe("layout — no overlap", () => {
  function assertNoOverlap(clients: CanvasClientData[], density: Density) {
    const positions = layoutClients(clients, density);
    const entries = clients.map((c) => ({
      client: c,
      bb: boundingBox(positions.get(c.id)!, c, density),
    }));
    for (let i = 0; i < entries.length; i++) {
      for (let j = i + 1; j < entries.length; j++) {
        const a = entries[i];
        const b = entries[j];
        expect(
          overlaps(a.bb, b.bb),
          `${a.client.name} overlaps ${b.client.name}`,
        ).toBe(false);
      }
    }
  }

  it("8 single-array clients — no overlaps in full density", () => {
    const clients = Array.from({ length: 8 }, (_, i) => singleArrayClient(i + 1));
    assertNoOverlap(clients, "full");
  });

  it("8 single-array clients fill in column-major order (span=1 all equal height)", () => {
    const clients = Array.from({ length: 8 }, (_, i) => singleArrayClient(i + 1));
    const positions = layoutClients(clients, "full");
    // With 4 columns all equal, first 4 cards should go to cols 0-3 (x = 40, 370, 700, 1030).
    expect(positions.get(1)!.x).toBe(40);
    expect(positions.get(2)!.x).toBe(370);
    expect(positions.get(3)!.x).toBe(700);
    expect(positions.get(4)!.x).toBe(1030);
    // Cards 5-8 go back to cols 0-3 (same cursor height, ties broken left-to-right).
    expect(positions.get(5)!.x).toBe(40);
    expect(positions.get(5)!.y).toBe(335); // 40 + 295
  });

  it("mix of tall and short clients — no overlaps", () => {
    const clients = [
      singleArrayClient(1),
      twoLoginGroupClient(2), // tall, span=2
      singleArrayClient(3),
      fourArrayClient(4),     // tall, span=2
      singleArrayClient(5),
    ];
    assertNoOverlap(clients, "full");
  });

  it("all tall clients (4 login groups each) — no overlaps", () => {
    const clients = Array.from({ length: 6 }, (_, i) => twoLoginGroupClient(i + 1));
    assertNoOverlap(clients, "full");
  });

  it("Green Valley Farm shape — no overlap with 4 other clients", () => {
    const clients = [
      greenValleyFarm,
      singleArrayClient(2),
      fourArrayClient(3),
      singleArrayClient(4),
      singleArrayClient(5),
    ];
    assertNoOverlap(clients, "full");
  });

  it("empty client list — returns empty map, no crash", () => {
    const positions = layoutClients([], "full");
    expect(positions.size).toBe(0);
  });

  it("single client — position is the origin (40, 40)", () => {
    const positions = layoutClients([singleArrayClient(1)], "full");
    expect(positions.get(1)).toEqual({ x: 40, y: 40 });
  });

  it("compact density — no overlaps with mix of clients", () => {
    const clients = [
      singleArrayClient(1),
      twoLoginGroupClient(2),
      singleArrayClient(3),
      singleArrayClient(4),
    ];
    assertNoOverlap(clients, "compact");
  });
});
