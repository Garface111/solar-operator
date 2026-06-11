import { describe, it, expect } from "vitest";
import { splitPortals } from "../components/settings/UtilityConnectionsCard";

const CATALOG = [
  { code: "gmp" },
  { code: "vec" },
  { code: "wec" },
  { code: "national_grid_ma" },
];

describe("splitPortals", () => {
  it("splits connected vs others using the backend list", () => {
    const { connected, others, connectedCodes } = splitPortals(
      CATALOG,
      ["gmp", "vec"],
    );
    expect(connected.map((p) => p.code)).toEqual(["gmp", "vec"]);
    expect(others.map((p) => p.code)).toEqual(["wec", "national_grid_ma"]);
    expect(connectedCodes.has("gmp")).toBe(true);
    expect(connectedCodes.has("wec")).toBe(false);
  });

  it("is case-insensitive on provider codes", () => {
    const { connected } = splitPortals([{ code: "GMP" }], ["gmp"]);
    expect(connected.map((p) => p.code)).toEqual(["GMP"]);
  });

  it("falls back to legacy codes when the backend list is empty", () => {
    const { connected } = splitPortals(CATALOG, [], ["gmp"]);
    expect(connected.map((p) => p.code)).toEqual(["gmp"]);
  });

  it("prefers the backend list over legacy when both are present", () => {
    const { connected } = splitPortals(CATALOG, ["vec"], ["gmp"]);
    // Backend says vec; legacy gmp is ignored because backend is non-empty.
    expect(connected.map((p) => p.code)).toEqual(["vec"]);
  });

  it("returns no connected when nothing matches (drives the show-all fallback)", () => {
    const { connected, others } = splitPortals(CATALOG, [], []);
    expect(connected).toHaveLength(0);
    expect(others).toHaveLength(CATALOG.length);
  });
});
