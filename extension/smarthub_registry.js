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
    "4riverselectric.smarthub.coop": {
      provider: "four_rivers",
      name: "4 Rivers Electric Cooperative",
    },
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
    "agralite.smarthub.coop": {
      provider: "agralite",
      name: "Agralite Electric Cooperative",
    },
    "ahec.smarthub.coop": {
      provider: "mo_ahec",
      name: "Atchison-Holt Electric Cooperative",
    },
    "alfalfaelectric.smarthub.coop": {
      provider: "alfalfa",
      name: "Alfalfa Electric Cooperative",
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
    "baelectric.smarthub.coop": {
      provider: "ba_electric",
      name: "Brown-Atchison Electric Cooperative",
    },
    "baldwinemc.smarthub.coop": {
      provider: "baldwinemc",
      name: "Baldwin EMC",
    },
    "bandera.smarthub.coop": {
      provider: "bandera",
      name: "Bandera Electric Cooperative",
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
    "beartoothelectric.smarthub.coop": {
      provider: "beartooth",
      name: "Beartooth Electric Cooperative",
    },
    "bedfordrec.smarthub.coop": {
      provider: "bedford_rec",
      name: "Bedford Rural Electric Cooperative",
    },
    "belmontlight.smarthub.coop": {
      provider: "belmont",
      name: "Belmont Municipal Light Department",
    },
    "beltramielectric.smarthub.coop": {
      provider: "beltrami",
      name: "Beltrami Electric Cooperative",
    },
    "bemc.smarthub.coop": {
      provider: "brunswick_emc",
      name: "Brunswick Electric Membership Corporation",
    },
    "benco.smarthub.coop": {
      provider: "benco",
      name: "BENCO Electric Cooperative",
    },
    "bentonpud.smarthub.coop": {
      provider: "benton_pud",
      name: "Benton County PUD",
    },
    "bentonrea.smarthub.coop": {
      provider: "bentonrea",
      name: "Benton County Electric System (Benton REA)",
    },
    "bgmu.smarthub.coop": {
      provider: "bowling_green",
      name: "Bowling Green Municipal Utilities",
    },
    "bhec.smarthub.coop": {
      provider: "bhec",
      name: "Black Hills Electric Cooperative",
    },
    "bigflatelectric.smarthub.coop": {
      provider: "bigflat",
      name: "Big Flat Electric Cooperative",
    },
    "bighorncounty.smarthub.coop": {
      provider: "bighorncounty",
      name: "Big Horn County Electric Cooperative",
    },
    "blachlylane.smarthub.coop": {
      provider: "blachlylane",
      name: "Blachly-Lane Electric Cooperative",
    },
    "blockisland.smarthub.coop": {
      provider: "block_island",
      name: "Block Island Power Company",
    },
    "bluebonnet.smarthub.coop": {
      provider: "bluebonnet",
      name: "Bluebonnet Electric Cooperative",
    },
    "bluestemelectric.smarthub.coop": {
      provider: "bluestem",
      name: "Bluestem Electric Cooperative",
    },
    "booneelectric.smarthub.coop": {
      provider: "mo_boone",
      name: "Boone Electric Cooperative",
    },
    "brec.smarthub.coop": {
      provider: "mo_brec",
      name: "Black River Electric Cooperative",
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
    "butlerrec.smarthub.coop": {
      provider: "butlercounty_rec",
      name: "Butler County Rural Electric Cooperative",
    },
    "butteelectric.smarthub.coop": {
      provider: "butte_electric",
      name: "Butte Electric Cooperative",
    },
    "bvea.smarthub.coop": {
      provider: "bvea",
      name: "Bridger Valley Electric Association",
    },
    "callawayelectric.smarthub.coop": {
      provider: "mo_callaway",
      name: "Callaway Electric Cooperative",
    },
    "canadianvalley.smarthub.coop": {
      provider: "canadianvalley",
      name: "Canadian Valley Electric Cooperative",
    },
    "capitalelec.smarthub.coop": {
      provider: "capitalelec",
      name: "Capital Electric Cooperative",
    },
    "carbonpower.smarthub.coop": {
      provider: "carbonpower",
      name: "Carbon Power & Light",
    },
    "cbec.smarthub.coop": {
      provider: "cbec",
      name: "Craig-Botetourt Electric Cooperative (CBEC)",
    },
    "cdec.smarthub.coop": {
      provider: "cdec",
      name: "Continental Divide Electric Cooperative",
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
    "centralec.smarthub.coop": {
      provider: "cec_sd",
      name: "Central Electric Cooperative",
    },
    "charlesmix.smarthub.coop": {
      provider: "charles_mix",
      name: "Charles Mix Electric Association",
    },
    "cherrytodd.smarthub.coop": {
      provider: "cherry_todd",
      name: "Cherry-Todd Electric Cooperative",
    },
    "choctawelectric.smarthub.coop": {
      provider: "choctaw",
      name: "Choctaw Electric Cooperative",
    },
    "choptankelectric.smarthub.coop": {
      provider: "choptank",
      name: "Choptank Electric Cooperative",
    },
    "cimarronelectric.smarthub.coop": {
      provider: "cimarron",
      name: "Cimarron Electric Cooperative",
    },
    "citizenselectric.smarthub.coop": {
      provider: "mo_citizens",
      name: "Citizens Electric Corporation",
    },
    "clallampud.smarthub.coop": {
      provider: "clallam_pud",
      name: "Clallam County PUD",
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
    "clearwaterpolk.smarthub.coop": {
      provider: "clearwater_polk",
      name: "Clearwater-Polk Electric Cooperative",
    },
    "clearwaterpower.smarthub.coop": {
      provider: "clearwater",
      name: "Clearwater Power Company",
    },
    "cloverland.smarthub.coop": {
      provider: "cloverland",
      name: "Cloverland Electric Cooperative",
    },
    "clpower.smarthub.coop": {
      provider: "clpa",
      name: "Cooperative Light & Power Association",
    },
    "clpud.smarthub.coop": {
      provider: "clpud",
      name: "Central Lincoln PUD",
    },
    "cmec.smarthub.coop": {
      provider: "clarke_washington",
      name: "Clarke-Washington EMC",
    },
    "cmecinc.smarthub.coop": {
      provider: "mo_cmec",
      name: "Central Missouri Electric Cooperative",
    },
    "cmselectric.smarthub.coop": {
      provider: "cms_electric",
      name: "CMS Electric Cooperative",
    },
    "cnmec.smarthub.coop": {
      provider: "cnmec",
      name: "Central New Mexico Electric Cooperative",
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
    "codingtonclarkelectric.smarthub.coop": {
      provider: "codington_clark",
      name: "Codington-Clark Electric Cooperative",
    },
    "columbiabasin.smarthub.coop": {
      provider: "columbia_basin",
      name: "Columbia Basin Electric Cooperative",
    },
    "columbiarea.smarthub.coop": {
      provider: "columbia_rea",
      name: "Columbia Rural Electric Association",
    },
    "columbuscoop.smarthub.coop": {
      provider: "columbuscoop",
      name: "Columbus Electric Cooperative",
    },
    "comelec.smarthub.coop": {
      provider: "comelec",
      name: "Community Electric Cooperative",
    },
    "como.smarthub.coop": {
      provider: "mo_como",
      name: "Co-Mo Electric Cooperative",
    },
    "concord.smarthub.coop": {
      provider: "concord",
      name: "Concord Municipal Light Plant",
    },
    "consolidatedelectric.smarthub.coop": {
      provider: "oh_consolidated",
      name: "Consolidated Cooperative",
    },
    "consumersenergy.smarthub.coop": {
      provider: "consumersenergy",
      name: "Consumers Energy (IA co-op)",
    },
    "cooksonhills.smarthub.coop": {
      provider: "cooksonhills",
      name: "Cookson Hills Electric Cooperative",
    },
    "cornbeltenergy.smarthub.coop": {
      provider: "cornbelt",
      name: "Corn Belt Energy Corporation",
    },
    "corridorenergy.smarthub.coop": {
      provider: "corridor",
      name: "Corridor Energy Cooperative (formerly Linn County REC)",
    },
    "coserv.smarthub.coop": {
      provider: "coserv",
      name: "CoServ Electric",
    },
    "cottonelectric.smarthub.coop": {
      provider: "cotton",
      name: "Cotton Electric Cooperative",
    },
    "cowlitzpud.smarthub.coop": {
      provider: "cowlitz_pud",
      name: "Cowlitz PUD",
    },
    "cpi.smarthub.coop": {
      provider: "cpi",
      name: "Consumers Power Inc.",
    },
    "craigheadelectric.smarthub.coop": {
      provider: "craighead",
      name: "Craighead Electric Cooperative",
    },
    "crawfordelec.smarthub.coop": {
      provider: "mo_crawford",
      name: "Crawford Electric Cooperative",
    },
    "crec.smarthub.coop": {
      provider: "crec",
      name: "Centennial Rural Electric Cooperative",
    },
    "crpud.smarthub.coop": {
      provider: "crpud",
      name: "Columbia River PUD",
    },
    "ctec.smarthub.coop": {
      provider: "ctec",
      name: "Central Texas Electric Cooperative",
    },
    "cumberlandvalley.smarthub.coop": {
      provider: "cumberland_valley",
      name: "Cumberland Valley Electric",
    },
    "custertel.smarthub.coop": {
      provider: "custertel",
      name: "Custer Telephone Cooperative (Electric Division)",
    },
    "cve.smarthub.coop": {
      provider: "chippewa_valley",
      name: "Chippewa Valley Electric Cooperative",
    },
    "cvea.smarthub.coop": {
      provider: "cvea",
      name: "Copper Valley Electric Association",
    },
    "cvec.smarthub.coop": {
      provider: "cvec",
      name: "Central Virginia Electric Cooperative (CVEC)",
    },
    "cwpower.smarthub.coop": {
      provider: "crow_wing",
      name: "Crow Wing Power",
    },
    "cwremc.smarthub.coop": {
      provider: "cwremc",
      name: "Carroll White REMC",
    },
    "dakotaelectric.smarthub.coop": {
      provider: "dakota",
      name: "Dakota Electric Association",
    },
    "dakotaenergy.smarthub.coop": {
      provider: "dakota_energy",
      name: "Dakota Energy Cooperative",
    },
    "dakotavalley.smarthub.coop": {
      provider: "dakotavalley",
      name: "Dakota Valley Electric Cooperative",
    },
    "dawsonpower.smarthub.coop": {
      provider: "dawson_ppd",
      name: "Dawson Public Power District",
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
    "deepeast.smarthub.coop": {
      provider: "deepeast",
      name: "Deep East Texas Electric Cooperative",
    },
    "demco.smarthub.coop": {
      provider: "demco",
      name: "Dixie Electric Membership Corporation (DEMCO)",
    },
    "dixieepa.smarthub.coop": {
      provider: "dixie_epa",
      name: "Dixie Electric Power Association",
    },
    "dixiepower.smarthub.coop": {
      provider: "dixie_power",
      name: "Dixie Power (Dixie Escalante REC)",
    },
    "dmea.smarthub.coop": {
      provider: "dmea",
      name: "Delta-Montrose Electric Association",
    },
    "dmremc.smarthub.coop": {
      provider: "dmremc",
      name: "Daviess-Martin County REMC",
    },
    "donrec.smarthub.coop": {
      provider: "doniphan",
      name: "Doniphan Electric Cooperative",
    },
    "douglaselectric.smarthub.coop": {
      provider: "douglas_sd",
      name: "Douglas Electric Cooperative",
    },
    "douglaspud.smarthub.coop": {
      provider: "douglas_pud",
      name: "Douglas County PUD",
    },
    "dsoelectric.smarthub.coop": {
      provider: "dso_electric",
      name: "DSO Electric Cooperative",
    },
    "duboisrec.smarthub.coop": {
      provider: "duboisrec",
      name: "Dubois REC",
    },
    "dunnenergy.smarthub.coop": {
      provider: "dunnenergy",
      name: "Dunn Energy Cooperative",
    },
    "easterniowa.smarthub.coop": {
      provider: "easterniowa_lp",
      name: "Eastern Iowa Light & Power Cooperative",
    },
    "ecec.smarthub.coop": {
      provider: "ecec",
      name: "Eau Claire Energy Cooperative",
    },
    "ecemn.smarthub.coop": {
      provider: "east_central",
      name: "East Central Energy",
    },
    "ecirec.smarthub.coop": {
      provider: "eastcentral_ia",
      name: "East-Central Iowa Rural Electric Cooperative",
    },
    "eea.smarthub.coop": {
      provider: "eea",
      name: "Empire Electric Association",
    },
    "egyptian.smarthub.coop": {
      provider: "egyptian",
      name: "Egyptian Electric Cooperative Association",
    },
    "eiec.smarthub.coop": {
      provider: "eiec",
      name: "Eastern Illini Electric Cooperative",
    },
    "elmhurstmutual.smarthub.coop": {
      provider: "elmhurst_mutual",
      name: "Elmhurst Mutual Power & Light",
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
    "epud.smarthub.coop": {
      provider: "epud",
      name: "Emerald People's Utility District",
    },
    "erec.smarthub.coop": {
      provider: "erec",
      name: "Escambia River Electric Cooperative (EREC)",
    },
    "fallriverelectric.smarthub.coop": {
      provider: "fallriver",
      name: "Fall River Rural Electric Cooperative",
    },
    "farmers.smarthub.coop": {
      provider: "farmers",
      name: "Farmers Electric Cooperative",
    },
    "farmerselectric.smarthub.coop": {
      provider: "farmers_recc",
      name: "Farmers RECC",
    },
    "farmersrec.smarthub.coop": {
      provider: "farmers_rec",
      name: "Farmers Electric Cooperative (multiple IA locations)",
    },
    "fayette.smarthub.coop": {
      provider: "fayette",
      name: "Fayette Electric Cooperative",
    },
    "fcec.smarthub.coop": {
      provider: "fannin",
      name: "Fannin County Electric Cooperative",
    },
    "fcremc.smarthub.coop": {
      provider: "four_county_emc",
      name: "Four County EMC",
    },
    "femelectric.smarthub.coop": {
      provider: "fem",
      name: "FEM Electric Association",
    },
    "ferguselectric.smarthub.coop": {
      provider: "fergus",
      name: "Fergus Electric Cooperative",
    },
    "firstelectric.smarthub.coop": {
      provider: "firstelectric",
      name: "First Electric Cooperative",
    },
    "fkec.smarthub.coop": {
      provider: "fkec",
      name: "Florida Keys Electric Cooperative (FKEC)",
    },
    "flatheadelectric.smarthub.coop": {
      provider: "flathead",
      name: "Flathead Electric Cooperative",
    },
    "flinthillsrec.smarthub.coop": {
      provider: "flint_hills",
      name: "Flint Hills Rural Electric Cooperative",
    },
    "fmec.smarthub.coop": {
      provider: "fleming_mason",
      name: "Fleming-Mason Energy Cooperative",
    },
    "franklinpud.smarthub.coop": {
      provider: "franklin_pud",
      name: "Franklin PUD",
    },
    "franklinrec.smarthub.coop": {
      provider: "franklin_rec",
      name: "Franklin Rural Electric Cooperative",
    },
    "frea.smarthub.coop": {
      provider: "federated",
      name: "Federated Rural Electric Association",
    },
    "freestate.smarthub.coop": {
      provider: "freestate",
      name: "FreeState Electric Cooperative",
    },
    "frontierpower.smarthub.coop": {
      provider: "oh_frontierpower",
      name: "Frontier Power Company",
    },
    "garkaneenergy.smarthub.coop": {
      provider: "garkane",
      name: "Garkane Energy Cooperative",
    },
    "garlandlightpower.smarthub.coop": {
      provider: "garlandpower",
      name: "Garland Light & Power Company",
    },
    "gascosage.smarthub.coop": {
      provider: "mo_gascosage",
      name: "Gascosage Electric Cooperative",
    },
    "gccea.smarthub.coop": {
      provider: "goodhue",
      name: "Goodhue County Cooperative Electric Association",
    },
    "gcea.smarthub.coop": {
      provider: "gcea",
      name: "Gunnison County Electric Association",
    },
    "gcec.smarthub.coop": {
      provider: "gcec",
      name: "Gulf Coast Electric Cooperative (GCEC)",
    },
    "ghpud.smarthub.coop": {
      provider: "grays_harbor_pud",
      name: "Grays Harbor PUD",
    },
    "glacierelectric.smarthub.coop": {
      provider: "glacier",
      name: "Glacier Electric Cooperative",
    },
    "gladesec.smarthub.coop": {
      provider: "gladesec",
      name: "Glades Electric Cooperative",
    },
    "glenergy.smarthub.coop": {
      provider: "gle",
      name: "Great Lakes Energy Cooperative",
    },
    "goldenwest.smarthub.coop": {
      provider: "goldenwest",
      name: "Goldenwest Electric Cooperative",
    },
    "grandelectric.smarthub.coop": {
      provider: "grand_electric",
      name: "Grand Electric Cooperative",
    },
    "graysoncollin.smarthub.coop": {
      provider: "grayson_collin",
      name: "Grayson-Collin Electric Cooperative",
    },
    "gridley.smarthub.coop": {
      provider: "gridley",
      name: "Gridley Electric Utility",
    },
    "gvea.smarthub.coop": {
      provider: "gvea",
      name: "Golden Valley Electric Association",
    },
    "gvec.smarthub.coop": {
      provider: "gvec",
      name: "Guadalupe Valley Electric Cooperative",
    },
    "gvp.smarthub.coop": {
      provider: "gvp",
      name: "Grand Valley Power",
    },
    "habershamemc.smarthub.coop": {
      provider: "habersham_emc",
      name: "Habersham EMC",
    },
    "hamiltonelectric.smarthub.coop": {
      provider: "oh_hamilton",
      name: "Hamilton Municipal Electric",
    },
    "harmonelectric.smarthub.coop": {
      provider: "harmon",
      name: "Harmon Electric Cooperative",
    },
    "harneyelectric.smarthub.coop": {
      provider: "harney",
      name: "Harney Electric Cooperative",
    },
    "harrisonrea.smarthub.coop": {
      provider: "hrea",
      name: "Harrison Rural Electrification Association (HREA)",
    },
    "haywoodemc.smarthub.coop": {
      provider: "haywood_emc",
      name: "Haywood EMC",
    },
    "hcelectric.smarthub.coop": {
      provider: "hcelectric",
      name: "Hill County Electric Cooperative",
    },
    "hcrec.smarthub.coop": {
      provider: "harrison_rec",
      name: "Harrison County Rural Electric Cooperative",
    },
    "hdelectric.smarthub.coop": {
      provider: "hd_electric",
      name: "H-D Electric Cooperative",
    },
    "heartlandpower.smarthub.coop": {
      provider: "heartland_power",
      name: "Heartland Power Cooperative",
    },
    "heartlandrec.smarthub.coop": {
      provider: "heartland",
      name: "Heartland Rural Electric Cooperative",
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
    "highplainspower.smarthub.coop": {
      provider: "highplainspower",
      name: "High Plains Power",
    },
    "highwestenergy.smarthub.coop": {
      provider: "highwestenergy",
      name: "High West Energy",
    },
    "hilco.smarthub.coop": {
      provider: "hilco",
      name: "Hilco Electric Cooperative",
    },
    "hoec.smarthub.coop": {
      provider: "mo_hoec",
      name: "Howell-Oregon Electric Cooperative",
    },
    "holston.smarthub.coop": {
      provider: "holston",
      name: "Holston Electric Cooperative",
    },
    "holycross.smarthub.coop": {
      provider: "holycross",
      name: "Holy Cross Energy",
    },
    "homeworks.smarthub.coop": {
      provider: "homeworks",
      name: "HomeWorks Tri-County Electric Cooperative",
    },
    "hotec.smarthub.coop": {
      provider: "hotec",
      name: "Heart of Texas Electric Cooperative",
    },
    "howardelectric.smarthub.coop": {
      provider: "mo_howard",
      name: "Howard Electric Cooperative",
    },
    "hrec.smarthub.coop": {
      provider: "hood_river",
      name: "Hood River Electric & Internet Co-op",
    },
    "hull.smarthub.coop": {
      provider: "hull",
      name: "Hull Municipal Lighting Plant",
    },
    "ieca.smarthub.coop": {
      provider: "mo_ieca",
      name: "Intercounty Electric Cooperative",
    },
    "ilec.smarthub.coop": {
      provider: "iowalakes",
      name: "Iowa Lakes Electric Cooperative",
    },
    "inlandpower.smarthub.coop": {
      provider: "inland_power",
      name: "Inland Power & Light",
    },
    "insidepassageelectric.smarthub.coop": {
      provider: "ipec",
      name: "Inside Passage Electric Cooperative",
    },
    "intercountyenergy.smarthub.coop": {
      provider: "inter_county",
      name: "Inter-County Energy Cooperative",
    },
    "itascamantrap.smarthub.coop": {
      provider: "itasca_mantrap",
      name: "Itasca-Mantrap Cooperative Electrical Association",
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
    "karnesec.smarthub.coop": {
      provider: "karnes",
      name: "Karnes Electric Cooperative",
    },
    "kayelectric.smarthub.coop": {
      provider: "kay",
      name: "Kay Electric Cooperative",
    },
    "kcelectric.smarthub.coop": {
      provider: "kcelectric",
      name: "K.C. Electric Association",
    },
    "kec.smarthub.coop": {
      provider: "kec",
      name: "Kootenai Electric Cooperative",
    },
    "kecsd.smarthub.coop": {
      provider: "kingsbury",
      name: "Kingsbury Electric Cooperative",
    },
    "kemelectric.smarthub.coop": {
      provider: "kemelectric",
      name: "KEM Electric Cooperative",
    },
    "kenergycorp.smarthub.coop": {
      provider: "kenergy",
      name: "Kenergy Corp.",
    },
    "kiamichielectric.smarthub.coop": {
      provider: "kiamichi",
      name: "Kiamichi Electric Cooperative",
    },
    "kiuc.smarthub.coop": {
      provider: "kiuc",
      name: "Kauai Island Utility Cooperative (KIUC)",
    },
    "klickitatpud.smarthub.coop": {
      provider: "klickitat_pud",
      name: "Klickitat PUD",
    },
    "klpd.smarthub.coop": {
      provider: "klpd",
      name: "Kennebunk Light & Power District",
    },
    "kodiakelectric.smarthub.coop": {
      provider: "kea",
      name: "Kodiak Electric Association",
    },
    "kvremc.smarthub.coop": {
      provider: "kvremc",
      name: "Kankakee Valley REMC",
    },
    "kwh.smarthub.coop": {
      provider: "kwh",
      name: "Cass County Electric Cooperative",
    },
    "lacledeelectric.smarthub.coop": {
      provider: "mo_laclede",
      name: "Laclede Electric Cooperative",
    },
    "lacreek.smarthub.coop": {
      provider: "lacreek",
      name: "Lacreek Electric Association",
    },
    "lakecountrypower.smarthub.coop": {
      provider: "lake_country",
      name: "Lake Country Power",
    },
    "lakeregion.smarthub.coop": {
      provider: "lake_region",
      name: "Lake Region Electric Cooperative",
    },
    "lakeviewlight.smarthub.coop": {
      provider: "lakeview_light",
      name: "Lakeview Light & Power",
    },
    "laneelectric.smarthub.coop": {
      provider: "lane_electric",
      name: "Lane Electric Cooperative",
    },
    "lanescott.smarthub.coop": {
      provider: "lane_scott",
      name: "Lane-Scott Electric Cooperative",
    },
    "lcec.smarthub.coop": {
      provider: "lcec",
      name: "Lee County Electric Cooperative (LCEC)",
    },
    "lcpd1.smarthub.coop": {
      provider: "lcpd",
      name: "Lincoln County Power District No. 1",
    },
    "leacountyelectric.smarthub.coop": {
      provider: "nm_lcec",
      name: "Lea County Electric Cooperative (NM)",
    },
    "lebanonutilities.smarthub.coop": {
      provider: "lebanon",
      name: "Lebanon Utilities",
    },
    "lewiscountyrec.smarthub.coop": {
      provider: "mo_lewis_county",
      name: "Lewis County Rural Electric Cooperative",
    },
    "lighthouse.smarthub.coop": {
      provider: "lighthouse",
      name: "Lighthouse Electric Cooperative",
    },
    "lincolnelectric.smarthub.coop": {
      provider: "lincoln",
      name: "Lincoln Electric Cooperative",
    },
    "llec.smarthub.coop": {
      provider: "lyon_lincoln",
      name: "Lyon-Lincoln Electric Cooperative",
    },
    "lpea.smarthub.coop": {
      provider: "lpea",
      name: "La Plata Electric Association",
    },
    "lrec.smarthub.coop": {
      provider: "lrec",
      name: "Lake Region Electric Association",
    },
    "ludlow.smarthub.coop": {
      provider: "ludlow",
      name: "Village of Ludlow Electric Light Department",
    },
    "lumbeeriver.smarthub.coop": {
      provider: "lumbee_river_emc",
      name: "Lumbee River EMC",
    },
    "lvenergy.smarthub.coop": {
      provider: "lvenergy",
      name: "Lower Valley Energy",
    },
    "lvrecc.smarthub.coop": {
      provider: "licking_valley",
      name: "Licking Valley RECC",
    },
    "lynchesriver.smarthub.coop": {
      provider: "lynches_river",
      name: "Lynches River Electric Cooperative",
    },
    "lyntegar.smarthub.coop": {
      provider: "lyntegar",
      name: "Lyntegar Electric Cooperative",
    },
    "lyrec.smarthub.coop": {
      provider: "lyon_rec",
      name: "Lyon Rural Electric Cooperative",
    },
    "maelectric.smarthub.coop": {
      provider: "maelectric",
      name: "Middle Alabama Electric Cooperative",
    },
    "magnoliaepa.smarthub.coop": {
      provider: "magnolia_epa",
      name: "Magnolia Electric Power Association",
    },
    "mariasriverec.smarthub.coop": {
      provider: "mariasriver",
      name: "Marias River Electric Cooperative",
    },
    "masonpud1.smarthub.coop": {
      provider: "mason_pud_1",
      name: "Mason County PUD 1",
    },
    "masonpud3.smarthub.coop": {
      provider: "mason_pud_3",
      name: "Mason County PUD 3",
    },
    "mcconeelectric.smarthub.coop": {
      provider: "mccone",
      name: "McCone Electric Cooperative",
    },
    "mcdonoughpower.smarthub.coop": {
      provider: "mcdonough",
      name: "McDonough Power Cooperative",
    },
    "mckenzieelectric.smarthub.coop": {
      provider: "mckenzieelectric",
      name: "McKenzie Electric Cooperative",
    },
    "mcleanelectric.smarthub.coop": {
      provider: "mcleanelectric",
      name: "McLean Electric Cooperative",
    },
    "mcrea.smarthub.coop": {
      provider: "mcrea",
      name: "Morgan County Rural Electric Association",
    },
    "mea.smarthub.coop": {
      provider: "mea",
      name: "Matanuska Electric Association",
    },
    "mec.smarthub.coop": {
      provider: "mec",
      name: "Mountain Electric Cooperative",
    },
    "meckelec.smarthub.coop": {
      provider: "meckelec",
      name: "Mecklenburg Electric Cooperative",
    },
    "meeker.smarthub.coop": {
      provider: "meeker",
      name: "Meeker Cooperative",
    },
    "midlandpower.smarthub.coop": {
      provider: "midland_power",
      name: "Midland Power Cooperative",
    },
    "midstateelectric.smarthub.coop": {
      provider: "midstate",
      name: "Midstate Electric Cooperative",
    },
    "mienergy.smarthub.coop": {
      provider: "mienergy",
      name: "MiEnergy Cooperative",
    },
    "missionvalleypower.smarthub.coop": {
      provider: "mvp",
      name: "Mission Valley Power",
    },
    "mlea.smarthub.coop": {
      provider: "mlea",
      name: "Moon Lake Electric Association",
    },
    "mohaveelectric.smarthub.coop": {
      provider: "mohave",
      name: "Mohave Electric Cooperative",
    },
    "morec.smarthub.coop": {
      provider: "mo_rural",
      name: "Missouri Rural Electric Cooperative",
    },
    "morgransou.smarthub.coop": {
      provider: "morgransou",
      name: "Mor-Gran-Sou Electric Cooperative",
    },
    "mtemc.smarthub.coop": {
      provider: "mtemc",
      name: "Middle Tennessee Electric Membership Corporation",
    },
    "mvea.smarthub.coop": {
      provider: "mvea",
      name: "Mountain View Electric Association",
    },
    "mvec.smarthub.coop": {
      provider: "magoffin_valley",
      name: "Magoffin Valley Electric Cooperative",
    },
    "mwec.smarthub.coop": {
      provider: "mwec",
      name: "Midwestern Electric",
    },
    "mwpower.smarthub.coop": {
      provider: "mwpower",
      name: "Mt. Wheeler Power",
    },
    "myec.smarthub.coop": {
      provider: "myec",
      name: "Mid-Yellowstone Electric Cooperative",
    },
    "myrec.smarthub.coop": {
      provider: "myrec",
      name: "Rappahannock Electric Cooperative (REC)",
    },
    "navopache.smarthub.coop": {
      provider: "navopache",
      name: "Navopache Electric Cooperative",
    },
    "nceci.smarthub.coop": {
      provider: "nceci",
      name: "North Central Electric Cooperative",
    },
    "nemahamarshall.smarthub.coop": {
      provider: "nemaha_marshall",
      name: "Nemaha-Marshall Electric Cooperative",
    },
    "newenterpriserec.smarthub.coop": {
      provider: "new_enterprise_rec",
      name: "New Enterprise Rural Electric Cooperative",
    },
    "newmac.smarthub.coop": {
      provider: "mo_newmac",
      name: "New-Mac Electric Cooperative",
    },
    "nhec.smarthub.coop": {
      provider: "nhec",
      name: "New Hampshire Electric Cooperative",
    },
    "ninnescah.smarthub.coop": {
      provider: "ninnescah",
      name: "Ninnescah Electric Cooperative",
    },
    "niobraraelectric.smarthub.coop": {
      provider: "niobrara_ea",
      name: "Niobrara Electric Association",
    },
    "nli.smarthub.coop": {
      provider: "nli",
      name: "Northern Lights Inc.",
    },
    "nnec.smarthub.coop": {
      provider: "nnec",
      name: "Northern Neck Electric Cooperative (NNEC)",
    },
    "nobleremc.smarthub.coop": {
      provider: "nobleremc",
      name: "Noble REMC",
    },
    "noblesce.smarthub.coop": {
      provider: "nobles",
      name: "Nobles Cooperative Electric",
    },
    "nodakelectric.smarthub.coop": {
      provider: "nodakelectric",
      name: "Nodak Electric Cooperative",
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
    "northernelectric.smarthub.coop": {
      provider: "northern_electric",
      name: "Northern Electric Cooperative",
    },
    "northstarelectric.smarthub.coop": {
      provider: "north_star",
      name: "North Star Electric Cooperative",
    },
    "northwesternrec.smarthub.coop": {
      provider: "northwestern_rec",
      name: "Northwestern Rural Electric Cooperative",
    },
    "norval.smarthub.coop": {
      provider: "norval",
      name: "NorVal Electric Cooperative",
    },
    "novec.smarthub.coop": {
      provider: "novec",
      name: "Northern Virginia Electric Cooperative (NOVEC)",
    },
    "nplains.smarthub.coop": {
      provider: "nplains",
      name: "Northern Plains Electric Cooperative",
    },
    "ntec.smarthub.coop": {
      provider: "nortex",
      name: "Nortex Electric Cooperative",
    },
    "nushagakelectric.smarthub.coop": {
      provider: "netc",
      name: "Nushagak Electric & Telephone Cooperative",
    },
    "nvrec.smarthub.coop": {
      provider: "nishnabotna_rec",
      name: "Nishnabotna Valley Rural Electric Cooperative",
    },
    "nwec.smarthub.coop": {
      provider: "nwec",
      name: "Northwestern Electric Cooperative",
    },
    "nwelectric.smarthub.coop": {
      provider: "nw_electric",
      name: "Northwest Electric Cooperative",
    },
    "nwrec.smarthub.coop": {
      provider: "northwest_rec",
      name: "North West Rural Electric Cooperative",
    },
    "oaheelectric.smarthub.coop": {
      provider: "oahe",
      name: "Oahe Electric Cooperative",
    },
    "ocontoelectric.smarthub.coop": {
      provider: "oconto",
      name: "Oconto Electric Cooperative",
    },
    "okanoganpud.smarthub.coop": {
      provider: "okanogan_pud",
      name: "Okanogan County PUD",
    },
    "okcoop.smarthub.coop": {
      provider: "okcoop",
      name: "Oklahoma Electric Cooperative",
    },
    "ontorea.smarthub.coop": {
      provider: "ontonagon",
      name: "Ontonagon County REA",
    },
    "opalco.smarthub.coop": {
      provider: "opalco",
      name: "OPALCO (Orcas Power & Light Cooperative)",
    },
    "osagevalley.smarthub.coop": {
      provider: "mo_osage_valley",
      name: "Osage Valley Electric Cooperative",
    },
    "otero.smarthub.coop": {
      provider: "otero",
      name: "Otero County Electric Cooperative",
    },
    "ozarkborder.smarthub.coop": {
      provider: "mo_ozark_border",
      name: "Ozark Border Electric Cooperative",
    },
    "ozarkelectric.smarthub.coop": {
      provider: "mo_ozark",
      name: "Ozark Electric Cooperative",
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
    "pellacea.smarthub.coop": {
      provider: "pella_coop",
      name: "Pella Cooperative Electric Association",
    },
    "pemc.smarthub.coop": {
      provider: "piedmont_emc",
      name: "Piedmont Electric Membership Corporation",
    },
    "peopleselectric.smarthub.coop": {
      provider: "peopleselectric",
      name: "People's Electric Cooperative",
    },
    "peoplesrec.smarthub.coop": {
      provider: "peoples_energy",
      name: "People's Energy Cooperative",
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
    "pkmcoop.smarthub.coop": {
      provider: "pkm",
      name: "PKM Electric Cooperative",
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
    "prairieenergy.smarthub.coop": {
      provider: "prairie",
      name: "Prairie Energy Cooperative",
    },
    "prairielandelectric.smarthub.coop": {
      provider: "prairie_land",
      name: "Prairie Land Electric Cooperative",
    },
    "preco.smarthub.coop": {
      provider: "preco",
      name: "Peace River Electric Cooperative (PRECO)",
    },
    "prema.smarthub.coop": {
      provider: "prema",
      name: "Panhandle Rural Electric Membership Association",
    },
    "priceelectric.smarthub.coop": {
      provider: "priceelectric",
      name: "Price Electric Cooperative",
    },
    "psrec.smarthub.coop": {
      provider: "psrec",
      name: "Plumas-Sierra Rural Electric Cooperative",
    },
    "pvrea.smarthub.coop": {
      provider: "pvrea",
      name: "Poudre Valley Rural Electric Association",
    },
    "rallscountyelectric.smarthub.coop": {
      provider: "mo_ralls_county",
      name: "Ralls County Electric Cooperative",
    },
    "ravallielectric.smarthub.coop": {
      provider: "ravalli",
      name: "Ravalli Electric Cooperative",
    },
    "rcec.smarthub.coop": {
      provider: "rcec",
      name: "Roosevelt County Electric Cooperative",
    },
    "reaenergy.smarthub.coop": {
      provider: "rea_energy",
      name: "REA Energy Cooperative",
    },
    "redlakeelectric.smarthub.coop": {
      provider: "red_lake",
      name: "Red Lake Electric Cooperative",
    },
    "redwoodelectric.smarthub.coop": {
      provider: "redwood",
      name: "Redwood Electric Cooperative",
    },
    "reedsburgutility.smarthub.coop": {
      provider: "reedsburg",
      name: "Reedsburg Utility Commission",
    },
    "rgec.smarthub.coop": {
      provider: "rgec",
      name: "Rio Grande Electric Cooperative",
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
    "rollinghills.smarthub.coop": {
      provider: "rolling_hills",
      name: "Rolling Hills Electric Cooperative",
    },
    "roseauelectric.smarthub.coop": {
      provider: "roseau",
      name: "Roseau Electric Cooperative",
    },
    "roughriderelectric.smarthub.coop": {
      provider: "roughriderelectric",
      name: "Roughrider Electric Cooperative",
    },
    "rrelectric.smarthub.coop": {
      provider: "rrelectric",
      name: "Raft River Rural Electric Co-op",
    },
    "rrvcoop.smarthub.coop": {
      provider: "red_river",
      name: "Red River Valley Cooperative Power Association",
    },
    "rrvrea.smarthub.coop": {
      provider: "rrvrea",
      name: "Red River Valley Electric Cooperative",
    },
    "rscpa.smarthub.coop": {
      provider: "renville_sibley",
      name: "Renville-Sibley Cooperative Power Association",
    },
    "rse.smarthub.coop": {
      provider: "rse",
      name: "RushShelby Energy",
    },
    "runestoneelectric.smarthub.coop": {
      provider: "runestone",
      name: "Runestone Electric Association",
    },
    "rvec.smarthub.coop": {
      provider: "raccoonvalley",
      name: "Raccoon Valley Electric Cooperative",
    },
    "sacosage.smarthub.coop": {
      provider: "mo_sac_osage",
      name: "Sac Osage Electric Cooperative",
    },
    "salemelectric.smarthub.coop": {
      provider: "salem_electric",
      name: "Salem Electric",
    },
    "samhouston.smarthub.coop": {
      provider: "sam_houston",
      name: "Sam Houston Electric Cooperative",
    },
    "santee.smarthub.coop": {
      provider: "santee",
      name: "Santee Electric Cooperative",
    },
    "sawnee.smarthub.coop": {
      provider: "sawnee_emc",
      name: "Sawnee EMC",
    },
    "sbec.smarthub.coop": {
      provider: "sbec",
      name: "San Bernard Electric Cooperative",
    },
    "scaec.smarthub.coop": {
      provider: "scaec",
      name: "South Central Arkansas Electric Cooperative",
    },
    "sec.smarthub.coop": {
      provider: "sec",
      name: "Southside Electric Cooperative",
    },
    "secpa.smarthub.coop": {
      provider: "secpa",
      name: "Southeast Colorado Power Association",
    },
    "sedgwickcountyelectric.smarthub.coop": {
      provider: "sedgwick_county",
      name: "Sedgwick County Electric Cooperative",
    },
    "seecoop.smarthub.coop": {
      provider: "mt_seco",
      name: "Southeast Electric Cooperative",
    },
    "seiec.smarthub.coop": {
      provider: "seiec",
      name: "SouthEastern Illinois Electric Cooperative",
    },
    "seiremc.smarthub.coop": {
      provider: "seiremc",
      name: "Southeastern Indiana REMC",
    },
    "semano.smarthub.coop": {
      provider: "mo_semano",
      name: "Se-Ma-No Electric Cooperative",
    },
    "shelbyelectric.smarthub.coop": {
      provider: "oh_shelby",
      name: "Shelby Municipal Electric",
    },
    "shelbyenergy.smarthub.coop": {
      provider: "shelby_energy",
      name: "Shelby Energy Cooperative",
    },
    "sheridanelectric.smarthub.coop": {
      provider: "sheridan",
      name: "Sheridan Electric Cooperative",
    },
    "siea.smarthub.coop": {
      provider: "siea",
      name: "San Isabel Electric Association",
    },
    "siec.smarthub.coop": {
      provider: "siec",
      name: "Southern Illinois Electric Cooperative",
    },
    "sierraelectric.smarthub.coop": {
      provider: "sierra",
      name: "Sierra Electric Cooperative",
    },
    "singingriver.smarthub.coop": {
      provider: "singing_river",
      name: "Singing River Electric Cooperative",
    },
    "siouxvalleyenergy.smarthub.coop": {
      provider: "sioux_valley",
      name: "Sioux Valley Energy",
    },
    "sirec.smarthub.coop": {
      provider: "sirec",
      name: "Southern Indiana REC",
    },
    "skamaniapud.smarthub.coop": {
      provider: "skamania_pud",
      name: "Skamania County PUD",
    },
    "skrecc.smarthub.coop": {
      provider: "south_kentucky",
      name: "South Kentucky RECC",
    },
    "slemco.smarthub.coop": {
      provider: "slemco",
      name: "South Louisiana Electric Membership Corporation (SLEMCO)",
    },
    "slopeelectric.smarthub.coop": {
      provider: "slopeelectric",
      name: "Slope Electric Cooperative",
    },
    "slvrec.smarthub.coop": {
      provider: "slvrec",
      name: "San Luis Valley Rural Electric Cooperative",
    },
    "smpa.smarthub.coop": {
      provider: "smpa",
      name: "San Miguel Power Association",
    },
    "somersetrec.smarthub.coop": {
      provider: "somerset_rec",
      name: "Somerset Rural Electric Cooperative",
    },
    "southcentralpower.smarthub.coop": {
      provider: "oh_southcentral",
      name: "South Central Power Company",
    },
    "southeasternelectric.smarthub.coop": {
      provider: "southeastern",
      name: "Southeastern Electric Cooperative",
    },
    "southwestepa.smarthub.coop": {
      provider: "southwest_epa",
      name: "Southwest Mississippi Electric Power Association",
    },
    "springgrove.smarthub.coop": {
      provider: "mn_spring_grove",
      name: "City of Spring Grove",
    },
    "srec.smarthub.coop": {
      provider: "srec",
      name: "Steuben Rural Electric Cooperative",
    },
    "ssvec.smarthub.coop": {
      provider: "ssvec",
      name: "Sulphur Springs Valley Electric Cooperative (SSVEC)",
    },
    "stearnselectric.smarthub.coop": {
      provider: "stearns",
      name: "Stearns Electric Association",
    },
    "stoweelectric.smarthub.coop": {
      provider: "stowe",
      name: "Village of Stowe Electric Department",
    },
    "sucocoop.smarthub.coop": {
      provider: "sumner_cowley",
      name: "Sumner-Cowley Electric Cooperative",
    },
    "sunriverec.smarthub.coop": {
      provider: "sunriver",
      name: "Sun River Electric Cooperative",
    },
    "svec.smarthub.coop": {
      provider: "svec",
      name: "Shenandoah Valley Electric Cooperative (SVEC)",
    },
    "swce.smarthub.coop": {
      provider: "steele_waseca",
      name: "Steele-Waseca Cooperative Electric",
    },
    "swiarec.smarthub.coop": {
      provider: "southwestiowa",
      name: "Southwest Iowa Rural Electric Cooperative",
    },
    "swre.smarthub.coop": {
      provider: "swre",
      name: "Southwest Rural Electric Association",
    },
    "swrea.smarthub.coop": {
      provider: "swrea",
      name: "Southwest Arkansas Electric Cooperative",
    },
    "syemc.smarthub.coop": {
      provider: "surry_yadkin_emc",
      name: "Surry-Yadkin EMC",
    },
    "tannerelectric.smarthub.coop": {
      provider: "tanner_electric",
      name: "Tanner Electric Cooperative",
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
    "tdpud.smarthub.coop": {
      provider: "tdpud",
      name: "Truckee Donner Public Utility District",
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
    "threeriverselectric.smarthub.coop": {
      provider: "mo_three_rivers",
      name: "Three Rivers Electric Cooperative",
    },
    "tipmont.smarthub.coop": {
      provider: "tipmont",
      name: "Tipmont REMC",
    },
    "tiprec.smarthub.coop": {
      provider: "tip_rec",
      name: "T.I.P. Rural Electric Cooperative",
    },
    "toddwadena.smarthub.coop": {
      provider: "todd_wadena",
      name: "Todd-Wadena Electric Cooperative",
    },
    "tombigbee.smarthub.coop": {
      provider: "tombigbee",
      name: "Tombigbee Electric Cooperative",
    },
    "tongueriverelectric.smarthub.coop": {
      provider: "tongueriver",
      name: "Tongue River Electric Cooperative",
    },
    "tpud.smarthub.coop": {
      provider: "tpud",
      name: "Tillamook People's Utility District",
    },
    "traverseelectric.smarthub.coop": {
      provider: "traverse",
      name: "Traverse Electric Cooperative",
    },
    "trico.smarthub.coop": {
      provider: "trico",
      name: "Trico Electric Cooperative",
    },
    "tricountycoop.smarthub.coop": {
      provider: "tri_county",
      name: "Tri-County Electric Cooperative",
    },
    "tricountyrec.smarthub.coop": {
      provider: "tri_county_rec",
      name: "Tri-County Rural Electric Cooperative",
    },
    "trinitypud.smarthub.coop": {
      provider: "trinitypud",
      name: "Trinity Public Utilities District",
    },
    "tristate.smarthub.coop": {
      provider: "tri_state_emc",
      name: "Tri-State EMC",
    },
    "tvec.smarthub.coop": {
      provider: "tvec_al",
      name: "Tennessee Valley Electric Cooperative (AL)",
    },
    "twinvalleyelectric.smarthub.coop": {
      provider: "twin_valley",
      name: "Twin Valley Electric Cooperative",
    },
    "ueci.smarthub.coop": {
      provider: "mo_ueci",
      name: "United Electric Cooperative",
    },
    "umatillaelectric.smarthub.coop": {
      provider: "umatilla_electric",
      name: "Umatilla Electric Cooperative",
    },
    "unitedelectric.smarthub.coop": {
      provider: "unitedelectric",
      name: "United Electric Cooperative",
    },
    "unitedpa.smarthub.coop": {
      provider: "united_ec",
      name: "United Electric Cooperative",
    },
    "unitedpower.smarthub.coop": {
      provider: "unitedpower",
      name: "United Power",
    },
    "valleyrec.smarthub.coop": {
      provider: "valley_rec",
      name: "Valley Rural Electric Cooperative",
    },
    "vea.smarthub.coop": {
      provider: "vea",
      name: "Valley Electric Association",
    },
    "verendrye.smarthub.coop": {
      provider: "verendrye",
      name: "Verendrye Electric Cooperative",
    },
    "vermontelectric.smarthub.coop": {
      provider: "vec",
      name: "Vermont Electric Cooperative",
    },
    "vernonelectric.smarthub.coop": {
      provider: "vernonelectric",
      name: "Vernon Electric Cooperative",
    },
    "victoriaelectric.smarthub.coop": {
      provider: "victoria",
      name: "Victoria Electric Cooperative",
    },
    "victoryelectric.smarthub.coop": {
      provider: "victory",
      name: "Victory Electric Cooperative",
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
    "vvec.smarthub.coop": {
      provider: "vvec",
      name: "Verdigris Valley Electric Cooperative",
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
    "wcec.smarthub.coop": {
      provider: "wcec",
      name: "Wood County Electric Cooperative",
    },
    "websterec.smarthub.coop": {
      provider: "mo_webster",
      name: "Webster Electric Cooperative",
    },
    "weci.smarthub.coop": {
      provider: "weci",
      name: "Wheatland Electric Cooperative Inc.",
    },
    "wemc.smarthub.coop": {
      provider: "wake_emc",
      name: "Wake Electric Membership Corporation",
    },
    "westcentralelectric.smarthub.coop": {
      provider: "mo_west_central",
      name: "West Central Electric",
    },
    "westerncoop.smarthub.coop": {
      provider: "western_coop",
      name: "Western Cooperative Electric Association",
    },
    "westoregon.smarthub.coop": {
      provider: "west_oregon",
      name: "West Oregon Electric Cooperative",
    },
    "westriver.smarthub.coop": {
      provider: "west_river",
      name: "West River Electric Association",
    },
    "wheatbelt.smarthub.coop": {
      provider: "wheatbelt_ppd",
      name: "Wheat Belt Public Power District",
    },
    "wheatland.smarthub.coop": {
      provider: "wheatland",
      name: "Wheatland Electric Cooperative",
    },
    "whetstone.smarthub.coop": {
      provider: "whetstone",
      name: "Whetstone Valley Electric Cooperative",
    },
    "whiteriver.smarthub.coop": {
      provider: "mo_white_river",
      name: "White River Valley Electric Cooperative",
    },
    "wildriceelectric.smarthub.coop": {
      provider: "wild_rice",
      name: "Wild Rice Electric Cooperative",
    },
    "wrea.smarthub.coop": {
      provider: "wright_hennepin",
      name: "Wright-Hennepin Cooperative Electric Association",
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
    "yvea.smarthub.coop": {
      provider: "yvea",
      name: "Yampa Valley Electric Association",
    },
    "yvec.smarthub.coop": {
      provider: "yvec",
      name: "Yellowstone Valley Electric Cooperative",
    },
    "ywelectric.smarthub.coop": {
      provider: "ywelectric",
      name: "Y-W Electric Association",
    },
  };

  // Detect provider from the current page's hostname.
  // Unknown *.smarthub.coop hosts get a DETERMINISTIC discovered code
  // ("sh_<subdomain>") instead of masquerading as VEC — the backend mints
  // the identical code from user.hostname (api/adapters/smarthub.py
  // derive_provider_from_host), records the sighting, and alerts us to
  // promote the utility to the catalog. Data flows correctly on the very
  // first login from a brand-new co-op.
  function detectProvider(hostname) {
    const host = hostname.toLowerCase();
    const entry = SMARTHUB_REGISTRY[host];
    if (entry) return entry;
    if (host.endsWith(".smarthub.coop")) {
      const sub = host.slice(0, -".smarthub.coop".length);
      const code = sub.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 37);
      console.info(
        `[EnergyAgent] New SmartHub host: ${host} — capturing under ` +
          `discovered code sh_${code}. It will be promoted to the catalog automatically.`
      );
      return {
        provider: "sh_" + code,
        name: sub.replace(/[-.]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) + " (SmartHub)",
        discovered: true,
      };
    }
    return null;
  }

  // Expose on window so smarthub_content.js (loaded in the same content-script
  // world) can call window.SMARTHUB_REGISTRY and window.detectSmartHubProvider.
  window.SMARTHUB_REGISTRY = SMARTHUB_REGISTRY;
  window.detectSmartHubProvider = detectProvider;
})();
