# Array Owner — persona & ranked pain (drives the Array Operator site design)

> Synthesized from domain knowledge across solar-operator / sun-mirror, NOT
> scraped from live owner forums (web research was unavailable when written —
> no Firecrawl credits). Validate with real complaints when web tools are on.

## Who the Array Operator is
Not an installer, not an engineer. A person/org who OWNS solar and wants the
thing they paid for to keep paying them. Three shapes:
1. Residential rooftop owner — checks the vendor app ~twice a year, no idea what
   "good" looks like.
2. Prosumer / multi-array owner (barn, field, second home) or a community-solar
   host like Bruce's customers — juggles separate logins. THE wedge: "one
   credential, every array."
3. Small commercial / PPA-lease holder — has a production guarantee in a
   contract nobody is verifying.

## Pain, ranked by how much it hurts
1. **"Is it even working?"** — core anxiety. Vendor apps (mySolarEdge,
   Enlighten) show a number but never say if it's GOOD or BAD. Silent
   underperformance (tripped optimizer, shaded string) bleeds money for a year,
   discovered at the annual true-up. THIS is what peer_index kills.
2. **"This app is for installers, not me."** — DC voltage, inverter mode,
   error 0x21. Jargon with no translation to dollars or action.
3. **"Nobody is watching."** — after the installer leaves, no one proactively
   monitors; the owner is the de-facto monitor with no tools.
4. **"What is it actually worth?"** — owners don't see energy offset +
   net-metering credits + RECs/SRECs; most don't know their RECs are sellable
   (installer often pockets them).
5. **"Many systems, many logins."** — no unified weather-normalized view.
6. **"Warranty claims are on me."** — owner must notice, document, and chase;
   most lack the evidence.
7. **"Am I being ripped off?"** — unverified PPA/lease production guarantees.

## Product thesis: take the work OUT of their hands
Vendor apps make YOU the analyst. Array Operator makes the AGENT the analyst and
hands the owner a verdict + finished paperwork. Dollar-first, plain English,
zero jargon. "Sublime over tool."

### Done-for-you features (not dashboards-for-you)
- Always watching (peer_index ground truth) — every inverter vs its own fleet
  under the same sky; catch the silent loser the day it starts.
- Plain-English + dollars — "Inverter C down ~9 kWh/day, ~$1.40. Here's why,
  here's what to do."
- Warranty claim pre-drafted — dead/faulted unit → drafted service email with
  loss-kWh + dates + fault code attached, one click.
- REC money found — detect unsold REC revenue, hand owner to the NEPOOL Operator
  side (umbrella synergy owner→verifier).
- One credential, every array — paste one SolarEdge account key, all sites
  appear (backend solaredge/discover already built).
- The annual story, auto-written — "what your array did this year."
