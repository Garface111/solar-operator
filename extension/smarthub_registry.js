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
    "adamsec.smarthub.coop": {
      provider: "adams_ec",
      name: "Adams Electric Cooperative",
    },
    "bartonelectric.smarthub.coop": {
      provider: "barton",
      name: "Village of Barton (Barton Village Electric)",
    },
    "bedfordrec.smarthub.coop": {
      provider: "bedford_rec",
      name: "Bedford Rural Electric Cooperative",
    },
    "belmontlight.smarthub.coop": {
      provider: "belmont",
      name: "Belmont Municipal Light Department",
    },
    "blockisland.smarthub.coop": {
      provider: "block_island",
      name: "Block Island Power Company",
    },
    "butler.smarthub.coop": {
      provider: "butler",
      name: "Butler Municipal Electric Light & Power",
    },
    "central.smarthub.coop": {
      provider: "central_ec",
      name: "Central Electric Cooperative",
    },
    "choptankelectric.smarthub.coop": {
      provider: "choptank",
      name: "Choptank Electric Cooperative",
    },
    "claverack.smarthub.coop": {
      provider: "claverack_rec",
      name: "Claverack Rural Electric Cooperative",
    },
    "concord.smarthub.coop": {
      provider: "concord",
      name: "Concord Municipal Light Plant",
    },
    "dce.smarthub.coop": {
      provider: "dce",
      name: "Delaware County Electric Cooperative",
    },
    "decoop.smarthub.coop": {
      provider: "dec",
      name: "Delaware Electric Cooperative",
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
    "newenterpriserec.smarthub.coop": {
      provider: "new_enterprise_rec",
      name: "New Enterprise Rural Electric Cooperative",
    },
    "nhec.smarthub.coop": {
      provider: "nhec",
      name: "New Hampshire Electric Cooperative",
    },
    "northwesternrec.smarthub.coop": {
      provider: "northwestern_rec",
      name: "Northwestern Rural Electric Cooperative",
    },
    "reaenergy.smarthub.coop": {
      provider: "rea_energy",
      name: "REA Energy Cooperative",
    },
    "somersetrec.smarthub.coop": {
      provider: "somerset_rec",
      name: "Somerset Rural Electric Cooperative",
    },
    "srec.smarthub.coop": {
      provider: "srec",
      name: "Steuben Rural Electric Cooperative",
    },
    "stoweelectric.smarthub.coop": {
      provider: "stowe",
      name: "Village of Stowe Electric Department",
    },
    "tricountyrec.smarthub.coop": {
      provider: "tri_county_rec",
      name: "Tri-County Rural Electric Cooperative",
    },
    "unitedpa.smarthub.coop": {
      provider: "united_ec",
      name: "United Electric Cooperative",
    },
    "valleyrec.smarthub.coop": {
      provider: "valley_rec",
      name: "Valley Rural Electric Cooperative",
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
    "warrenec.smarthub.coop": {
      provider: "warren_ec",
      name: "Warren Electric Cooperative",
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
