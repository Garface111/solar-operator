"""Regressions for bugs surfaced by NEPOOL Operator ops-alert emails."""
import importlib


def test_content_disposition_is_latin1_safe():
    """A customer name with an em-dash (or any non-latin-1 char) must not crash
    the download — HTTP headers are latin-1. Regression for the recurring
    'UnicodeEncodeError on /subscriptions/{id}/preview' 500."""
    routes = importlib.import_module("api.billing.routes")
    cd = routes._content_disposition_attachment
    for name in ["Untitled client 2 \u2014 report_invoice.xlsx",
                 "Bruce Genereaux_invoice.xlsx", 'has"quote', "caf\u00e9 \u2615", "", None]:
        header = cd(name)
        header.encode("latin-1")  # must not raise
        assert header.startswith("attachment; filename=")
        assert "filename*=UTF-8''" in header
    # the real UTF-8 name is preserved via RFC 5987 filename*
    assert "%E2%80%94" in cd("a \u2014 b")  # em-dash percent-encoded


def test_synthetic_gmp_monitor_skips_when_unconfigured(monkeypatch):
    """Missing SYNTHETIC_GMP_REFRESH_TOKEN must skip cleanly, not raise (which
    paged a daily 'unhandled exception')."""
    import os
    monkeypatch.delenv("SYNTHETIC_GMP_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("SYNTHETIC_GMP_ACCOUNT_NUMBER", raising=False)
    mod = importlib.import_module("scripts.synthetic_gmp_monitor")
    out = mod.run()
    assert out.get("skipped") is True
    assert out.get("success") is None
