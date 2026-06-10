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
    "aec.smarthub.coop": {
      provider: "aiken",
      name: "Aiken Electric Cooperative",
    },
    "barcelectric.smarthub.coop": {
      provider: "barc",
      name: "BARC Electric Cooperative",
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
    "bemc.smarthub.coop": {
      provider: "brunswick_emc",
      name: "Brunswick Electric Membership Corporation",
    },
    "blockisland.smarthub.coop": {
      provider: "block_island",
      name: "Block Island Power Company",
    },
    "bremc.smarthub.coop": {
      provider: "blue_ridge_emc",
      name: "Blue Ridge Electric Membership Corporation",
    },
    "broadriverelectric.smarthub.coop": {
      provider: "broad_river",
      name: "Broad River Electric Cooperative",
    },
    "butler.smarthub.coop": {
      provider: "butler",
      name: "Butler Municipal Electric Light & Power",
    },
    "cbec.smarthub.coop": {
      provider: "cbec",
      name: "Craig-Botetourt Electric Cooperative (CBEC)",
    },
    "cec.smarthub.coop": {
      provider: "central_sc",
      name: "Central Electric Power Cooperative",
    },
    "cemc.smarthub.coop": {
      provider: "central_emc",
      name: "Central Electric Membership Corporation",
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
    "clayelectric.smarthub.coop": {
      provider: "clayelectric",
      name: "Clay Electric Cooperative",
    },
    "coastalelectric.smarthub.coop": {
      provider: "coastal_electric",
      name: "Coastal Electric Cooperative",
    },
    "cobbemc.smarthub.coop": {
      provider: "cobb_emc",
      name: "Cobb EMC",
    },
    "comelec.smarthub.coop": {
      provider: "comelec",
      name: "Community Electric Cooperative",
    },
    "concord.smarthub.coop": {
      provider: "concord",
      name: "Concord Municipal Light Plant",
    },
    "cvec.smarthub.coop": {
      provider: "cvec",
      name: "Central Virginia Electric Cooperative (CVEC)",
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
    "energyunited.smarthub.coop": {
      provider: "energy_united",
      name: "EnergyUnited",
    },
    "erec.smarthub.coop": {
      provider: "erec",
      name: "Escambia River Electric Cooperative (EREC)",
    },
    "fcremc.smarthub.coop": {
      provider: "four_county_emc",
      name: "Four County EMC",
    },
    "fkec.smarthub.coop": {
      provider: "fkec",
      name: "Florida Keys Electric Cooperative (FKEC)",
    },
    "gcec.smarthub.coop": {
      provider: "gcec",
      name: "Gulf Coast Electric Cooperative (GCEC)",
    },
    "gladesec.smarthub.coop": {
      provider: "gladesec",
      name: "Glades Electric Cooperative",
    },
    "habershamemc.smarthub.coop": {
      provider: "habersham_emc",
      name: "Habersham EMC",
    },
    "harrisonrea.smarthub.coop": {
      provider: "hrea",
      name: "Harrison Rural Electrification Association (HREA)",
    },
    "haywoodemc.smarthub.coop": {
      provider: "haywood_emc",
      name: "Haywood EMC",
    },
    "hemc.smarthub.coop": {
      provider: "halifax_emc",
      name: "Halifax Electric Membership Corporation",
    },
    "hull.smarthub.coop": {
      provider: "hull",
      name: "Hull Municipal Lighting Plant",
    },
    "jacksonemc.smarthub.coop": {
      provider: "jackson_emc",
      name: "Jackson EMC",
    },
    "klpd.smarthub.coop": {
      provider: "klpd",
      name: "Kennebunk Light & Power District",
    },
    "lcec.smarthub.coop": {
      provider: "lcec",
      name: "Lee County Electric Cooperative (LCEC)",
    },
    "ludlow.smarthub.coop": {
      provider: "ludlow",
      name: "Village of Ludlow Electric Light Department",
    },
    "lumbeeriver.smarthub.coop": {
      provider: "lumbee_river_emc",
      name: "Lumbee River EMC",
    },
    "lynchesriver.smarthub.coop": {
      provider: "lynches_river",
      name: "Lynches River Electric Cooperative",
    },
    "meckelec.smarthub.coop": {
      provider: "meckelec",
      name: "Mecklenburg Electric Cooperative",
    },
    "myrec.smarthub.coop": {
      provider: "myrec",
      name: "Rappahannock Electric Cooperative (REC)",
    },
    "newenterpriserec.smarthub.coop": {
      provider: "new_enterprise_rec",
      name: "New Enterprise Rural Electric Cooperative",
    },
    "nhec.smarthub.coop": {
      provider: "nhec",
      name: "New Hampshire Electric Cooperative",
    },
    "nnec.smarthub.coop": {
      provider: "nnec",
      name: "Northern Neck Electric Cooperative (NNEC)",
    },
    "northwesternrec.smarthub.coop": {
      provider: "northwestern_rec",
      name: "Northwestern Rural Electric Cooperative",
    },
    "novec.smarthub.coop": {
      provider: "novec",
      name: "Northern Virginia Electric Cooperative (NOVEC)",
    },
    "pec.smarthub.coop": {
      provider: "palmetto",
      name: "Palmetto Electric Cooperative",
    },
    "peedeeelectric.smarthub.coop": {
      provider: "pee_dee",
      name: "Pee Dee Electric Cooperative",
    },
    "pemc.smarthub.coop": {
      provider: "piedmont_emc",
      name: "Piedmont Electric Membership Corporation",
    },
    "pgec.smarthub.coop": {
      provider: "pgec",
      name: "Prince George Electric Cooperative",
    },
    "planters.smarthub.coop": {
      provider: "planters_emc",
      name: "Planters EMC",
    },
    "preco.smarthub.coop": {
      provider: "preco",
      name: "Peace River Electric Cooperative (PRECO)",
    },
    "reaenergy.smarthub.coop": {
      provider: "rea_energy",
      name: "REA Energy Cooperative",
    },
    "santee.smarthub.coop": {
      provider: "santee",
      name: "Santee Electric Cooperative",
    },
    "sawnee.smarthub.coop": {
      provider: "sawnee_emc",
      name: "Sawnee EMC",
    },
    "sec.smarthub.coop": {
      provider: "sec",
      name: "Southside Electric Cooperative",
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
    "svec.smarthub.coop": {
      provider: "svec",
      name: "Shenandoah Valley Electric Cooperative (SVEC)",
    },
    "syemc.smarthub.coop": {
      provider: "surry_yadkin_emc",
      name: "Surry-Yadkin EMC",
    },
    "tcec.smarthub.coop": {
      provider: "tcec",
      name: "Tri-County Electric Cooperative",
    },
    "tec.smarthub.coop": {
      provider: "tec",
      name: "Talquin Electric Cooperative",
    },
    "tricountycoop.smarthub.coop": {
      provider: "tri_county",
      name: "Tri-County Electric Cooperative",
    },
    "tricountyrec.smarthub.coop": {
      provider: "tri_county_rec",
      name: "Tri-County Rural Electric Cooperative",
    },
    "tristate.smarthub.coop": {
      provider: "tri_state_emc",
      name: "Tri-State EMC",
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
    "washingtonemc.smarthub.coop": {
      provider: "washington_emc",
      name: "Washington EMC",
    },
    "wemc.smarthub.coop": {
      provider: "wake_emc",
      name: "Wake Electric Membership Corporation",
    },
    "wrec.smarthub.coop": {
      provider: "wrec",
      name: "Withlacoochee River Electric Cooperative (WREC)",
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
