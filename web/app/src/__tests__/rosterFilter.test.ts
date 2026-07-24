/** The roster rule is shared so the Clients table and the sandbox canvas can
 *  never disagree about which clients exist (Ford 2026-07-24: a retired client
 *  with 41 arrays rendered a sandbox card but had no table row). */
import { afterEach, describe, expect, it } from "vitest";
import { hideInactiveClients, rosterClients } from "../lib/rosterFilter";

type W = { __soGenrepEmbed?: boolean };

afterEach(() => {
  delete (window as W).__soGenrepEmbed;
});

const CLIENTS = [
  { id: 1, name: "GMCS Producing Accounts", active: true },
  { id: 2, name: "Bruce Genereaux", active: false },
  { id: 3, name: "Pbozuwa", active: true },
];

describe("rosterFilter", () => {
  it("hides retired clients inside the embed", () => {
    (window as W).__soGenrepEmbed = true;
    expect(hideInactiveClients()).toBe(true);
    expect(rosterClients(CLIENTS).map((c) => c.name)).toEqual([
      "GMCS Producing Accounts",
      "Pbozuwa",
    ]);
  });

  it("keeps every client in the standalone SPA (reactivate flow needs them)", () => {
    expect(hideInactiveClients()).toBe(false);
    expect(rosterClients(CLIENTS)).toHaveLength(3);
  });

  it("treats a missing `active` as active so an older payload isn't blanked", () => {
    (window as W).__soGenrepEmbed = true;
    const legacy = [{ id: 9, name: "No active field" }];
    expect(rosterClients(legacy)).toHaveLength(1);
  });

  it("passes null through untouched", () => {
    (window as W).__soGenrepEmbed = true;
    expect(rosterClients(null)).toBeNull();
  });

  it("is evaluated at call time, not module load", () => {
    // The flag is set by embed.tsx AFTER this module is imported; a cached
    // module-level const captured `false` and the filter silently never fired.
    expect(rosterClients(CLIENTS)).toHaveLength(3);
    (window as W).__soGenrepEmbed = true;
    expect(rosterClients(CLIENTS)).toHaveLength(2);
  });
});
