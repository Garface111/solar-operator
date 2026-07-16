"""Sovereign expansion powers — unit tests (no live API keys required)."""
from __future__ import annotations

import json

from api.energy_agent_sovereign_expand import (
    code_sandbox_run,
    extract_pdf_text,
    parse_har_object,
    enrich_attachment,
    browser_fetch_public,
)


def test_code_sandbox_runs_python():
    res = code_sandbox_run("print(2 + 40)\nprint('ok')")
    assert res.get("ok") is True
    assert "42" in (res.get("stdout") or "")
    assert "ok" in (res.get("stdout") or "")


def test_code_sandbox_blocks_rm_rf():
    res = code_sandbox_run("import os\nos.system('rm -rf /')")
    assert res.get("ok") is False
    assert res.get("denied") or "blocked" in str(res.get("denied_reason") or res.get("error") or "").lower() or res.get("returncode") != 0


def test_parse_har_interesting_endpoints():
    har = {
        "log": {
            "entries": [
                {
                    "request": {"method": "POST", "url": "https://portal.example.com/api/login"},
                    "response": {"status": 200, "content": {"mimeType": "application/json"}},
                },
                {
                    "request": {"method": "GET", "url": "https://cdn.example.com/logo.png"},
                    "response": {"status": 200, "content": {"mimeType": "image/png"}},
                },
                {
                    "request": {"method": "GET", "url": "https://portal.example.com/api/usage"},
                    "response": {"status": 200, "content": {"mimeType": "application/json"}},
                },
            ]
        }
    }
    out = parse_har_object(har)
    assert out["endpoint_count"] >= 2
    assert "portal.example.com" in out["hosts"]
    interesting_urls = " ".join(e["url"] for e in out["interesting"])
    assert "login" in interesting_urls or "usage" in interesting_urls


def test_enrich_json_attachment():
    raw = json.dumps({"account": "12345", "kWh": 12.5}).encode()
    enr = enrich_attachment("bill.json", "application/json", raw, do_vision=False)
    assert enr["kind"] == "json"
    assert enr["structured"].get("account") == "12345" or "account" in enr["text"]


def test_enrich_har_attachment():
    har = {
        "log": {
            "entries": [
                {
                    "request": {"method": "GET", "url": "https://x.test/api/meter"},
                    "response": {"status": 200, "content": {}},
                }
            ]
        }
    }
    enr = enrich_attachment("cap.har", "application/json", json.dumps(har).encode(), do_vision=False)
    assert enr["kind"] == "har"
    assert enr["structured"].get("endpoint_count", 0) >= 1


def test_browser_blocks_localhost():
    res = browser_fetch_public("http://127.0.0.1/secret")
    assert res.get("ok") is False
    assert res.get("denied") is True


def test_pdf_extract_empty_is_safe():
    assert extract_pdf_text(b"") == ""
