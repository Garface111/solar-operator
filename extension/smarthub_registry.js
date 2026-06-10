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
    "adams.smarthub.coop": {
      provider: "adams",
      name: "Adams Electric Cooperative",
    },
    "adamsec.smarthub.coop": {
      provider: "adams_ec",
      name: "Adams Electric Cooperative",
    },
    "aec.smarthub.coop": {
      provider: "aiken",
      name: "Aiken Electric Cooperative",
    },
    "algerdelta.smarthub.coop": {
      provider: "algerdelta",
      name: "Alger Delta Cooperative Electric Association",
    },
    "arkvalley.smarthub.coop": {
      provider: "arkvalley",
      name: "Arkansas Valley Electric Cooperative",
    },
    "ashleychicot.smarthub.coop": {
      provider: "ashleychicot",
      name: "Ashley-Chicot Electric Cooperative",
    },
    "baldwinemc.smarthub.coop": {
      provider: "baldwinemc",
      name: "Baldwin EMC",
    },
    "barcelectric.smarthub.coop": {
      provider: "barc",
      name: "BARC Electric Cooperative",
    },
    "barronelectric.smarthub.coop": {
      provider: "barron",
      name: "Barron Electric Cooperative",
    },
    "bartonelectric.smarthub.coop": {
      provider: "barton",
      name: "Village of Barton (Barton Village Electric)",
    },
    "bayfieldelectric.smarthub.coop": {
      provider: "bayfield",
      name: "Bayfield Electric Cooperative",
    },
    "bcremc.smarthub.coop": {
      provider: "bcremc",
      name: "Bartholomew County REMC",
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
    "bentonrea.smarthub.coop": {
      provider: "bentonrea",
      name: "Benton County Electric System (Benton REA)",
    },
    "bgmu.smarthub.coop": {
      provider: "bowling_green",
      name: "Bowling Green Municipal Utilities",
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
    "cecpower.smarthub.coop": {
      provider: "carroll",
      name: "Carroll Electric Cooperative",
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
    "clarkenergy.smarthub.coop": {
      provider: "clark_energy",
      name: "Clark Energy Cooperative",
    },
    "claverack.smarthub.coop": {
      provider: "claverack_rec",
      name: "Claverack Rural Electric Cooperative",
    },
    "claycountyelectric.smarthub.coop": {
      provider: "claycounty_ar",
      name: "Clay County Electric Cooperative",
    },
    "clayelectric.smarthub.coop": {
      provider: "clayelectric",
      name: "Clay Electric Cooperative",
    },
    "cloverland.smarthub.coop": {
      provider: "cloverland",
      name: "Cloverland Electric Cooperative",
    },
    "cmec.smarthub.coop": {
      provider: "clarke_washington",
      name: "Clarke-Washington EMC",
    },
    "coastal.smarthub.coop": {
      provider: "coastal_al",
      name: "Coastal Electric Cooperative (AL)",
    },
    "coastalelectric.smarthub.coop": {
      provider: "coastal_electric",
      name: "Coastal Electric Cooperative",
    },
    "coastelectric.smarthub.coop": {
      provider: "coast_electric",
      name: "Coast Electric Power Association",
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
    "consolidatedelectric.smarthub.coop": {
      provider: "oh_consolidated",
      name: "Consolidated Cooperative",
    },
    "cornbeltenergy.smarthub.coop": {
      provider: "cornbelt",
      name: "Corn Belt Energy Corporation",
    },
    "craigheadelectric.smarthub.coop": {
      provider: "craighead",
      name: "Craighead Electric Cooperative",
    },
    "cumberlandvalley.smarthub.coop": {
      provider: "cumberland_valley",
      name: "Cumberland Valley Electric",
    },
    "cve.smarthub.coop": {
      provider: "chippewa_valley",
      name: "Chippewa Valley Electric Cooperative",
    },
    "cvec.smarthub.coop": {
      provider: "cvec",
      name: "Central Virginia Electric Cooperative (CVEC)",
    },
    "cwremc.smarthub.coop": {
      provider: "cwremc",
      name: "Carroll White REMC",
    },
    "dce.smarthub.coop": {
      provider: "dce",
      name: "Delaware County Electric Cooperative",
    },
    "dcremc.smarthub.coop": {
      provider: "dcremc",
      name: "Decatur County REMC",
    },
    "decoop.smarthub.coop": {
      provider: "dec",
      name: "Delaware Electric Cooperative",
    },
    "demco.smarthub.coop": {
      provider: "demco",
      name: "Dixie Electric Membership Corporation (DEMCO)",
    },
    "dixieepa.smarthub.coop": {
      provider: "dixie_epa",
      name: "Dixie Electric Power Association",
    },
    "dmremc.smarthub.coop": {
      provider: "dmremc",
      name: "Daviess-Martin County REMC",
    },
    "duboisrec.smarthub.coop": {
      provider: "duboisrec",
      name: "Dubois REC",
    },
    "dunnenergy.smarthub.coop": {
      provider: "dunnenergy",
      name: "Dunn Energy Cooperative",
    },
    "ecec.smarthub.coop": {
      provider: "ecec",
      name: "Eau Claire Energy Cooperative",
    },
    "egyptian.smarthub.coop": {
      provider: "egyptian",
      name: "Egyptian Electric Cooperative Association",
    },
    "eiec.smarthub.coop": {
      provider: "eiec",
      name: "Eastern Illini Electric Cooperative",
    },
    "emec.smarthub.coop": {
      provider: "emec",
      name: "Eastern Maine Electric Cooperative",
    },
    "energyunited.smarthub.coop": {
      provider: "energy_united",
      name: "EnergyUnited",
    },
    "enerstar.smarthub.coop": {
      provider: "enerstar",
      name: "EnerStar Electric Cooperative",
    },
    "erec.smarthub.coop": {
      provider: "erec",
      name: "Escambia River Electric Cooperative (EREC)",
    },
    "farmers.smarthub.coop": {
      provider: "farmers",
      name: "Farmers Electric Cooperative",
    },
    "farmerselectric.smarthub.coop": {
      provider: "farmers_recc",
      name: "Farmers RECC",
    },
    "fcremc.smarthub.coop": {
      provider: "four_county_emc",
      name: "Four County EMC",
    },
    "firstelectric.smarthub.coop": {
      provider: "firstelectric",
      name: "First Electric Cooperative",
    },
    "fkec.smarthub.coop": {
      provider: "fkec",
      name: "Florida Keys Electric Cooperative (FKEC)",
    },
    "fmec.smarthub.coop": {
      provider: "fleming_mason",
      name: "Fleming-Mason Energy Cooperative",
    },
    "frontierpower.smarthub.coop": {
      provider: "oh_frontierpower",
      name: "Frontier Power Company",
    },
    "gcec.smarthub.coop": {
      provider: "gcec",
      name: "Gulf Coast Electric Cooperative (GCEC)",
    },
    "gladesec.smarthub.coop": {
      provider: "gladesec",
      name: "Glades Electric Cooperative",
    },
    "glenergy.smarthub.coop": {
      provider: "gle",
      name: "Great Lakes Energy Cooperative",
    },
    "habershamemc.smarthub.coop": {
      provider: "habersham_emc",
      name: "Habersham EMC",
    },
    "hamiltonelectric.smarthub.coop": {
      provider: "oh_hamilton",
      name: "Hamilton Municipal Electric",
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
    "hendrickspower.smarthub.coop": {
      provider: "hendrickspower",
      name: "Hendricks Power Cooperative",
    },
    "henrycountyremc.smarthub.coop": {
      provider: "henrycountyremc",
      name: "Henry County REMC",
    },
    "holston.smarthub.coop": {
      provider: "holston",
      name: "Holston Electric Cooperative",
    },
    "homeworks.smarthub.coop": {
      provider: "homeworks",
      name: "HomeWorks Tri-County Electric Cooperative",
    },
    "hull.smarthub.coop": {
      provider: "hull",
      name: "Hull Municipal Lighting Plant",
    },
    "intercountyenergy.smarthub.coop": {
      provider: "inter_county",
      name: "Inter-County Energy Cooperative",
    },
    "jacksonemc.smarthub.coop": {
      provider: "jackson_emc",
      name: "Jackson EMC",
    },
    "jacksonenergy.smarthub.coop": {
      provider: "jackson_energy",
      name: "Jackson Energy Cooperative",
    },
    "jacksonremc.smarthub.coop": {
      provider: "jacksonremc",
      name: "Jackson County REMC",
    },
    "jasper.smarthub.coop": {
      provider: "jasper",
      name: "Jasper Municipal Electric",
    },
    "jayremc.smarthub.coop": {
      provider: "jayremc",
      name: "Jay County REMC",
    },
    "jcremc.smarthub.coop": {
      provider: "jcremc",
      name: "Johnson County REMC",
    },
    "jrec.smarthub.coop": {
      provider: "jrec",
      name: "Jump River Electric Cooperative",
    },
    "kenergycorp.smarthub.coop": {
      provider: "kenergy",
      name: "Kenergy Corp.",
    },
    "klpd.smarthub.coop": {
      provider: "klpd",
      name: "Kennebunk Light & Power District",
    },
    "kvremc.smarthub.coop": {
      provider: "kvremc",
      name: "Kankakee Valley REMC",
    },
    "lcec.smarthub.coop": {
      provider: "lcec",
      name: "Lee County Electric Cooperative (LCEC)",
    },
    "lebanonutilities.smarthub.coop": {
      provider: "lebanon",
      name: "Lebanon Utilities",
    },
    "ludlow.smarthub.coop": {
      provider: "ludlow",
      name: "Village of Ludlow Electric Light Department",
    },
    "lumbeeriver.smarthub.coop": {
      provider: "lumbee_river_emc",
      name: "Lumbee River EMC",
    },
    "lvrecc.smarthub.coop": {
      provider: "licking_valley",
      name: "Licking Valley RECC",
    },
    "lynchesriver.smarthub.coop": {
      provider: "lynches_river",
      name: "Lynches River Electric Cooperative",
    },
    "maelectric.smarthub.coop": {
      provider: "maelectric",
      name: "Middle Alabama Electric Cooperative",
    },
    "magnoliaepa.smarthub.coop": {
      provider: "magnolia_epa",
      name: "Magnolia Electric Power Association",
    },
    "mcdonoughpower.smarthub.coop": {
      provider: "mcdonough",
      name: "McDonough Power Cooperative",
    },
    "mec.smarthub.coop": {
      provider: "mec",
      name: "Mountain Electric Cooperative",
    },
    "meckelec.smarthub.coop": {
      provider: "meckelec",
      name: "Mecklenburg Electric Cooperative",
    },
    "mtemc.smarthub.coop": {
      provider: "mtemc",
      name: "Middle Tennessee Electric Membership Corporation",
    },
    "mvec.smarthub.coop": {
      provider: "magoffin_valley",
      name: "Magoffin Valley Electric Cooperative",
    },
    "mwec.smarthub.coop": {
      provider: "mwec",
      name: "Midwestern Electric",
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
    "nobleremc.smarthub.coop": {
      provider: "nobleremc",
      name: "Noble REMC",
    },
    "nolinrecc.smarthub.coop": {
      provider: "nolin_recc",
      name: "Nolin RECC",
    },
    "norriselectric.smarthub.coop": {
      provider: "norris",
      name: "Norris Electric Cooperative",
    },
    "northcentralelectric.smarthub.coop": {
      provider: "northcentral_ec",
      name: "Northcentral Electric Cooperative",
    },
    "northwesternrec.smarthub.coop": {
      provider: "northwestern_rec",
      name: "Northwestern Rural Electric Cooperative",
    },
    "novec.smarthub.coop": {
      provider: "novec",
      name: "Northern Virginia Electric Cooperative (NOVEC)",
    },
    "ocontoelectric.smarthub.coop": {
      provider: "oconto",
      name: "Oconto Electric Cooperative",
    },
    "ontorea.smarthub.coop": {
      provider: "ontonagon",
      name: "Ontonagon County REA",
    },
    "ozarksecc.smarthub.coop": {
      provider: "ozarks",
      name: "Ozarks Electric Cooperative",
    },
    "pcemc.smarthub.coop": {
      provider: "pcemc",
      name: "Pointe Coupee Electric Membership Corporation",
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
    "pieg.smarthub.coop": {
      provider: "pieg",
      name: "Presque Isle Electric & Gas Co-op",
    },
    "piercepepin.smarthub.coop": {
      provider: "piercepepin",
      name: "Pierce Pepin Cooperative Services",
    },
    "pioneerec.smarthub.coop": {
      provider: "oh_pioneerec",
      name: "Pioneer Rural Electric Cooperative",
    },
    "pjecc.smarthub.coop": {
      provider: "pjecc",
      name: "Petit Jean Electric Cooperative",
    },
    "planters.smarthub.coop": {
      provider: "planters_emc",
      name: "Planters EMC",
    },
    "polkburnett.smarthub.coop": {
      provider: "polkburnett",
      name: "Polk-Burnett Electric Cooperative",
    },
    "ppec.smarthub.coop": {
      provider: "ppec",
      name: "Paulding Putnam Electric Cooperative",
    },
    "preco.smarthub.coop": {
      provider: "preco",
      name: "Peace River Electric Cooperative (PRECO)",
    },
    "priceelectric.smarthub.coop": {
      provider: "priceelectric",
      name: "Price Electric Cooperative",
    },
    "reaenergy.smarthub.coop": {
      provider: "rea_energy",
      name: "REA Energy Cooperative",
    },
    "reedsburgutility.smarthub.coop": {
      provider: "reedsburg",
      name: "Reedsburg Utility Commission",
    },
    "riverlandenergy.smarthub.coop": {
      provider: "riverlandenergy",
      name: "Riverland Energy Cooperative",
    },
    "rmec.smarthub.coop": {
      provider: "rmec",
      name: "Rich Mountain Electric Cooperative",
    },
    "rock.smarthub.coop": {
      provider: "rock",
      name: "Rock Energy Cooperative",
    },
    "rse.smarthub.coop": {
      provider: "rse",
      name: "RushShelby Energy",
    },
    "santee.smarthub.coop": {
      provider: "santee",
      name: "Santee Electric Cooperative",
    },
    "sawnee.smarthub.coop": {
      provider: "sawnee_emc",
      name: "Sawnee EMC",
    },
    "scaec.smarthub.coop": {
      provider: "scaec",
      name: "South Central Arkansas Electric Cooperative",
    },
    "sec.smarthub.coop": {
      provider: "sec",
      name: "Southside Electric Cooperative",
    },
    "seiec.smarthub.coop": {
      provider: "seiec",
      name: "SouthEastern Illinois Electric Cooperative",
    },
    "seiremc.smarthub.coop": {
      provider: "seiremc",
      name: "Southeastern Indiana REMC",
    },
    "shelbyelectric.smarthub.coop": {
      provider: "oh_shelby",
      name: "Shelby Municipal Electric",
    },
    "shelbyenergy.smarthub.coop": {
      provider: "shelby_energy",
      name: "Shelby Energy Cooperative",
    },
    "siec.smarthub.coop": {
      provider: "siec",
      name: "Southern Illinois Electric Cooperative",
    },
    "singingriver.smarthub.coop": {
      provider: "singing_river",
      name: "Singing River Electric Cooperative",
    },
    "sirec.smarthub.coop": {
      provider: "sirec",
      name: "Southern Indiana REC",
    },
    "skrecc.smarthub.coop": {
      provider: "south_kentucky",
      name: "South Kentucky RECC",
    },
    "slemco.smarthub.coop": {
      provider: "slemco",
      name: "South Louisiana Electric Membership Corporation (SLEMCO)",
    },
    "somersetrec.smarthub.coop": {
      provider: "somerset_rec",
      name: "Somerset Rural Electric Cooperative",
    },
    "southcentralpower.smarthub.coop": {
      provider: "oh_southcentral",
      name: "South Central Power Company",
    },
    "southwestepa.smarthub.coop": {
      provider: "southwest_epa",
      name: "Southwest Mississippi Electric Power Association",
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
    "swrea.smarthub.coop": {
      provider: "swrea",
      name: "Southwest Arkansas Electric Cooperative",
    },
    "syemc.smarthub.coop": {
      provider: "surry_yadkin_emc",
      name: "Surry-Yadkin EMC",
    },
    "taylorelectric.smarthub.coop": {
      provider: "taylor_electric",
      name: "Taylor County RECC",
    },
    "tcec.smarthub.coop": {
      provider: "tcec",
      name: "Tri-County Electric Cooperative",
    },
    "tcrecc.smarthub.coop": {
      provider: "tri_county_ky",
      name: "Tri-County Electric Membership Corporation (KY)",
    },
    "teammidwest.smarthub.coop": {
      provider: "midwest_energy",
      name: "Midwest Energy & Communications",
    },
    "tec.smarthub.coop": {
      provider: "tec",
      name: "Talquin Electric Cooperative",
    },
    "tecmi.smarthub.coop": {
      provider: "tecmi",
      name: "Thumb Electric Cooperative",
    },
    "theenergycoop.smarthub.coop": {
      provider: "oh_tec",
      name: "The Energy Cooperative",
    },
    "tipmont.smarthub.coop": {
      provider: "tipmont",
      name: "Tipmont REMC",
    },
    "tombigbee.smarthub.coop": {
      provider: "tombigbee",
      name: "Tombigbee Electric Cooperative",
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
    "tvec.smarthub.coop": {
      provider: "tvec_al",
      name: "Tennessee Valley Electric Cooperative (AL)",
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
    "vernonelectric.smarthub.coop": {
      provider: "vernonelectric",
      name: "Vernon Electric Cooperative",
    },
    "villageofenosburgfalls.smarthub.coop": {
      provider: "enosburg",
      name: "Village of Enosburg Falls",
    },
    "villageofhydepark.smarthub.coop": {
      provider: "hyde_park",
      name: "Village of Hyde Park",
    },
    "volunteer.smarthub.coop": {
      provider: "volunteer",
      name: "Volunteer Energy Cooperative",
    },
    "wabash.smarthub.coop": {
      provider: "wabash",
      name: "Wabash County REMC",
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
    "wrecc.smarthub.coop": {
      provider: "warren_recc",
      name: "Warren RECC",
    },
    "wwvremc.smarthub.coop": {
      provider: "wwvremc",
      name: "Whitewater Valley REMC",
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
