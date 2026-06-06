// smarthub_registry.js — single source of truth for SmartHub utility hosts.
//
// Keep this file in sync with SMARTHUB_UTILITIES in api/adapters/smarthub.py.
// Adding a new utility = one entry here + one entry in the Python registry.
//
// Exported as window.SMARTHUB_REGISTRY so smarthub_content.js can read it
// without module imports (content scripts run in the page context, not ES modules).

(function () {
  "use strict";

  // Maps *.smarthub.coop hostname → lowercase provider code (matches DB)
  const SMARTHUB_REGISTRY = {
    "vermontelectric.smarthub.coop": {
      provider: "vec",
      name: "Vermont Electric Cooperative",
    },
    "washingtonelectric.smarthub.coop": {
      provider: "wec",
      name: "Washington Electric Cooperative",
    },
    "weci.smarthub.coop": {
      // Alternate WEC hostname
      provider: "wec",
      name: "Washington Electric Cooperative",
    },
    "stoweelectric.smarthub.coop": {
      provider: "stowe",
      name: "Stowe Electric Department",
    },
    "villageofhydepark.smarthub.coop": {
      provider: "hyde_park",
      name: "Village of Hyde Park",
    },
    "ludlow.smarthub.coop": {
      provider: "ludlow",
      name: "Village of Ludlow Electric",
    },
    "villageofenosburgfalls.smarthub.coop": {
      provider: "enosburg",
      name: "Village of Enosburg Falls",
    },
    "nhec.smarthub.coop": {
      provider: "nhec",
      name: "New Hampshire Electric Cooperative",
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
          "Treating as VEC (vermontelectric). Add this host to smarthub_registry.js."
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
