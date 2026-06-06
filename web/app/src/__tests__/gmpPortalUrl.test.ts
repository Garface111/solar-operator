// Regression test for the v1.4.5 extension localStorage-stale 404 bug.
//
// v1.4.5 wipes cookies but not localStorage. GMP's SPA at /account/ boots,
// reads the stale token from localStorage, hits its own API, gets a JSON 404,
// and the browser renders raw JSON. v1.4.6 added portal_cleaner.js to wipe
// localStorage at document_start.
//
// gmpPortalUrl() gates the landing URL on whether the installed extension
// supports localStorage wipe. v >= 1.4.6 → /account/ (fast path).
// v < 1.4.6 or unknown → / (root redirects to login on stale auth).

import { describe, it, expect } from "vitest";
import { gmpPortalUrl, GMP_ACCOUNT_URL, GMP_SAFE_URL } from "../lib/openPortalTab";

describe("gmpPortalUrl", () => {
  it("safe URL is the GMP marketing root (not /account/)", () => {
    expect(GMP_SAFE_URL).toBe("https://greenmountainpower.com/");
  });

  it("account URL is the GMP account dashboard", () => {
    expect(GMP_ACCOUNT_URL).toBe("https://greenmountainpower.com/account/");
  });

  // --- versions that SHOULD use the account URL (localStorage wipe present) ---

  it("returns account URL for v1.4.6 (first version with portal_cleaner.js)", () => {
    expect(gmpPortalUrl("1.4.6")).toBe(GMP_ACCOUNT_URL);
  });

  it("returns account URL for v1.4.7", () => {
    expect(gmpPortalUrl("1.4.7")).toBe(GMP_ACCOUNT_URL);
  });

  it("returns account URL for v1.5.0", () => {
    expect(gmpPortalUrl("1.5.0")).toBe(GMP_ACCOUNT_URL);
  });

  it("returns account URL for v2.0.0", () => {
    expect(gmpPortalUrl("2.0.0")).toBe(GMP_ACCOUNT_URL);
  });

  // --- versions that MUST use the safe fallback URL ---

  it("returns safe URL for v1.4.5 (the buggy Store version)", () => {
    expect(gmpPortalUrl("1.4.5")).toBe(GMP_SAFE_URL);
  });

  it("returns safe URL for v1.4.4", () => {
    expect(gmpPortalUrl("1.4.4")).toBe(GMP_SAFE_URL);
  });

  it("returns safe URL for v1.4.0", () => {
    expect(gmpPortalUrl("1.4.0")).toBe(GMP_SAFE_URL);
  });

  it("returns safe URL for v1.3.0", () => {
    expect(gmpPortalUrl("1.3.0")).toBe(GMP_SAFE_URL);
  });

  it("returns safe URL for v0.9.0", () => {
    expect(gmpPortalUrl("0.9.0")).toBe(GMP_SAFE_URL);
  });

  // --- null / unknown version → safe fallback ---

  it("returns safe URL when version is null (extension absent or not yet reported)", () => {
    expect(gmpPortalUrl(null)).toBe(GMP_SAFE_URL);
  });
});
