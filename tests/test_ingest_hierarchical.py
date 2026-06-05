"""
Round-trip tests for the hierarchical ingest pipeline.

Strategy:
1. Build spreadsheets in-memory with openpyxl from canonical reference examples.
2. Exercise flatten/count helpers directly (no LLM needed).
3. Mock _llm_extract_hierarchical to return a reference example, then call the
   preview endpoint and verify the flat rows match what flatten produces.
"""
from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.import_examples import (
    EXAMPLE_GMCS_STYLE,
    EXAMPLE_RESIDENTIAL_PORTFOLIO,
    EXAMPLE_SPARSE_MESSY,
    EXAMPLE_MIXED_VEC_GMP,
)
from api.ingest import (
    _flatten_hierarchy_to_rows,
    _flatten_hierarchy_to_pairs,
    _count_logins,
    _count_clients,
)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _wrap(example: dict) -> dict:
    """Wrap a single example in the {operators: [...]} shape the LLM returns."""
    return {"operators": [example]}


def _build_xlsx_from_example(example: dict) -> bytes:
    """Build a simple .xlsx roster from a reference example using openpyxl.

    Produces: headers [Client, Login Email, Account #, Array Name, NEPOOL ID, Notes]
    One row per array across all logins/accounts.
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Roster"
    ws.append(["Client", "Login Email", "Account #", "Array Name", "NEPOOL ID", "Notes"])
    for client in example.get("clients", []):
        client_name = client.get("name", "")
        for login in client.get("logins", []):
            email = login.get("login_email", "")
            for account in login.get("accounts", []):
                acct = account.get("account_number", "") or ""
                for array in account.get("arrays", []):
                    ws.append([
                        client_name,
                        email,
                        acct,
                        array.get("name", ""),
                        array.get("nepool_gis_id", "") or "",
                        array.get("notes", "") or "",
                    ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ─── unit tests on flatten/count helpers ─────────────────────────────────────

class TestFlattenHierarchyToRows:
    def test_gmcs_style_row_count(self):
        data = _wrap(EXAMPLE_GMCS_STYLE)
        rows = _flatten_hierarchy_to_rows(data)
        # GMCS example: Tannery 3 arrays + Starlake 4 arrays = 7 total
        assert len(rows) == 7

    def test_residential_row_count(self):
        data = _wrap(EXAMPLE_RESIDENTIAL_PORTFOLIO)
        rows = _flatten_hierarchy_to_rows(data)
        # 1 + 2 + 1 + 1 = 5
        assert len(rows) == 5

    def test_all_rows_have_array_name(self):
        for example in [EXAMPLE_GMCS_STYLE, EXAMPLE_RESIDENTIAL_PORTFOLIO]:
            rows = _flatten_hierarchy_to_rows(_wrap(example))
            for r in rows:
                assert r["array_name"], f"row missing array_name: {r}"

    def test_operator_name_mapped_to_client(self):
        data = _wrap(EXAMPLE_RESIDENTIAL_PORTFOLIO)
        rows = _flatten_hierarchy_to_rows(data)
        client_names = {r["operator_name"] for r in rows}
        assert "Alice Moreau" in client_names
        assert "Robert Chagnon" in client_names

    def test_gmp_account_number_populated(self):
        data = _wrap(EXAMPLE_GMCS_STYLE)
        rows = _flatten_hierarchy_to_rows(data)
        acct_rows = [r for r in rows if r.get("gmp_account_number")]
        assert len(acct_rows) > 0, "expected at least one row with gmp_account_number"

    def test_vec_account_not_put_in_gmp_field(self):
        data = _wrap(EXAMPLE_MIXED_VEC_GMP)
        rows = _flatten_hierarchy_to_rows(data)
        # CVO East Field is on a VEC login — its account_number should NOT appear
        # in gmp_account_number.
        vec_row = next(r for r in rows if r["array_name"] == "CVO East Field")
        assert vec_row["gmp_account_number"] is None

    def test_missing_nepool_id_is_null(self):
        data = _wrap(EXAMPLE_SPARSE_MESSY)
        rows = _flatten_hierarchy_to_rows(data)
        benoit_farm = next(r for r in rows if r["array_name"] == "Benoit Farm")
        assert benoit_farm["nepool_gis_id"] is None

    def test_empty_operators_returns_empty(self):
        assert _flatten_hierarchy_to_rows({"operators": []}) == []

    def test_malformed_data_returns_empty(self):
        assert _flatten_hierarchy_to_rows({}) == []


class TestFlattenHierarchyToPairs:
    def test_only_populated_nepool_ids(self):
        data = _wrap(EXAMPLE_SPARSE_MESSY)
        pairs = _flatten_hierarchy_to_pairs(data)
        ids = {p["nepool_gis_id"] for p in pairs}
        # Benoit Farm and Rutland Beta and Benoit LLC have no NEPOOL ID
        assert None not in ids
        assert "" not in ids

    def test_gmcs_pairs_count(self):
        data = _wrap(EXAMPLE_GMCS_STYLE)
        pairs = _flatten_hierarchy_to_pairs(data)
        # Starlake West has no NEPOOL ID; 6 out of 7 arrays have IDs
        assert len(pairs) == 6


class TestCountHelpers:
    def test_count_logins_gmcs(self):
        data = _wrap(EXAMPLE_GMCS_STYLE)
        # 2 clients, each with 1 login
        assert _count_logins(data) == 2

    def test_count_logins_sparse_messy(self):
        data = _wrap(EXAMPLE_SPARSE_MESSY)
        # Benoit 3 + Rutland 1 + Johnson 1 = 5
        assert _count_logins(data) == 5

    def test_count_logins_mixed(self):
        data = _wrap(EXAMPLE_MIXED_VEC_GMP)
        # Champlain 2 (gmp+vec) + Hardwick 1 = 3
        assert _count_logins(data) == 3

    def test_count_clients_residential(self):
        data = _wrap(EXAMPLE_RESIDENTIAL_PORTFOLIO)
        assert _count_clients(data) == 4

    def test_count_clients_zero(self):
        assert _count_clients({"operators": []}) == 0


# ─── endpoint round-trip test (mocked LLM) ───────────────────────────────────

@pytest.fixture()
def authed_client(client):
    """Add a tenant + session token so the ingest endpoint accepts the call."""
    import secrets
    from api.account import mint_session_for_tenant
    from api.db import SessionLocal
    from api.models import Tenant

    tenant_id = f"ten_{secrets.token_hex(8)}"
    with SessionLocal() as db:
        db.add(Tenant(
            id=tenant_id,
            name="Test Operator",
            contact_email="ingest-test@example.com",
            tenant_key=f"sol_live_{secrets.token_urlsafe(18)}",
            plan="comped",
            active=True,
        ))
        db.commit()

    session_token = mint_session_for_tenant(tenant_id)
    return client, f"Bearer {session_token}"


def _call_preview(authed_client, xlsx_bytes: bytes):
    c, auth = authed_client
    return c.post(
        "/v1/ingest/preview",
        files={"file": ("roster.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers={"Authorization": auth},
    )


class TestPreviewEndpointRoundTrip:
    def test_residential_round_trip(self, authed_client):
        xlsx = _build_xlsx_from_example(EXAMPLE_RESIDENTIAL_PORTFOLIO)
        hierarchy = _wrap(EXAMPLE_RESIDENTIAL_PORTFOLIO)
        expected_rows = _flatten_hierarchy_to_rows(hierarchy)

        with patch("api.ingest._llm_extract_hierarchical", return_value=hierarchy):
            resp = _call_preview(authed_client, xlsx)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["source"] == "llm"
        assert body["count"] == len(expected_rows)
        # imported_logins: 4 clients × 1 login each = 4
        assert body["imported_logins"] == 4
        assert body["imported_clients"] == 4

        returned_names = {r["array_name"] for r in body["arrays"]}
        expected_names = {r["array_name"] for r in expected_rows}
        assert returned_names == expected_names

    def test_sparse_messy_round_trip(self, authed_client):
        xlsx = _build_xlsx_from_example(EXAMPLE_SPARSE_MESSY)
        hierarchy = _wrap(EXAMPLE_SPARSE_MESSY)
        expected_rows = _flatten_hierarchy_to_rows(hierarchy)

        with patch("api.ingest._llm_extract_hierarchical", return_value=hierarchy):
            resp = _call_preview(authed_client, xlsx)

        assert resp.status_code == 200
        body = resp.json()
        assert body["imported_logins"] == 5  # Benoit 3 + Rutland 1 + Johnson 1
        assert body["count"] == len(expected_rows)

        # Arrays with null NEPOOL IDs must still appear in results
        names = [r["array_name"] for r in body["arrays"]]
        assert "Benoit Farm" in names
        assert "Rutland Array Beta" in names

    def test_heuristic_fallback_returns_zero_logins(self, authed_client):
        xlsx = _build_xlsx_from_example(EXAMPLE_RESIDENTIAL_PORTFOLIO)

        with patch("api.ingest._llm_extract_hierarchical", return_value=None):
            resp = _call_preview(authed_client, xlsx)

        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "heuristic"
        assert body["imported_logins"] == 0
        assert body["imported_clients"] == 0
