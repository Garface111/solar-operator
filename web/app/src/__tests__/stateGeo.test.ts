import { describe, it, expect } from "vitest";
import { haversineMiles, milesToState, STATE_CENTROIDS } from "../lib/stateGeo";

describe("stateGeo", () => {
  it("has centroids for all 50 states + DC", () => {
    // 50 states + DC = 51 entries.
    expect(Object.keys(STATE_CENTROIDS).length).toBe(51);
    expect(STATE_CENTROIDS.VT).toBeDefined();
    expect(STATE_CENTROIDS.CA).toBeDefined();
    expect(STATE_CENTROIDS.DC).toBeDefined();
  });

  it("haversine is ~0 for identical points and symmetric", () => {
    const a = { lat: 44.0, lng: -72.7 };
    expect(haversineMiles(a, a)).toBeCloseTo(0, 5);
    const b = { lat: 34.0, lng: -118.0 };
    expect(haversineMiles(a, b)).toBeCloseTo(haversineMiles(b, a), 5);
  });

  it("VT↔CA is a continental distance (~2500-2800 mi)", () => {
    const d = haversineMiles(STATE_CENTROIDS.VT, STATE_CENTROIDS.CA);
    expect(d).toBeGreaterThan(2300);
    expect(d).toBeLessThan(3000);
  });

  it("milesToState returns null for unknown/blank state codes", () => {
    const from = { lat: 44.0, lng: -72.7 };
    expect(milesToState(from, "")).toBeNull();
    expect(milesToState(from, "ZZ")).toBeNull();
    expect(milesToState(from, "PR")).toBeNull();
  });

  it("ranks a VT operator's nearby states ahead of far ones", () => {
    // Operator sitting in central Vermont.
    const from = { lat: 44.26, lng: -72.58 };
    const states = ["CA", "VT", "NH", "TX", "MA"];
    const ranked = states
      .map((s) => ({ s, mi: milesToState(from, s)! }))
      .sort((a, b) => a.mi - b.mi)
      .map((x) => x.s);
    // VT (own state) first; NH/MA (neighbors) before TX/CA (far).
    expect(ranked[0]).toBe("VT");
    expect(ranked.indexOf("NH")).toBeLessThan(ranked.indexOf("TX"));
    expect(ranked.indexOf("MA")).toBeLessThan(ranked.indexOf("CA"));
    expect(ranked[ranked.length - 1]).toBe("CA");
  });
});
