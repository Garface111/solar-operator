# Inverter vendor expansion — what's wired, what's next, and the grounding bar

Lane B goal: "support ALL the brands people have." The framework
(`api/inverters/`) makes each vendor a module, so breadth is cheap — EXCEPT the
honesty bar: **never fabricate an endpoint/auth.** A vendor gets an adapter only
when its auth + station-list contract is grounded against published docs or a
community-verified client. Telemetry field names may be parsed defensively + flagged
"unverified" (the SMA bar) — but auth/base/paths must be real.

## Wired (as of 2026-06-21)

| Vendor | Status | Auth grounded from |
|--------|--------|--------------------|
| SolarEdge | 🟢 LIVE-PROVEN | Bruce's account |
| Chint / CPS | 🟢 extension capture (HAR-grounded, live-capture unproven) | Bruce HAR |
| Enphase (Enlighten v4) | 🟡 code-complete, unverified-live | developer-v4.enphase.com |
| **Solis (SolisCloud)** | 🟡 code-complete, unverified-live | oss.soliscloud.com HMAC docs |
| **Tigo (EI v3)** | 🟡 code-complete, unverified-live | api2.tigoenergy.com + EI docs |
| Locus / Fronius / SMA / AlsoEnergy | 🟡 code-complete, unverified-live | their published docs |

Each 🟡 needs ONE real account to flip to 🟢 (the recurring blocker is always a
credential, never code — see `inverter-vendor-status.md`).

## Next candidates — NOT yet built (need grounding before we can avoid faking)

These are common brands, but their APIs are reverse-engineered / login-gated, so
the exact auth handshake + field names aren't safely groundable from public docs
alone. Building blind would fabricate. What each needs from Ford:

- **Sungrow (iSolarCloud)** — has a developer portal (developer-api.isolarcloud.com)
  but NO published spec; the contract (appkey + x-access-key + login token,
  `getPowerStationList` / `getDeviceRealTimeData`) is only known from
  reverse-engineered clients (GoSungrow, pysolarcloud). ASK: register an iSolarCloud
  app (appkey + secret) so we can confirm the live handshake.
- **GoodWe (SEMS)** — `semsportal.com/api/v2/Common/CrossLogin` token dance →
  `GetMonitorDetailByPowerStationId`. Widely used (HA `goodwe`), but the crosslogin
  token-header format is fiddly. ASK: one real SEMS login to capture the exact
  login response + station-detail shape (a HAR, like Chint).
- **Growatt (ShinePhone/OSS)** — `server-api.growatt.com`, MD5-hashed password
  login, `plantList`/`devList`. Reverse-engineered only; Growatt rotates endpoints.
  ASK: a Growatt OSS API account (official) OR a real login HAR.
- **Huawei FusionSolar (Northbound)** — documented but account-gated
  (`/thirdData/login` → XSRF token → `/getStationList`, `/getDevRealKpi`). ASK: a
  FusionSolar Northbound account (systemCode) to ground + verify.
- **Tesla (Fleet/Energy API)** — large brand, but OAuth + partner registration +
  strict TOS + virtual-key install. Heavier; do only if a Tesla-heavy prospect
  appears. ASK: Tesla developer/partner registration.
- **APsystems (EMA)** — limited public API; mostly installer-tier. ASK: confirm an
  EMA API tier exists for the owner before building.

## How to add one (the repeatable shape)
1. Ground auth + station-list + a current-power + a daily-energy endpoint
   (published docs OR a community client OR a HAR — never memory alone).
2. Copy `enphase.py` / `solis.py` as the template; defensive field parsing; LOUD
   "unverified-live" docstring; honest NOTE.
3. Register in `VENDORS` (`__init__.py`) + add to `array-operator/public/onboarding.html`
   VENDORS with honest "in verification" copy.
4. Mocked-shape tests + update the vendors-listing assertion in
   `tests/test_inverters.py`.
5. Deploy backend (push→Railway) + AO (`deploy_ao_clean.sh`). Flip 🟡→🟢 only after
   a real account runs validate/fetch_live/fetch_daily clean.
