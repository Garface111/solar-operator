# PROOF: SolarEdge live peer-analysis (2026-06-13)

**Result: the product's peer-analysis engine ran end-to-end on REAL live SolarEdge
inverter data. The "demo -> live" milestone for the Array Operator engine is proven.**

## What ran
`docs/proofs/solaredge_live_peer_proof.py` — pulls a SolarEdge site's inverters via the
official monitoring API, builds per-inverter daily kWh from the `totalEnergy` lifetime
counter (diff per day), derives nameplate from the model string (e.g. `SE20K` -> 20 kW),
and feeds the cohort straight into `api/inverters/peer_analysis.py::analyze_cohort`.

Run it with the key in env (NEVER commit the key):
```
cd ~/solar-operator && SE_KEY='<key>' .venv/bin/python docs/proofs/solaredge_live_peer_proof.py
```

## Live result (Bruce's array, pulled live)
- Site: **Londonderry Community Solar** (SolarEdge site id 416160), 99.54 kW, installed 2016.
- 6 inverters: 2x RSE33.3K, 1x SE33.3K, 2x SE20K, 1x SE10K.
- Snapshot: currentPower 116.6 kW, today 590 kWh, month 11.3 MWh, lifetime ~2.0 GWh.
- Peer analysis: **6/6 ok**, peer_index range 0.96–1.02 (healthy tight cluster), 0 loss.
  - peer_index normalizes by nameplate share, so the 10 kW unit reading 0.96 is fine.
  - A shaded/dead string would drop below 0.85 (UNDERPERFORM_THRESHOLD) and flag.

## API mechanics learned (for the real adapter wiring)
- `GET /sites/list` — account-level key returns ALL sites; site-level key returns just its one.
- `GET /site/{id}/inventory` — inverter list with SN + model + optimizer count.
- `GET /site/{id}/overview` — currentPower + lastDay/Month/lifeTime energy.
- `GET /equipment/{id}/{sn}/data?startTime=&endTime=` — per-inverter telemetry; **7-day span
  cap per call**. `totalEnergy` is a lifetime Wh counter (diff for daily kWh). `inverterMode`
  carries fault state (STARTING/MPPT/PRODUCING = healthy; FAULT/ERROR/SHUTDOWN/LOCKED = fault).
- Rate budget: 300 req/day per key -> needs the 5-min server cache (already noted in skill).

## Caveats / next steps
1. **The key tested was SITE-LEVEL** — only Londonderry (1 of Bruce's ~7 arrays). To light up
   the whole fleet via "one credential, all arrays", need an ACCOUNT-LEVEL key (then
   `/sites/list` returns all). Open question: are Bruce's 7 arrays under ONE account or
   several? The site-level key seeing only Londonderry hints they may be split.
2. **Not yet wired into the product** — this is a standalone proof. To go live in the UI:
   store the key on Bruce's Array record (credential storage = Ford's call), hook the pull
   into `api/inverters/solaredge.py` + `/v1/array-owners/overview`, render in
   `ArrayOverview.tsx` (peer bars + diagnosis — the built-but-unfed UI).
3. **Key handling**: not stored anywhere during this proof; temp JSON scrubbed. Production
   needs a real credential-storage decision.
