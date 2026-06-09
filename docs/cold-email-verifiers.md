# Cold Email Drafts — Solar Operator → NEPOOL Verifier ecosystem

Date: 2026-06-09
Product assumed: **Solar Operator** (automated net-metering credit reports for community-solar
operators). If you meant the consumer owner-dashboard, say so — these don't fit that.

Voice: kitchen-table honest, not enterprise. Short. One ask. From a real person (Ford), not "the team."
Sender: admin@solaroperator.org (Resend). See deliverability notes at bottom BEFORE sending.

────────────────────────────────────────────────────────
TARGETING — who actually gets an email (not all 68)
────────────────────────────────────────────────────────

SEGMENT A — Consultants / REC & energy-services firms (the ones doing the manual work SO replaces).
  Best targets. Use Template A (sell) or A2 (white-label/channel).
  • Daymark Energy Advisors (formerly La Capra)        617-778-5515
  • CommonWealth Resource Management Corporation         (Boston, MA — verify alive)
  • Peregrine Energy Group                                617-720-0070
  • The Cadmus Group                                      617-673-7000
  • ECR Strategies, LLC                                   (low data)
  • Oak Point Energy Associates LLC                       (low data)
  • Energy Tariff Experts LLC                             (low data)
  • Conservation Resource Solutions Inc.                  (low data)
  • Sustainable Energy Developments, Inc.                 (low data)
  • Natural Capital, LLC                                  (low data)
  • New England Net Metering LLC                          (low data)

SEGMENT B — Monitoring / software firms (data sources & possible integration partners).
  Use Template B (integrate/partner). NOT a "buy my SaaS" pitch.
  • AlsoEnergy (GE Vernova)            866-303-5668
  • Locus Energy (now AlsoEnergy)       —
  • Solar-Log                           info@solar-log.com
  • SolarEdge Technologies              510-498-3200
  • Solectria / Yaskawa-Solectria       978-683-9700  sales@solectria.com
  • PowerDash Inc.                       —
  • Draker (defunct/absorbed)           — skip unless contact surfaces

SEGMENT C — Solar developers/EPCs (own/operate arrays → could BE customers or refer owners).
  Use Template A, lightly reworded.
  • Solect Energy                       508-598-3511  info@solect.com
  • EnterSolar (EDF Renewables)          —
  • Clean Energy Associates / Intertek CEA  —

DO NOT EMAIL (reputation risk / not buyers):
  • Utilities & quasi-gov: Eversource, National Grid/Mass Electric, Unitil/Fitchburg, CL&P,
    United Illuminating, Narragansett/RI Energy, VELCO, MMWEC, VEPP, VPPSA, NH Electric Co-op,
    Energy New England, MassCEC, National Semiconductor, ABB, Eaton, BGC.
  • Bare names / login artifacts: A Quincy Vale, Adam Kohler PE, Bill Short, Chad Singleton,
    wdanfort, Sorapro, etc.
  • Unverifiable common-name contractors: Cady Electric, Coastal Electric, Titan Electric,
    Bennett Engineering, Chase Systems, EcoLectric, Ampersand, Axsess Group, etc.

────────────────────────────────────────────────────────
TEMPLATE A — SELL (Segment A consultants + Segment C developers)
Goal: a 15-min call. Merge fields in {{ }}.
────────────────────────────────────────────────────────
Subject: the quarterly net-metering reports {{FirstName}}

Hi {{FirstName}},

I saw {{Company}} on the NEPOOL GIS verifier list, so you almost certainly know the pain
I'm writing about: the quarterly net-metering credit reports for community-solar projects —
the ones that mean pulling utility bills, reconciling kWh against the GIS data, and rebuilding
the same spreadsheet every quarter for every array.

I built Solar Operator to do that part automatically. It logs into the utility portal, pulls
the bills, and emails a finished, audit-ready credit report — per array, every quarter — for a
fraction of what the manual version costs in hours.

It's live with a Vermont operator running 7 arrays across 9 GMP accounts today.

Worth 15 minutes to see if it saves {{Company}} the grind? I'm happy to run one of your real
arrays through it so you're looking at your own numbers, not a demo.

— Ford
Solar Operator · solaroperator.org

────────────────────────────────────────────────────────
TEMPLATE A2 — WHITE-LABEL / CHANNEL (Segment A, for firms who'd rather keep the client)
────────────────────────────────────────────────────────
Subject: keep the client, lose the spreadsheet {{FirstName}}

Hi {{FirstName}},

Quick one. {{Company}} owns the client relationship for net-metering reporting — I'm not trying
to get between you and that. I'm trying to take the part nobody enjoys off your plate.

Solar Operator pulls the utility bills and generates the per-array quarterly credit report
automatically. You keep the relationship, the branding, the trust; it just stops being a manual
spreadsheet job every quarter.

If you're doing this for more than a handful of arrays, the math gets interesting fast. Open to
a short call to see whether a white-label fit makes sense?

— Ford
Solar Operator · solaroperator.org

────────────────────────────────────────────────────────
TEMPLATE B — INTEGRATE / PARTNER (Segment B monitoring & inverter firms)
Goal: explore data integration, not sell SaaS.
────────────────────────────────────────────────────────
Subject: {{Company}} data → finished credit reports

Hi {{FirstName}},

{{Company}} already has the generation data operators care about. The gap I keep seeing is the
last mile: turning that production data into the quarterly net-metering CREDIT report a community-
solar operator actually has to file — reconciled against utility bills, per array, audit-ready.

That's the piece Solar Operator automates. I think there's a clean fit: your monitoring data in,
finished operator-facing reports out — without your team building a reporting product you don't
want to own.

Is there someone on your side who owns partnerships/integrations I should talk to? Happy to show
a working example first so it's concrete.

— Ford
Solar Operator · solaroperator.org

────────────────────────────────────────────────────────
DELIVERABILITY & COMPLIANCE — read before sending
────────────────────────────────────────────────────────
1. CAN-SPAM: every email needs a physical postal address + a working unsubscribe/opt-out line.
   Add a footer:  "Solar Operator, {{postal address}}. Not interested? Reply 'no' and I won't
   write again." Cold B2B is legal in the US with these; it is NOT in some other jurisdictions.
2. Volume/reputation: solaroperator.org is your TRANSACTIONAL domain (Stripe receipts, the actual
   reports). Do NOT send cold outreach from it — one spam complaint can hurt deliverability of
   real customer reports. Use a SEPARATE domain/subdomain (e.g. mail.solaroperator.com or a
   .com cousin) warmed up for outreach. This is the single most important note here.
3. Personalize line 1 per recipient (reference their actual firm/work) or these read as spray-and-
   pray. The merge fields are a floor, not a finish.
4. Send in small batches (10–20/day from a warmed domain), plain text, real reply-to.
5. These are B2B firm contacts, mostly "web form" not direct emails — many will go through a
   contact form, not an inbox. Adjust expectations.
