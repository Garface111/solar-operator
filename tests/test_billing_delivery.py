"""Tests for the Array Operator billing endpoints + delivery pipeline.

Covers: /match preview, subscription create/list/patch/delete, the recipient
slider (to me / to client / to both), format selection, and a dry-run send that
mocks Resend so no real email goes out.
"""
from __future__ import annotations

import pathlib
import secrets
from datetime import date

import pytest
from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, Client, BillingReportSubscription, Array, DailyGeneration

FIX = pathlib.Path(__file__).parent / "fixtures" / "billing"


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Billing Test Operator",
            contact_email=f"{tid}@operator.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _upload(client, auth, fixture="fairlee.xlsx", **form):
    data = (FIX / fixture).read_bytes()
    files = {"file": (fixture, data,
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    return client.post("/v1/array-operator/billing/subscriptions",
                       files=files, data=form, headers={"Authorization": auth})


# ─── /match ─────────────────────────────────────────────────────────────────

def test_match_preview_saves_nothing(client):
    _, auth = _make_tenant()
    data = (FIX / "norwich.xlsx").read_bytes()
    r = client.post("/v1/array-operator/billing/match",
                    files={"file": ("norwich.xlsx", data, "application/octet-stream")},
                    headers={"Authorization": auth})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"]
    assert body["match"]["customer"]["name"] == "Norwich Fire District"
    assert body["match"]["billing_model"] == "percent_of_array"
    # nothing persisted
    with SessionLocal() as db:
        assert db.execute(select(BillingReportSubscription)).first() is None


def test_match_requires_auth(client):
    data = (FIX / "norwich.xlsx").read_bytes()
    r = client.post("/v1/array-operator/billing/match",
                    files={"file": ("n.xlsx", data, "application/octet-stream")})
    assert r.status_code == 401


# ─── subscription lifecycle ─────────────────────────────────────────────────

def test_create_subscription_links_client_and_defaults_to_me(client):
    tid, auth = _make_tenant()
    r = _upload(client, auth, "fairlee.xlsx", cadence="monthly")
    assert r.status_code == 200, r.text
    sub = r.json()["subscription"]
    assert sub["customer_name"] == "Town of Fairlee"
    assert sub["send_mode"] == "to_me"          # safe default — no customer email yet
    assert sub["cadence"] == "monthly"
    assert sub["next_send_at"]
    # A Client row was created underneath the operator.
    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.tenant_id == tid)).scalar_one()
        assert c.name == "Town of Fairlee"
        s = db.execute(select(BillingReportSubscription)).scalar_one()
        assert s.source_workbook  # workbook bytes stored
        assert s.client_id == c.id


def test_list_and_patch_slider_and_formats(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth, "fairlee.xlsx").json()["subscription"]["id"]

    lst = client.get("/v1/array-operator/billing/subscriptions",
                     headers={"Authorization": auth}).json()
    assert len(lst["subscriptions"]) == 1

    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"send_mode": "to_both", "client_email": "town@fairlee.gov",
                           "formats": ["pdf", "xlsx"], "cadence": "quarterly"},
                     headers={"Authorization": auth})
    assert r.status_code == 200
    s = r.json()["subscription"]
    assert s["send_mode"] == "to_both"
    assert s["client_email"] == "town@fairlee.gov"
    assert set(s["formats"]) == {"pdf", "xlsx"}
    assert s["cadence"] == "quarterly"


def test_patch_rejects_bad_send_mode(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"send_mode": "to_everyone"},
                     headers={"Authorization": auth})
    assert r.status_code == 400


def test_delete_is_soft(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    assert client.delete(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                         headers={"Authorization": auth}).status_code == 200
    lst = client.get("/v1/array-operator/billing/subscriptions",
                     headers={"Authorization": auth}).json()
    assert lst["subscriptions"] == []


def test_tenant_isolation(client):
    _, auth_a = _make_tenant()
    _, auth_b = _make_tenant()
    sub_id = _upload(client, auth_a).json()["subscription"]["id"]
    # B cannot see or touch A's subscription.
    assert client.get("/v1/array-operator/billing/subscriptions",
                      headers={"Authorization": auth_b}).json()["subscriptions"] == []
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"enabled": False}, headers={"Authorization": auth_b})
    assert r.status_code == 404


# ─── preview ────────────────────────────────────────────────────────────────

def test_preview_invoice_pdf_streams(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth, "valley_cares.xlsx").json()["subscription"]["id"]
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sub_id}/preview",
                   params={"kind": "invoice", "fmt": "pdf"},
                   headers={"Authorization": auth})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"


# ─── delivery (mocked Resend) ───────────────────────────────────────────────

def test_send_now_test_goes_to_operator(client, monkeypatch):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "fairlee.xlsx",
                     send_mode="to_client", client_email="town@fairlee.gov",
                     formats='["pdf","xlsx"]').json()["subscription"]["id"]
    # The Array Operator performance summary is OPT-IN (off by default) — turn it on
    # so this test exercises the summary-attached path (and the include_summary PATCH).
    client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                 json={"include_summary": True}, headers={"Authorization": auth})

    captured = {}

    def fake_send(to, subject, html, text, attachments=None, from_addr=None,
                  reply_to=None, product="nepool", cc=None, bcc=None, **kw):
        captured.update(to=to, subject=subject, attachments=attachments,
                        product=product, cc=cc, bcc=bcc)
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "true"}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    # Test send always goes to the operator, never the customer.
    to = captured["to"]
    to_list = to if isinstance(to, list) else [to]
    assert any("operator.test" in addr for addr in to_list)
    assert all("fairlee.gov" not in addr for addr in to_list)
    # Both formats produced invoice + summary attachments.
    names = [a["filename"] for a in captured["attachments"]]
    assert any(n.endswith("_invoice.pdf") for n in names)
    assert any(n.endswith("_invoice.xlsx") for n in names)
    assert any("summary" in n for n in names)


def test_send_now_live_to_client_stamps_schedule(client, monkeypatch):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx",
                     send_mode="to_both", client_email="nfd@norwich.gov",
                     formats="pdf").json()["subscription"]["id"]

    captured = {}
    monkeypatch.setattr("api.notify._send_via_resend",
                        lambda **kw: captured.update(kw) or True)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "false"}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    # to_both → client is primary, operator cc'd.
    to = captured["to"]
    to_list = to if isinstance(to, list) else [to]
    assert any("norwich.gov" in a for a in to_list)
    # Live send stamps the schedule fields.
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub_id)
        assert s.last_sent_at is not None
        assert s.next_send_at is not None
        assert s.last_invoice_number  # e.g. "2026-05"


def test_send_now_to_client_without_email_errors(client, monkeypatch):
    _, auth = _make_tenant()
    # Fairlee workbook has no customer email; force to_client with none set.
    sub_id = _upload(client, auth, "fairlee.xlsx").json()["subscription"]["id"]
    client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                 json={"send_mode": "to_client", "client_email": ""},
                 headers={"Authorization": auth})
    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: True)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "false"}, headers={"Authorization": auth})
    assert r.status_code == 422


# ─── manual customer-input path (no workbook) ───────────────────────────────


def _make_array_with_generation(tid: str, kwh_per_day: float = 100.0,
                                days: int = 30) -> int:
    """An array with a recent full month of DailyGeneration rows. Returns its id."""
    from datetime import date, timedelta
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="Manual Co", active=True)
        db.add(c)
        db.flush()
        arr = Array(tenant_id=tid, name="Manual Array", client_id=c.id,
                    fuel_type="solar")
        db.add(arr)
        db.flush()
        aid = arr.id
        # Most-recent complete month: anchor on the 1st of last month.
        today = date.today()
        first_this = today.replace(day=1)
        anchor = (first_this - timedelta(days=1)).replace(day=1)  # 1st of last month
        for i in range(days):
            d = anchor + timedelta(days=i)
            if d.month != anchor.month:
                break
            db.add(DailyGeneration(tenant_id=tid, array_id=aid, day=d,
                                   kwh=kwh_per_day, source="csv"))
        db.commit()
    return aid


def _create_manual(client, auth, **form):
    """POST the subscriptions endpoint as multipart/form-data WITHOUT a file."""
    return client.post("/v1/array-operator/billing/subscriptions",
                       data=form, headers={"Authorization": auth})


def test_manual_subscription_no_file_creates_and_stores_allocation(client):
    """The manual customer-input path: NO xlsx, just typed fields. Asserts 200,
    the typed allocation is stored, and the sub appears in GET /subscriptions.

    Old behavior (proof this would FAIL pre-change): create_subscription did
    `if file is None: raise HTTPException(400, "Upload the billing workbook …")`,
    so this same request returned 400 and stored nothing.
    """
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid, kwh_per_day=100.0, days=30)

    r = _create_manual(client, auth,
                       customer_name="Paul Bozuwa", array_id=aid,
                       allocation_pct="0.25", cadence="monthly",
                       delivery_mode="approval", send_mode="to_me",
                       client_email="paul@example.com")
    assert r.status_code == 200, r.text
    sub = r.json()["subscription"]
    assert sub["customer_name"] == "Paul Bozuwa"
    assert sub["array_id"] == aid
    assert abs(sub["allocation_pct"] - 0.25) < 1e-9
    assert sub["billing_model"] == "percent_of_array"
    assert sub["cadence"] == "monthly"
    assert sub["next_send_at"]

    # Stored without a workbook, with the typed allocation.
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub["id"])
        assert s.source_workbook is None
        assert s.allocation_pct == 0.25
        assert s.array_id == aid

    # Appears in the list.
    lst = client.get("/v1/array-operator/billing/subscriptions",
                     headers={"Authorization": auth}).json()
    assert sub["id"] in [x["id"] for x in lst["subscriptions"]]


def test_manual_subscription_rejects_bad_allocation(client):
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid)
    # > 1.0 (caller must pass a fraction).
    r = _create_manual(client, auth, customer_name="Bad Pct", array_id=aid,
                       allocation_pct="1.5")
    assert r.status_code == 400
    # Missing array_id.
    r = _create_manual(client, auth, customer_name="No Array",
                       allocation_pct="0.5")
    assert r.status_code == 400


def test_manual_unbound_subscription_is_blocked_from_sending(client, monkeypatch):
    """GUARDRAIL: a manual offtaker NOT bound to a GMP utility account has no
    settled utility bill to invoice from, so send-now is BLOCKED — it must NEVER
    email an invoice synthesized from generation telemetry (DailyGeneration / GMP
    hourly data). The send-now endpoint returns 422 with an actionable message,
    and no email goes out. (The share is still COMPUTED for previews/drafts; we
    just refuse to put telemetry figures in a customer invoice.)"""
    tid, auth = _make_tenant()
    # Healthy generation exists (30 × 100 = 3000 kWh) — yet still must not send,
    # because telemetry is not a settled utility bill.
    aid = _make_array_with_generation(tid, kwh_per_day=100.0, days=30)
    sub_id = _create_manual(client, auth, customer_name="Share Test",
                            array_id=aid, allocation_pct="0.10",
                            send_mode="to_me").json()["subscription"]["id"]

    sent = {"called": False}

    def fake_send(*args, **kwargs):
        sent["called"] = True
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "true"}, headers={"Authorization": auth})
    assert r.status_code == 422, r.text
    assert "gmp utility" in r.json()["detail"].lower()
    # Crucially: no telemetry-based invoice was emailed.
    assert sent["called"] is False


def test_manual_subscription_array_must_be_owned(client):
    tid_a, auth_a = _make_tenant()
    tid_b, auth_b = _make_tenant()
    aid = _make_array_with_generation(tid_a)
    # Tenant B cannot attach a manual customer to tenant A's array.
    r = _create_manual(client, auth_b, customer_name="Cross Tenant",
                       array_id=aid, allocation_pct="0.2")
    assert r.status_code == 404


def test_create_subscription_persists_array_share_and_invoice_start(client):
    """Piece 4 (2026-07-01): POST /subscriptions now accepts array_share_pct (the
    GMP allocation share used by the bill-accuracy cross-check, DISTINCT from
    allocation_pct) and invoice_number_start (the sequential-numbering seed) at
    CREATE time — previously they were PATCH-only. Assert both persist, round-trip
    through GET /subscriptions, and seed the running invoice counter.

    (Proof this would FAIL pre-change: create_subscription had no Form param for
    either field, so they were silently dropped on create — the operator had to
    create then edit to set the share the accuracy check needs.)"""
    from api.db import SessionLocal
    from api.models import UtilityAccount
    tid, auth = _make_tenant()
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Cross-check Array", region="VT")
        db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="GMP-" + secrets.token_hex(2),
                              nickname="XCheck GMP")
        db.add(acct); db.flush()
        acct_id = acct.id
        db.commit()

    r = _create_manual(client, auth, customer_name="Group Net Metered Co",
                       utility_account_id=str(acct_id), allocation_pct="1.0",
                       array_share_pct="0.25", invoice_number_start="5000",
                       client_email="gnm@example.test")
    assert r.status_code == 200, r.text
    sub = r.json()["subscription"]
    sub_id = sub["id"]
    assert abs(sub["array_share_pct"] - 0.25) < 1e-9
    assert sub["invoice_number_start"] == 5000
    # The running counter is seeded from the start so the first invoice uses it.
    assert sub["invoice_number_next"] == 5000

    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub_id)
        assert s.array_share_pct == 0.25
        assert s.invoice_number_start == 5000
        assert s.invoice_number_next == 5000

    # Round-trips through the list the frontend reads.
    lst = client.get("/v1/array-operator/billing/subscriptions",
                     headers={"Authorization": auth}).json()
    row = next(x for x in lst["subscriptions"] if x["id"] == sub_id)
    assert abs(row["array_share_pct"] - 0.25) < 1e-9
    assert row["invoice_number_start"] == 5000


def test_create_subscription_validates_array_share(client):
    """array_share_pct must be a fraction in (0, 1] — a percent typo (25 instead of
    0.25) or a negative is rejected before any write, same rule as the PATCH path."""
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid)
    r = _create_manual(client, auth, customer_name="Bad Share", array_id=aid,
                       allocation_pct="0.5", array_share_pct="25")
    assert r.status_code == 400
    assert "array_share_pct" in r.json()["detail"]


def test_patch_subscription_sets_and_clears_array_share(client):
    """array_share_pct is settable AND clearable via PATCH: a number sets it, an
    explicit null clears it (so the accuracy check falls back to allocation_pct)."""
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid)
    sub_id = _create_manual(client, auth, customer_name="Share Edit",
                            array_id=aid, allocation_pct="1.0").json()["subscription"]["id"]
    # Set it.
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"array_share_pct": 0.4}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert abs(r.json()["subscription"]["array_share_pct"] - 0.4) < 1e-9
    # Clear it (explicit null).
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"array_share_pct": None}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert r.json()["subscription"]["array_share_pct"] is None


def test_share_percents_round_trip_at_three_decimals(client):
    """Bruce (2026-07-03): the share fields carry 3-decimal PERCENT precision
    (e.g. 24.783% = 0.24783). Nothing in the pipeline may round the stored
    fraction: POST keeps it exactly, GET /subscriptions (the list the editor
    prefills from) returns it exactly, and PATCH (the accordion-editor path)
    keeps it exactly. The UI renders (value * 100).toFixed(3), so any backend
    rounding here would corrupt the third decimal on the very next save."""
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid)
    r = _create_manual(client, auth, customer_name="Three Decimals",
                       array_id=aid, allocation_pct="0.24783",
                       array_share_pct="0.31417")
    assert r.status_code == 200, r.text
    sub = r.json()["subscription"]
    sub_id = sub["id"]
    # Exact float equality on purpose — "0.24783" parses to one double and it
    # must survive bit-for-bit (Float/DOUBLE PRECISION column, no rounding).
    assert sub["allocation_pct"] == 0.24783
    assert sub["array_share_pct"] == 0.31417

    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub_id)
        assert s.allocation_pct == 0.24783
        assert s.array_share_pct == 0.31417

    lst = client.get("/v1/array-operator/billing/subscriptions",
                     headers={"Authorization": auth}).json()
    row = next(x for x in lst["subscriptions"] if x["id"] == sub_id)
    assert row["allocation_pct"] == 0.24783
    assert row["array_share_pct"] == 0.31417

    # PATCH both shares to another 3-decimal value; the third decimal survives.
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"allocation_pct": 0.10057, "array_share_pct": 0.10057},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    s2 = r.json()["subscription"]
    assert s2["allocation_pct"] == 0.10057
    assert s2["array_share_pct"] == 0.10057


def test_manual_offtaker_with_unlinked_account_heals_the_link(client):
    """Bruce (2026-07-03): a freshly captured GMP account has array_id = NULL
    until it's matched to an array, so the add-offtaker bill picker (which
    filtered on array_id) rendered EMPTY even though every bill was downloaded.
    The UI now falls back to the tenant's full account list and passes the pick
    explicitly alongside array_id. The backend must:
      (a) accept utility_account_id for an account not yet linked to any array,
      (b) record the account -> array link so the next add resolves silently, and
      (c) keep the sub bound to the named array for list views (pre-fix it
          copied acct.array_id = NULL and the sub lost its group binding)."""
    from api.models import UtilityAccount
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid)
    with SessionLocal() as db:
        acct = UtilityAccount(tenant_id=tid, array_id=None, provider="gmp",
                              account_number="GMP-" + secrets.token_hex(2),
                              nickname="Fresh Capture")
        db.add(acct); db.flush()
        acct_id = acct.id
        db.commit()

    # The list the picker's fallback reads MUST include the unlinked account.
    lst = client.get("/v1/array-operator/billing/utility-accounts",
                     headers={"Authorization": auth}).json()
    row = next(u for u in lst["utility_accounts"]
               if u["utility_account_id"] == acct_id)
    assert row["array_id"] is None

    r = _create_manual(client, auth, customer_name="Fresh Link Co",
                       array_id=aid, utility_account_id=str(acct_id),
                       allocation_pct="0.25", client_email="fresh@example.test")
    assert r.status_code == 200, r.text
    sub = r.json()["subscription"]
    assert sub["utility_account_id"] == acct_id      # (a)
    assert sub["array_id"] == aid                    # (c)
    with SessionLocal() as db:
        assert db.get(UtilityAccount, acct_id).array_id == aid   # (b)


def test_manual_offtaker_never_relinks_an_account_bound_elsewhere(client):
    """Companion guard to the self-heal: an account EXPLICITLY linked to array Y
    stays linked to Y even when an offtaker is created naming array X with that
    account — the account's own binding wins (it also becomes the sub's
    array_id, matching how delivery resolves the bill)."""
    from api.models import UtilityAccount
    tid, auth = _make_tenant()
    aid_x = _make_array_with_generation(tid)
    with SessionLocal() as db:
        arr_y = Array(tenant_id=tid, name="Other Array", region="VT")
        db.add(arr_y); db.flush()
        aid_y = arr_y.id
        acct = UtilityAccount(tenant_id=tid, array_id=aid_y, provider="gmp",
                              account_number="GMP-" + secrets.token_hex(2),
                              nickname="Bound Elsewhere")
        db.add(acct); db.flush()
        acct_id = acct.id
        db.commit()

    r = _create_manual(client, auth, customer_name="No Relink Co",
                       array_id=aid_x, utility_account_id=str(acct_id),
                       allocation_pct="0.5", client_email="norelink@example.test")
    assert r.status_code == 200, r.text
    assert r.json()["subscription"]["array_id"] == aid_y
    with SessionLocal() as db:
        assert db.get(UtilityAccount, acct_id).array_id == aid_y


def test_patch_subscription_with_utility_account_id_succeeds(client):
    """REGRESSION: the edit-offtaker form re-sends utility_account_id on every save
    (the GMP bill picker is pre-selected to the current bill). patch_subscription
    referenced UtilityAccount WITHOUT importing it in scope → NameError → 500
    'Save failed.' for every edit of a GMP-bound offtaker. The PATCH must succeed
    and re-bind the bill (refreshing array_id from the account)."""
    from api.db import SessionLocal
    from api.models import Array, UtilityAccount
    tid, auth = _make_tenant()
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Shelburne", region="VT")
        db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="GMP-" + secrets.token_hex(2),
                              nickname="Shelburne GMP")
        db.add(acct); db.flush()
        acct_id = acct.id
        db.commit()
    sub_id = _create_manual(client, auth, customer_name="Rick Lunt",
                            utility_account_id=str(acct_id), allocation_pct="0.25",
                            client_email="rick@example.test").json()["subscription"]["id"]
    # Edit-save replays the same fields the form sends, incl. utility_account_id.
    r = client.patch(
        f"/v1/array-operator/billing/subscriptions/{sub_id}",
        json={"customer_name": "Rick Lunt", "utility_account_id": acct_id,
              "allocation_pct": 0.25, "discount_pct": 0.10, "cadence": "monthly"},
        headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub_id)
        assert s.utility_account_id == acct_id


def test_invoice_template_defaults_on_and_toggles_both_ways(client):
    """Uploading a template defaults it to ON (Ford: use their template by default).
    The PUT enable toggle must work BOTH ways — regression: put_invoice_template
    referenced an undefined `is_excel` (NameError -> 500 on every toggle) and force-
    re-enabled whenever html existed, so 'Default format' could never stick."""
    tid, auth = _make_tenant()
    files = {"file": ("invoice.html",
                      b"<html><body>Invoice {{amount_due}}</body></html>", "text/html")}
    r = client.post("/v1/array-operator/billing/invoice-template",
                    files=files, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert r.json()["template"]["enabled"] is True          # default ON on upload
    # Switch to the standard format — must succeed (was 500) and STICK (was bouncing on).
    r = client.put("/v1/array-operator/billing/invoice-template",
                   json={"enabled": False}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert r.json()["template"]["enabled"] is False
    # Switch back to the template.
    r = client.put("/v1/array-operator/billing/invoice-template",
                   json={"enabled": True}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert r.json()["template"]["enabled"] is True


# ─── scheduler ──────────────────────────────────────────────────────────────


def test_scheduler_monthly_billing_delivers(client, monkeypatch):
    """The scheduler job picks up THIS tenant's enabled monthly sub. With
    delivery_mode='auto' it sends straight to the recipient. (Asserts on our own
    sub id — the session-scoped test DB accumulates subs from other tests, so
    exact counts aren't meaningful.)"""
    from api import scheduler
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx", cadence="monthly",
                     delivery_mode="auto", send_mode="to_me").json()["subscription"]["id"]

    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: True)
    result = scheduler.deliver_billing_reports("monthly")
    assert sub_id in result["sent"]
    assert sub_id not in result["failed"]
    # And it stamped the schedule on our sub.
    with SessionLocal() as db:
        assert db.get(BillingReportSubscription, sub_id).last_sent_at is not None


def test_offtaker_send_bccs_the_operator(client, monkeypatch):
    """Every invoice emailed to an offtaker (send_mode=to_client) BCCs the operator,
    so the operator always gets a copy of exactly what the customer received — even
    though the message goes To the customer."""
    from api import scheduler
    tid, auth = _make_tenant()
    op_email = f"{tid}@operator.test"          # _make_tenant sets this as contact_email
    sub_id = _upload(client, auth, "norwich.xlsx", cadence="monthly",
                     delivery_mode="auto", send_mode="to_client",
                     client_email="offtaker@example.test").json()["subscription"]["id"]

    captured = {}

    def fake_send(to, subject, html, text, attachments=None, cc=None, bcc=None,
                  from_addr=None, reply_to=None, product="nepool", **kw):
        captured.update(to=to, cc=cc, bcc=bcc)
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    result = scheduler.deliver_billing_reports("monthly")
    assert sub_id in result["sent"]
    to_list = captured["to"] if isinstance(captured["to"], list) else [captured["to"]]
    assert "offtaker@example.test" in to_list      # the customer is the To
    assert captured["bcc"] == [op_email]           # the operator is BCC'd
    assert op_email not in to_list                 # operator is NOT a visible recipient


def test_offtaker_email_is_white_labeled_to_the_operator(client, monkeypatch):
    """The offtaker invoice email carries NO Array Operator branding — it's sent
    under the OPERATOR's name, replies route to the operator's inbox, and the
    footer + wordmark show the operator instead of Array Operator."""
    from api import scheduler
    tid, auth = _make_tenant()                     # _make_tenant names it "Billing Test Operator"
    op_email = f"{tid}@operator.test"
    sub_id = _upload(client, auth, "norwich.xlsx", cadence="monthly",
                     delivery_mode="auto", send_mode="to_client",
                     client_email="offtaker@example.test").json()["subscription"]["id"]
    cap = {}

    def fake_send(to, subject, html, text, attachments=None, cc=None, bcc=None,
                  from_addr=None, reply_to=None, product="nepool", **kw):
        cap.update(html=html, text=text, from_addr=from_addr, reply_to=reply_to)
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    assert sub_id in scheduler.deliver_billing_reports("monthly")["sent"]
    # No Array Operator branding anywhere the offtaker sees:
    assert "Array Operator" not in cap["html"]
    assert "arrayoperator.com" not in cap["html"]
    assert "admin@solaroperator.org" not in cap["html"]
    assert "Array Operator" not in cap["text"]
    # White-labeled to the operator + replies routed to them:
    assert "Billing Test Operator" in cap["html"]
    assert "Billing Test Operator" in (cap["from_addr"] or "")
    assert cap["reply_to"] == op_email


def test_budget_amount_overrides_calculated_total(client):
    """Budget billing: a per-offtaker fixed final amount overrides the calculated
    Amount Due (line items still compute), set via PATCH; clearing it restores it."""
    from api.billing import delivery
    from api.db import SessionLocal
    from api.models import BillingReportSubscription
    _B = "/v1/array-operator/billing"
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx").json()["subscription"]["id"]
    with SessionLocal() as db:
        base = (delivery.build_match(db.get(BillingReportSubscription, sub_id))
                .computed_invoice or {}).get("amount_owed")
    assert isinstance(base, (int, float)) and base != 250.0
    # Set the budget override.
    r = client.patch(f"{_B}/subscriptions/{sub_id}",
                     json={"budget_amount_usd": 250.0}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        ci = (delivery.build_match(db.get(BillingReportSubscription, sub_id))
              .computed_invoice or {})
    assert ci["amount_owed"] == 250.0 and ci.get("budget_override") is True
    # Clearing it restores the calculated total.
    client.patch(f"{_B}/subscriptions/{sub_id}",
                 json={"budget_amount_usd": None}, headers={"Authorization": auth})
    with SessionLocal() as db:
        ci = (delivery.build_match(db.get(BillingReportSubscription, sub_id))
              .computed_invoice or {})
    assert ci["amount_owed"] == base and not ci.get("budget_override")


def test_ai_email_uses_live_subscription_name_not_frozen_draft(client, monkeypatch):
    """The AI cover email must address the offtaker by their CURRENT name (from the
    subscription), not the frozen ReportDraft snapshot — which can still hold a
    since-corrected typo (the 'Brtuce' bug)."""
    import api.billing.repro.llm as LLM
    from api.models import ReportDraft
    _B = "/v1/array-operator/billing"
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx").json()["subscription"]["id"]
    client.post(f"{_B}/subscriptions/{sub_id}/draft", headers={"Authorization": auth})
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sub_id)
        sub.customer_name = "Bruce Genereaux"            # corrected name (lives on sub)
        d = (db.query(ReportDraft).filter_by(subscription_id=sub_id)
             .order_by(ReportDraft.id.desc()).first())
        d.customer_name = "Brtuce"                        # stale frozen snapshot
        draft_id = d.id
        db.commit()
    cap = {}
    def fake_call_json(*, system, user_text, **kw):
        cap["user_text"] = user_text
        return {"email": "Hello,"}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(LLM, "call_json", fake_call_json)
    r = client.post(f"{_B}/drafts/{draft_id}/ai-email", headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert "Bruce Genereaux" in cap["user_text"]
    assert "Brtuce" not in cap["user_text"]


# ─── billing rate ($/kWh): global default + per-customer override ────────────


def _math(client, auth, sub_id):
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sub_id}/preview-math",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    return r.json()


def test_rate_global_and_per_customer_override(client):
    """Legacy flat-rate back-compat under the discount model. A flat rate (per
    customer or global) is treated as a net rate with 0 discount, so the billed
    amount == kWh × flat_rate (unchanged dollars), and rate_source reflects the
    legacy_flat provenance. With NO rate set, the new default is 10% off the VT
    net rate."""
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid, kwh_per_day=100.0, days=30)  # 3000 kWh
    pct = 0.40
    cust_kwh = round(3000 * pct, 2)  # 1200.0

    sub_id = _create_manual(client, auth, customer_name="Rate Co", array_id=aid,
                            allocation_pct=str(pct), cadence="monthly",
                            send_mode="to_me").json()["subscription"]["id"]

    # A) no rate anywhere → default discount (10%) off the resolved VT net rate.
    #    No UtilityAccount on this array → provider default 0.21 (api/rates.py).
    a = _math(client, auth, sub_id)
    assert a["discount_source"] == "default"
    assert abs(a["discount_pct"] - 0.10) < 1e-9
    NET_DEFAULT = 0.21
    assert abs(a["net_rate_per_kwh"] - NET_DEFAULT) < 1e-6
    assert abs(a["effective_rate_per_kwh"] - round(NET_DEFAULT * 0.9, 6)) < 1e-6
    assert a["customer_kwh"] == cust_kwh
    # savings = kWh × net × discount
    assert abs(a["solar_savings_usd"] - round(cust_kwh * NET_DEFAULT * 0.10, 2)) < 0.02

    # B) legacy global flat rate 0.20 → billed at exactly 0.20 (0 discount).
    r = client.put("/v1/array-operator/billing/global-rate",
                   json={"default_billing_rate_per_kwh": 0.20},
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    b = _math(client, auth, sub_id)
    assert b["rate_source"] == "legacy_flat"
    assert abs(b["rate"] - 0.20) < 1e-9
    assert abs(b["amount_usd"] - round(cust_kwh * 0.20, 2)) < 0.01

    # C) per-customer legacy flat override wins over the global rate.
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"rate_per_kwh": 0.14},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert abs(r.json()["subscription"]["rate_per_kwh"] - 0.14) < 1e-9
    c = _math(client, auth, sub_id)
    assert abs(c["amount_usd"] - round(cust_kwh * 0.14, 2)) < 0.01

    # D) clearing the override (null) falls back to the global flat rate.
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"rate_per_kwh": None},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert r.json()["subscription"]["rate_per_kwh"] is None
    d = _math(client, auth, sub_id)
    assert abs(d["amount_usd"] - round(cust_kwh * 0.20, 2)) < 0.01


def test_discount_model_global_and_per_customer(client):
    """The discount billing model: invoice = kWh × net_rate × (1 − discount).
    Default 10% off; editable globally and per-customer; savings reported."""
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid, kwh_per_day=100.0, days=30)  # 3000 kWh
    pct = 0.50
    cust_kwh = round(3000 * pct, 2)  # 1500.0
    NET = 0.21   # no UtilityAccount → provider default net rate (api/rates.py)

    sub_id = _create_manual(client, auth, customer_name="Disc Co", array_id=aid,
                            allocation_pct=str(pct), cadence="monthly",
                            send_mode="to_me").json()["subscription"]["id"]

    # A) default 10% off the VT net rate.
    a = _math(client, auth, sub_id)
    assert abs(a["amount_usd"] - round(cust_kwh * NET * 0.90, 2)) < 0.02

    # B) set a GLOBAL discount of 25% (and an explicit global net rate of 0.20).
    r = client.put("/v1/array-operator/billing/global-rate",
                   json={"default_discount_pct": 0.25, "default_net_rate_per_kwh": 0.20},
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    b = _math(client, auth, sub_id)
    assert b["net_rate_source"] == "global"
    assert b["discount_source"] == "global"
    assert abs(b["discount_pct"] - 0.25) < 1e-9
    assert abs(b["amount_usd"] - round(cust_kwh * 0.20 * 0.75, 2)) < 0.02

    # C) per-customer discount override (40%) wins over the global 25%.
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"discount_pct": 0.40},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    c = _math(client, auth, sub_id)
    assert c["discount_source"] == "customer"
    assert abs(c["amount_usd"] - round(cust_kwh * 0.20 * 0.60, 2)) < 0.02
    assert abs(c["solar_savings_usd"] - round(cust_kwh * 0.20 * 0.40, 2)) < 0.02

    # D) clearing the per-customer discount falls back to the global 25%.
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"discount_pct": None},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    d = _math(client, auth, sub_id)
    assert d["discount_source"] == "global"
    assert abs(d["amount_usd"] - round(cust_kwh * 0.20 * 0.75, 2)) < 0.02

    # E) a discount ≥ 1 is rejected (would zero/inverse the bill).
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"discount_pct": 1.5},
                     headers={"Authorization": auth})
    assert r.status_code == 400


def test_rate_rejects_out_of_range(client):
    """A fat-fingered rate (negative or absurdly high) is rejected, so a units
    mistake can't silently produce a wild invoice."""
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid)
    r = _create_manual(client, auth, customer_name="Bad Rate", array_id=aid,
                       allocation_pct="0.25", rate_per_kwh="99")
    assert r.status_code == 400
    r = client.put("/v1/array-operator/billing/global-rate",
                   json={"default_billing_rate_per_kwh": -1},
                   headers={"Authorization": auth})
    assert r.status_code == 400


def test_kwh_source_prefers_gmp_else_falls_back(client):
    """Source-agnostic period generation: when GMP daily-read has coverage the
    invoice is sourced from it (kwh_source='gmp_api'); otherwise it falls back
    to DailyGeneration ('daily_csv'). Verified via the live preview-math route."""
    from api.models import Client as ClientM, Array, UtilityAccount, \
        DailyGeneration, GmpDailyGeneration
    from datetime import date, timedelta

    tid, auth = _make_tenant()
    # Array with a month of DailyGeneration (the fallback source).
    today = date.today()
    anchor = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    with SessionLocal() as db:
        c = ClientM(tenant_id=tid, name="Src Co", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="Src Array", client_id=c.id, fuel_type="solar")
        db.add(arr); db.flush()
        aid = arr.id
        for i in range(28):
            db.add(DailyGeneration(tenant_id=tid, array_id=aid,
                                   day=anchor + timedelta(days=i), kwh=50.0, source="csv"))
        db.commit()

    sid = _create_manual(client, auth, customer_name="Src Cust", array_id=aid,
                         allocation_pct="0.50", send_mode="to_me",
                         ).json()["subscription"]["id"]

    # No GMP rows yet → falls back to DailyGeneration.
    a = _math(client, auth, sid)
    assert a["kwh_source"] == "daily_csv"
    assert a["has_data"] is True

    # Now add GMP daily-read coverage for a DIFFERENT, later month via a GMP
    # utility account — the adapter must PREFER it.
    gmp_anchor = anchor  # same month is fine; distinct source table
    with SessionLocal() as db:
        ua = UtilityAccount(tenant_id=tid, array_id=aid, provider="gmp",
                            account_number="GMP-TEST-1", enabled=True)
        db.add(ua); db.flush()
        for i in range(28):
            db.add(GmpDailyGeneration(
                tenant_id=tid, account_id=ua.id, account_number="GMP-TEST-1",
                array_id=aid, day=gmp_anchor + timedelta(days=i),
                kwh=70.0, interval_count=96, source="gmp_api"))
        db.commit()

    b = _math(client, auth, sid)
    assert b["kwh_source"] == "gmp_api", b
    # GMP month total 28*70=1960 → 50% share = 980 kWh (distinct from the 50/day CSV).
    assert abs(b["array_total_kwh"] - 1960.0) < 1.0
    assert abs(b["customer_kwh"] - 980.0) < 1.0


def test_auto_attach_gmp_bill_when_captured_else_nothing(client, monkeypatch):
    """The auto-attach toggle: when a durable GMP bill PDF is captured for the
    array+period, it rides the email automatically; when none is captured,
    nothing is attached (never fabricated). Manual upload is unaffected."""
    import pathlib, tempfile
    from datetime import date as _date
    from api.billing import delivery
    from api.models import Client as ClientM, Array, BillingReportSubscription
    from api.billing.matcher import BillingMatch, Period

    tid, auth = _make_tenant()
    with SessionLocal() as db:
        c = ClientM(tenant_id=tid, name="Auto Co", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="Auto Array", client_id=c.id, fuel_type="solar")
        db.add(arr); db.flush()
        sub = BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name="Auto Cust",
            array_id=arr.id, allocation_pct=0.5, billing_model="percent_of_array",
            auto_attach_gmp=True, cadence="monthly", send_mode="to_me")
        db.add(sub); db.commit()
        sub_id, array_id = sub.id, arr.id

    period = Period(month="2026-05", start=_date(2026, 5, 1), end=_date(2026, 5, 31),
                    array_kwh=1000.0, customer_kwh=500.0)
    match = BillingMatch(
        matched=True, confidence=1.0, source="manual",
        customer={"name": "Auto Cust"}, billing_model="percent_of_array",
        periods=[period], latest_period=period,
        computed_invoice={"invoice_number": "2026-05", "period_start": "2026-05-01",
                          "period_end": "2026-05-31", "amount_owed": 100.0, "kwh": 500},
    )

    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sub_id)

        # 1) No captured PDF → nothing attached (read seam returns None today).
        #    formats=[] so only the GMP-attach branch runs (no invoice render).
        with tempfile.TemporaryDirectory() as tmp:
            paths = delivery.generate_files(match, [], False,
                                            pathlib.Path(tmp), sub=sub)
            assert not any("gmp_utility_bill" in p.name for p in paths)

        # 2) Simulate ingestion having landed a durable PDF → auto-attached.
        monkeypatch.setattr(
            "api.reports.gmp_bill_pdf_read.get_bill_pdf_for_period",
            lambda aid, ps=None, pe=None, **kw: {
                "bytes": b"%PDF-1.4\nGMP bill\n", "filename": "GMP_bill_2026-05.pdf",
                "content_type": "application/pdf", "account_id": 1,
                "period_start": None, "period_end": None})
        with tempfile.TemporaryDirectory() as tmp:
            paths = delivery.generate_files(match, [], False,
                                            pathlib.Path(tmp), sub=sub)
            assert any(p.name.startswith("gmp_utility_bill_") and p.name.endswith(".pdf")
                       for p in paths), [p.name for p in paths]
