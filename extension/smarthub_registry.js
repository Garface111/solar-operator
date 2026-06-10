// smarthub_registry.js — GENERATED FILE. DO NOT EDIT BY HAND.
//
// Source of truth: api/data/providers/*.csv (rows with a smarthub_host).
// Regenerate:  python scripts/gen_smarthub_registry_js.py
// CI verifies this file is in sync via --check.
//
// Exported as window.SMARTHUB_REGISTRY so smarthub_content.js can read it
// without module imports (content scripts run in the page context).

(function () {
  "use strict";

  // Maps *.smarthub.coop hostname → lowercase provider code (matches DB)
  const SMARTHUB_REGISTRY = {
    "bartonelectric.smarthub.coop": {
      provider: "barton",
      name: "Village of Barton (Barton Village Electric)",
    },
    "belmontlight.smarthub.coop": {
      provider: "belmont",
      name: "Belmont Municipal Light Department",
    },
    "blockisland.smarthub.coop": {
      provider: "block_island",
      name: "Block Island Power Company",
    },
    "concord.smarthub.coop": {
      provider: "concord",
      name: "Concord Municipal Light Plant",
    },
    "emec.smarthub.coop": {
      provider: "emec",
      name: "Eastern Maine Electric Cooperative",
    },
    "hull.smarthub.coop": {
      provider: "hull",
      name: "Hull Municipal Lighting Plant",
    },
    "klpd.smarthub.coop": {
      provider: "klpd",
      name: "Kennebunk Light & Power District",
    },
    "ludlow.smarthub.coop": {
      provider: "ludlow",
      name: "Village of Ludlow Electric Light Department",
    },
    "nhec.smarthub.coop": {
      provider: "nhec",
      name: "New Hampshire Electric Cooperative",
    },
    "stoweelectric.smarthub.coop": {
      provider: "stowe",
      name: "Village of Stowe Electric Department",
    },
    "vermontelectric.smarthub.coop": {
      provider: "vec",
      name: "Vermont Electric Cooperative",
    },
    "villageofenosburgfalls.smarthub.coop": {
      provider: "enosburg",
      name: "Village of Enosburg Falls",
    },
    "villageofhydepark.smarthub.coop": {
      provider: "hyde_park",
      name: "Village of Hyde Park",
    },
    "washingtonelectric.smarthub.coop": {
      provider: "wec",
      name: "Washington Electric Co-op",
    },
  };

  // Detect provider from the current page's hostname.
  // Falls back to "vec" (the first deployed utility) for unknown *.smarthub.coop hosts.
  function detectProvider(hostname) {
    const entry = SMARTHUB_REGISTRY[hostname.toLowerCase()];
    if (entry) return entry;
    if (hostname.endsWith(".smarthub.coop")) {
      console.warn(
        `[Solar Operator] Unknown SmartHub host: ${hostname}. ` +
          "Treating as VEC (vermontelectric). Add this host to a provider CSV."
      );
      return { provider: "vec", name: "Unknown SmartHub Utility" };
    }
    return null;
  }

  // Expose on window so smarthub_content.js (loaded in the same content-script
  // world) can call window.SMARTHUB_REGISTRY and window.detectSmartHubProvider.
  window.SMARTHUB_REGISTRY = SMARTHUB_REGISTRY;
  window.detectSmartHubProvider = detectProvider;
})();
