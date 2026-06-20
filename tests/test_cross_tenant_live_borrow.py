"""Cross-tenant live-power borrow — _live_power_w(..., borrow=...).

When the same physical inverter (same vendor+serial) is captured into two
tenants and one browser returns a bogus near-zero live wattage while the other
captured the real value THIS window, the array card should show the real one.
Upward-only, serial-exact, daylight-gated.

Covers the borrow decision in _live_power_w directly (pure function, no DB):
  1. Local bogus-low + fresh sibling high -> borrow the high value
  2. Local already-good >= sibling        -> keep local (never drag down)
  3. Night (daylight=False)               -> never borrow (everyone reads ~0)
  4. No borrow entry for this serial       -> unchanged local value
  5. Live telemetry (m.last_power_w) still wins as the real instant when present,
     but a higher fresh sibling still corrects a low live value
"""
from __future__ import annotations

from types import SimpleNamespace

from api import inverter_fleet as F
from api.models import now


def _iv(serial: str, vendor: str = "fronius", last_power_w=None, fresh: bool = True):
    return SimpleNamespace(
        serial=serial, vendor=vendor,
        last_power_w=last_power_w,
        last_power_at=now() if fresh else None,
    )


# 1. local captured a bogus 3W; a sibling tenant captured 2257W fresh -> borrow.
def test_borrow_lifts_bogus_low_local():
    iv = _iv("dev-1", last_power_w=3.0)
    borrow = {("fronius", "dev-1"): 2257.1}
    out = F._live_power_w(iv, {"last_power_w": None}, daylight=True, borrow=borrow)
    assert out == 2257.1


# 2. local is already the best -> keep it, never let a lower sibling drag it down.
def test_local_good_not_dragged_down():
    iv = _iv("dev-1", last_power_w=2300.0)
    borrow = {("fronius", "dev-1"): 2257.1}
    out = F._live_power_w(iv, {"last_power_w": None}, daylight=True, borrow=borrow)
    assert out == 2300.0


# 3. at night nothing is borrowed (every tenant reads ~0; no real value exists).
def test_no_borrow_at_night():
    iv = _iv("dev-1", last_power_w=3.0, fresh=False)
    borrow = {("fronius", "dev-1"): 2257.1}
    out = F._live_power_w(iv, {"last_power_w": None}, daylight=False, borrow=borrow)
    assert out is None


# 4. no sibling entry for this serial -> local value passes through unchanged.
def test_no_borrow_entry_keeps_local():
    iv = _iv("dev-9", last_power_w=3.0)
    borrow = {("fronius", "dev-1"): 2257.1}
    out = F._live_power_w(iv, {"last_power_w": None}, daylight=True, borrow=borrow)
    assert out == 3.0


# 5. a live telemetry value is the real instant, but a higher fresh sibling still
#    corrects it (same upward-only rule); an equal/higher live value is kept.
def test_live_telemetry_still_correctable_upward():
    iv = _iv("dev-1", last_power_w=None)
    borrow = {("fronius", "dev-1"): 2257.1}
    # live telemetry reads a low 3W -> sibling corrects upward
    out = F._live_power_w(iv, {"last_power_w": 3.0}, daylight=True, borrow=borrow)
    assert out == 2257.1
    # live telemetry reads a healthy 2400W -> kept (sibling lower)
    out2 = F._live_power_w(iv, {"last_power_w": 2400.0}, daylight=True, borrow=borrow)
    assert out2 == 2400.0


# 6. vendor must match — a same-serial different-vendor sibling is ignored.
def test_vendor_must_match():
    iv = _iv("dev-1", vendor="fronius", last_power_w=3.0)
    borrow = {("sma", "dev-1"): 2257.1}   # different vendor keyed
    out = F._live_power_w(iv, {"last_power_w": None}, daylight=True, borrow=borrow)
    assert out == 3.0
