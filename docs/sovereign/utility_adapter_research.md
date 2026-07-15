Utility adapter research backlog plan (2026-07)

3 requests currently in `researching` status per utility_requests.py.

Priority: NEPOOL/owner-facing AO utilities.

For each:
- Identify portal family (SmartHub? bespoke? NISC?)
- Document login + data endpoints (from public info/HAR only)
- Open adapter work ticket ONLY if HAR/credentials available
- NEVER fabricate adapters or code (see smarthub.py, auto_adapters.py)

Next steps:
1. Query utility_requests table for researching rows (filter NEPOOL/state hints).
2. Cross-ref providers/*.csv and adapters/ for family matches.
3. Produce per-utility endpoint notes in this doc (no new adapter files).
4. Flag any ready for HAR capture in cloud_capture.py flow.

No code changes; research artifact only.