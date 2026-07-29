"""Reading the household's own planning spreadsheet."""
import pytest

from bankai import config
from bankai.connectors import sheets
from bankai.ingest import upsert_account

CSV = """Date,Expense,Type,,Gaurav End Running Balance,Off by,Gaurav In,Gaurav Out,Total Cash In Hand,Stocks,Ford Running Balance,Off by,Ford In,Ford Out,Apple Card Payment,Apple Card Projection,Other CC,AC Off 2,Excess
02/15/2026,,,,2722,,,,23741.78,,21019.78,,,,,,7626,,23741.78
07/29/2026,Groceries,food,,"8,049",,,,"17,086",,"9,037",,,,,"(15,340)","(1,800)",,-54
07/31/2026,,,,7764,,,285,16801,,9037,,,,,,,,-533
,,,,,,,,,,,,,,,,,,
not a date,,,,1,,,,1,,1,,,,,,,,1
"""


@pytest.fixture(autouse=True)
def sheet_configured(monkeypatch):
    monkeypatch.setattr(config, "SHEETS_ID", "test-sheet")
    monkeypatch.setattr(config, "SHEETS_GID", "")
    monkeypatch.setattr(config, "SHEETS_SERVICE_ACCOUNT_JSON", "")


def test_parses_us_dates_and_money():
    data = sheets.parse(CSV)
    rows = {r["date"]: r for r in data["rows"]}
    assert set(rows) == {"2026-02-15", "2026-07-29", "2026-07-31"}
    today = rows["2026-07-29"]
    assert today["total_cash"] == 17086.0        # comma thousands
    assert today["apple_card_projection"] == -15340.0  # parenthesised negative
    assert today["other_cc"] == -1800.0
    assert today["excess"] == -54.0
    assert today["expense"] == "Groceries" and today["type"] == "food"


def test_blank_and_malformed_rows_are_skipped():
    data = sheets.parse(CSV)
    assert len(data["rows"]) == 3  # blank row and "not a date" dropped


def test_empty_cells_become_none_not_zero():
    """A missing figure is unknown, not zero — treating it as zero would put
    invented numbers into their plan."""
    rows = {r["date"]: r for r in sheets.parse(CSV)["rows"]}
    assert rows["2026-07-29"]["gaurav_in"] is None
    assert rows["2026-07-31"]["gaurav_out"] == 285.0


def test_unknown_columns_are_reported_not_dropped():
    data = sheets.parse("Date,Total Cash In Hand,Crypto Wallet\n07/29/2026,100,5\n")
    assert data["unmapped_columns"] == ["Crypto Wallet"]


def test_parse_handles_an_empty_sheet():
    assert sheets.parse("")["rows"] == []


def test_writing_is_off_until_a_service_account_exists(monkeypatch):
    assert sheets.configured() is True
    assert sheets.can_write() is False
    monkeypatch.setattr(config, "SHEETS_SERVICE_ACCOUNT_JSON", "/path/to/key.json")
    assert sheets.can_write() is True


def test_read_plan_splits_past_from_future(monkeypatch):
    monkeypatch.setattr(sheets, "fetch_csv", lambda gid=None: CSV)
    plan = sheets.read_plan()
    assert plan["total_rows"] == 3
    assert plan["writable"] is False
    assert "not configured yet" in plan["note"]
    dates = [r["date"] for r in plan["recent"]] + [r["date"] for r in plan["upcoming"]]
    assert dates == sorted(dates)


def test_reconcile_names_the_gap_against_real_accounts(session, monkeypatch):
    monkeypatch.setattr(sheets, "fetch_csv", lambda gid=None: CSV)
    upsert_account(session, source="simplefin", name="Checking", kind="checking",
                   balance=20_109.18)
    upsert_account(session, source="simplefin", name="Savings", kind="savings", balance=0.37)
    # investments must not count as spendable cash
    upsert_account(session, source="simplefin", name="Brokerage", kind="investment",
                   balance=500_000.0)
    session.flush()
    out = sheets.reconcile(session)
    assert out["live_liquid_total"] == 20_109.55
    assert out["difference_vs_sheet_cash"] == pytest.approx(3_023.55, abs=0.01)
    assert "disagree" in out["interpretation"]


def test_reconcile_says_agree_when_they_match(session, monkeypatch):
    monkeypatch.setattr(sheets, "fetch_csv", lambda gid=None: CSV)
    upsert_account(session, source="simplefin", name="Checking", kind="checking",
                   balance=17_086.0)
    session.flush()
    out = sheets.reconcile(session)
    assert "agree" in out["interpretation"]


# --- writing: only ever into our own tab ---

def test_write_refuses_without_a_key(session, monkeypatch):
    monkeypatch.setattr(config, "SHEETS_SERVICE_ACCOUNT_JSON", "")
    with pytest.raises(RuntimeError, match="SHEETS_SERVICE_ACCOUNT_JSON"):
        sheets.write_actuals(session)


def test_write_targets_the_dedicated_tab_never_the_planning_columns(session, monkeypatch):
    """Their workbook is full of formulas whose computed values are all we can
    see. Writing into those columns would destroy the formula with no undo, so
    every write must be confined to the copilot's own tab."""
    monkeypatch.setattr(config, "SHEETS_SERVICE_ACCOUNT_JSON", "/fake/key.json")
    monkeypatch.setattr(sheets, "ensure_actuals_tab", lambda: None)
    calls = []

    def fake_api(method, path, **kwargs):
        calls.append({"method": method, "path": path, **kwargs})
        return {}

    monkeypatch.setattr(sheets, "_api", fake_api)
    upsert_account(session, source="simplefin", name="Checking", kind="checking",
                   balance=20_109.18)
    upsert_account(session, source="manual", name="Mortgage", kind="mortgage",
                   balance=-1_226_301.14)
    session.flush()

    out = sheets.write_actuals(session)
    assert out["written"] is True
    write = [c for c in calls if c["method"] == "PUT"][0]
    assert write["path"].startswith(f"/values/{sheets.ACTUALS_TAB}!")
    assert write["params"]["valueInputOption"] == "USER_ENTERED"

    values = write["json"]["values"]
    flat = [str(cell) for row in values for cell in row]
    assert "NET WORTH" in flat
    assert "Checking" in flat and "Mortgage" in flat
    assert "do not edit by hand" in values[0][0]


def test_written_totals_group_accounts_correctly(session, monkeypatch):
    monkeypatch.setattr(config, "SHEETS_SERVICE_ACCOUNT_JSON", "/fake/key.json")
    monkeypatch.setattr(sheets, "ensure_actuals_tab", lambda: None)
    captured = {}

    def fake_api(method, path, **kw):
        if method == "PUT":
            captured.update(kw)
        return {}

    monkeypatch.setattr(sheets, "_api", fake_api)
    upsert_account(session, source="simplefin", name="Checking", kind="checking", balance=1_000.0)
    upsert_account(session, source="simplefin", name="Savings", kind="savings", balance=500.0)
    upsert_account(session, source="simplefin", name="Card", kind="credit", balance=-250.0)
    session.flush()
    sheets.write_actuals(session)
    rows = {r[0]: r[1] for r in captured["json"]["values"] if len(r) > 1}
    assert rows["Cash (checking + savings)"] == 1_500.0
    assert rows["Credit cards owed"] == -250.0
    assert rows["NET WORTH"] == 1_250.0


# --- Apps Script transport (works where service-account keys are blocked) ---

def test_apps_script_url_alone_enables_writing(monkeypatch):
    monkeypatch.setattr(config, "SHEETS_SERVICE_ACCOUNT_JSON", "")
    monkeypatch.setattr(config, "SHEETS_WEBHOOK_URL", "https://script.google.com/x/exec")
    assert sheets.can_write() is True


def test_apps_script_is_preferred_over_the_service_account(session, monkeypatch):
    monkeypatch.setattr(config, "SHEETS_WEBHOOK_URL", "https://script.google.com/x/exec")
    monkeypatch.setattr(config, "SHEETS_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(config, "SHEETS_SERVICE_ACCOUNT_JSON", "/fake/key.json")
    monkeypatch.setattr(
        sheets, "_api",
        lambda *a, **k: pytest.fail("must not hit the Sheets API when a script exists"),
    )
    posted = {}

    class FakeResponse:
        text = "ok: wrote 12 rows to BankAI Actuals"

        def raise_for_status(self):
            return None

    def fake_post(url, json=None, timeout=None, follow_redirects=None):
        posted.update({"url": url, "body": json})
        return FakeResponse()

    monkeypatch.setattr(sheets.httpx, "post", fake_post)
    upsert_account(session, source="simplefin", name="Checking", kind="checking", balance=10.0)
    session.flush()

    out = sheets.write_actuals(session)
    assert out["transport"] == "apps_script"
    assert posted["body"]["secret"] == "s3cret"
    assert posted["body"]["tab"] == sheets.ACTUALS_TAB
    assert any("NET WORTH" in str(cell) for row in posted["body"]["values"] for cell in row)


def test_a_wrong_secret_is_reported_as_a_secret_problem(monkeypatch):
    monkeypatch.setattr(config, "SHEETS_WEBHOOK_URL", "https://script.google.com/x/exec")
    monkeypatch.setattr(config, "SHEETS_WEBHOOK_SECRET", "wrong")

    class FakeResponse:
        text = "unauthorized"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(sheets.httpx, "post", lambda *a, **k: FakeResponse())
    with pytest.raises(RuntimeError, match="secret"):
        sheets._write_via_apps_script("BankAI Actuals", [["x"]])


def test_a_login_page_response_explains_the_deployment_setting(monkeypatch):
    """The classic Apps Script mistake is deploying with restricted access; the
    symptom is an HTML sign-in page, which is useless unless we name the fix."""
    monkeypatch.setattr(config, "SHEETS_WEBHOOK_URL", "https://script.google.com/x/exec")

    class FakeResponse:
        text = "<!DOCTYPE html><html><head><title>Sign in</title>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(sheets.httpx, "post", lambda *a, **k: FakeResponse())
    with pytest.raises(RuntimeError, match="Anyone"):
        sheets._write_via_apps_script("BankAI Actuals", [["x"]])
