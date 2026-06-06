"""
Tests for per-row provenance, collision detection, and warning generation
in the /v1/ingest/preview endpoint.

Strategy: mock _llm_extract_hierarchical to return known JSON, create a
tenant with pre-seeded clients/arrays, verify the provenance fields the
endpoint attaches to each row.
"""
from __future__ import annotations

import io
import secrets
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.db import SessionLocal
from api.models import Tenant, Client, Array
from api.account import mint_session_for_tenant


# ─── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def authed_seeded(client):
    """Tenant with one existing client ('Alice Moreau') and one array
    ('Moreau Main', nepool_gis_id='10001') for collision tests."""
    tenant_id = f"ten_{secrets.token_hex(8)}"
    with SessionLocal() as db:
        db.add(Tenant(
            id=tenant_id,
            name="Provenance Test Op",
            contact_email=f"prov-{secrets.token_hex(4)}@example.com",
            tenant_key=f"sol_live_{secrets.token_urlsafe(18)}",
            plan="comped",
            active=True,
        ))
        db.flush()
        existing_client = Client(
            tenant_id=tenant_id,
            name="Alice Moreau",
            active=True,
        )
        db.add(existing_client)
        db.flush()
        db.add(Array(
            tenant_id=tenant_id,
            client_id=existing_client.id,
            name="Moreau Main",
            nepool_gis_id="10001",
        ))
        db.commit()

    token = mint_session_for_tenant(tenant_id)
    return client, f"Bearer {token}"


def _simple_xlsx(rows: list[dict]) -> bytes:
    """Build a plain roster workbook from a list of row dicts."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Roster"
    ws.append(["Client", "Array Name", "NEPOOL ID", "Account #"])
    for r in rows:
        ws.append([
            r.get("operator_name") or "",
            r.get("array_name") or "",
            r.get("nepool_gis_id") or "",
            r.get("gmp_account_number") or "",
        ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _hierarchy(client_name: str, arrays: list[dict]) -> dict:
    """Build a minimal LLM hierarchy response with a single client."""
    return {
        "operators": [{
            "name": "TestOp",
            "clients": [{
                "name": client_name,
                "logins": [{
                    "utility": "gmp",
                    "login_email": None,
                    "accounts": [{
                        "account_number": None,
                        "arrays": arrays,
                    }],
                }],
            }],
        }]
    }


def _call(authed_seeded, xlsx_bytes: bytes):
    c, auth = authed_seeded
    return c.post(
        "/v1/ingest/preview",
        files={"file": ("roster.xlsx", xlsx_bytes,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers={"Authorization": auth},
    )


# ─── Tests: provenance fields ─────────────────────────────────────────────────

class TestProvenanceFields:
    def test_llm_row_has_provenance_with_confidence(self, authed_seeded):
        """LLM rows carry provenance.source='llm' and the confidence from the JSON."""
        xlsx = _simple_xlsx([{"operator_name": "Test Client", "array_name": "Test Array", "nepool_gis_id": "99999"}])
        hier = _hierarchy("Test Client", [{"name": "Test Array", "nepool_gis_id": "99999", "notes": None, "confidence": 0.95}])

        with patch("api.ingest._llm_extract_hierarchical", return_value=hier):
            resp = _call(authed_seeded, xlsx)

        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "llm"
        row = body["arrays"][0]
        assert "provenance" in row
        prov = row["provenance"]
        assert prov["source"] == "llm"
        assert prov["confidence"] == pytest.approx(0.95)
        assert prov["nepool_collision"] is None

    def test_missing_confidence_backfills_to_085(self, authed_seeded):
        """When LLM returns no confidence field, endpoint backfills to 0.85."""
        xlsx = _simple_xlsx([{"operator_name": "X", "array_name": "Y", "nepool_gis_id": "99998"}])
        # No 'confidence' key in the array dict — simulates old-prompt response.
        hier = _hierarchy("X", [{"name": "Y", "nepool_gis_id": "99998", "notes": None}])

        with patch("api.ingest._llm_extract_hierarchical", return_value=hier):
            resp = _call(authed_seeded, xlsx)

        row = resp.json()["arrays"][0]
        assert row["provenance"]["confidence"] == pytest.approx(0.85)

    def test_nepool_collision_detected(self, authed_seeded):
        """Row whose nepool_gis_id matches an existing Array gets nepool_collision set."""
        xlsx = _simple_xlsx([{"operator_name": "New Client", "array_name": "New Array", "nepool_gis_id": "10001"}])
        hier = _hierarchy("New Client", [{"name": "New Array", "nepool_gis_id": "10001", "notes": None, "confidence": 0.90}])

        with patch("api.ingest._llm_extract_hierarchical", return_value=hier):
            resp = _call(authed_seeded, xlsx)

        assert resp.status_code == 200
        body = resp.json()
        row = body["arrays"][0]
        nc = row["provenance"]["nepool_collision"]
        assert nc is not None
        assert nc["existing_array_name"] == "Moreau Main"
        assert nc["existing_client_name"] == "Alice Moreau"

    def test_exact_client_match(self, authed_seeded):
        """Exact operator_name match gives client_match.match_kind='exact'."""
        xlsx = _simple_xlsx([{"operator_name": "Alice Moreau", "array_name": "New Array 2", "nepool_gis_id": "99997"}])
        hier = _hierarchy("Alice Moreau", [{"name": "New Array 2", "nepool_gis_id": "99997", "notes": None, "confidence": 0.95}])

        with patch("api.ingest._llm_extract_hierarchical", return_value=hier):
            resp = _call(authed_seeded, xlsx)

        row = resp.json()["arrays"][0]
        cm = row["provenance"]["client_match"]
        assert cm is not None
        assert cm["match_kind"] == "exact"
        assert cm["client_name"] == "Alice Moreau"

    def test_fuzzy_client_match(self, authed_seeded):
        """'Alice Moreau Jr' fuzzy-matches 'Alice Moreau' at >= 0.85."""
        xlsx = _simple_xlsx([{"operator_name": "Alice Moreau Jr", "array_name": "Fuzzy Farm", "nepool_gis_id": "99996"}])
        hier = _hierarchy("Alice Moreau Jr", [{"name": "Fuzzy Farm", "nepool_gis_id": "99996", "notes": None, "confidence": 0.90}])

        with patch("api.ingest._llm_extract_hierarchical", return_value=hier):
            resp = _call(authed_seeded, xlsx)

        row = resp.json()["arrays"][0]
        cm = row["provenance"]["client_match"]
        assert cm is not None
        assert cm["match_kind"] == "fuzzy"
        # "alice moreau jr" vs "alice moreau" → ratio ≈ 0.889 ≥ 0.85
        assert cm["client_name"] == "Alice Moreau"

    def test_no_match_for_completely_different_name(self, authed_seeded):
        """Completely different name yields no client_match."""
        xlsx = _simple_xlsx([{"operator_name": "Zephyr Wind Farm LLC", "array_name": "Wind1", "nepool_gis_id": "99995"}])
        hier = _hierarchy("Zephyr Wind Farm LLC", [{"name": "Wind1", "nepool_gis_id": "99995", "notes": None, "confidence": 0.90}])

        with patch("api.ingest._llm_extract_hierarchical", return_value=hier):
            resp = _call(authed_seeded, xlsx)

        row = resp.json()["arrays"][0]
        assert row["provenance"]["client_match"] is None


# ─── Tests: warnings ──────────────────────────────────────────────────────────

class TestWarnings:
    def test_empty_file_warning_http_200(self, authed_seeded):
        """File with no data rows returns HTTP 200 with empty_file warning."""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Client", "Array Name"])  # header only, no data rows
        buf = io.BytesIO()
        wb.save(buf)
        xlsx = buf.getvalue()

        with patch("api.ingest._llm_extract_hierarchical", return_value={"operators": []}):
            resp = _call(authed_seeded, xlsx)

        assert resp.status_code == 200
        body = resp.json()
        assert body["arrays"] == []
        kinds = [w["kind"] for w in body["warnings"]]
        assert "empty_file" in kinds

    def test_nepool_collision_warning_in_summary(self, authed_seeded):
        """When nepool_collision rows exist, warnings includes nepool_collision entry."""
        xlsx = _simple_xlsx([{"operator_name": "X", "array_name": "Y", "nepool_gis_id": "10001"}])
        hier = _hierarchy("X", [{"name": "Y", "nepool_gis_id": "10001", "notes": None, "confidence": 0.92}])

        with patch("api.ingest._llm_extract_hierarchical", return_value=hier):
            resp = _call(authed_seeded, xlsx)

        body = resp.json()
        kinds = [w["kind"] for w in body["warnings"]]
        assert "nepool_collision" in kinds
        nc_warn = next(w for w in body["warnings"] if w["kind"] == "nepool_collision")
        assert nc_warn["count"] == 1

    def test_low_confidence_warning(self, authed_seeded):
        """Rows with confidence < 0.85 trigger low_confidence_rows warning."""
        xlsx = _simple_xlsx([{"operator_name": "A", "array_name": "B", "nepool_gis_id": "99994"}])
        hier = _hierarchy("A", [{"name": "B", "nepool_gis_id": "99994", "notes": None, "confidence": 0.60}])

        with patch("api.ingest._llm_extract_hierarchical", return_value=hier):
            resp = _call(authed_seeded, xlsx)

        kinds = [w["kind"] for w in resp.json()["warnings"]]
        assert "low_confidence_rows" in kinds


# ─── Tests: backwards compat ──────────────────────────────────────────────────

class TestBackwardsCompat:
    def test_old_response_keys_present(self, authed_seeded):
        """All original response keys must still be present — regression guard."""
        xlsx = _simple_xlsx([{"operator_name": "T", "array_name": "U", "nepool_gis_id": "99993"}])
        hier = _hierarchy("T", [{"name": "U", "nepool_gis_id": "99993", "notes": None, "confidence": 0.95}])

        with patch("api.ingest._llm_extract_hierarchical", return_value=hier):
            resp = _call(authed_seeded, xlsx)

        assert resp.status_code == 200
        body = resp.json()
        # Top-level keys
        for key in ("arrays", "source", "count", "imported_logins", "imported_clients"):
            assert key in body, f"missing top-level key: {key}"
        # Per-row keys
        row = body["arrays"][0]
        for key in ("array_name", "operator_name", "nepool_gis_id", "gmp_account_number", "notes", "collision"):
            assert key in row, f"missing per-row key: {key}"

    def test_heuristic_source_has_null_confidence(self, authed_seeded):
        """Heuristic rows have provenance.source='heuristic' and confidence=null."""
        xlsx = _simple_xlsx([{"operator_name": "Col Op", "array_name": "Col Array", "nepool_gis_id": "99992"}])

        with patch("api.ingest._llm_extract_hierarchical", return_value=None):
            resp = _call(authed_seeded, xlsx)

        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "heuristic"
        # Heuristic rows get provenance but confidence is null
        for row in body["arrays"]:
            prov = row.get("provenance")
            assert prov is not None
            assert prov["confidence"] is None
