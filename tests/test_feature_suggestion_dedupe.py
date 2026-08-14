"""Filing dedupe: literal repeats resolve to the existing row.

Why: the proactive mind re-files its standing UX themes every ~6h with
byte-identical text. Each copy minted its own row — 39 of the 40 rows queued
between Aug 9 and Aug 14 were copies of ONE suggestion — and before the Aug-9
pipeline pause each copy triggered its own AUTO build (#159–163: five
near-identical restyles shipped in one morning). That spend is what got the
whole self-build pipeline cost-swept; the dedupe is what makes re-enabling it
safe. A human re-ask with fresh wording still files normally.

Also pins the repaired submit endpoint: the 2026-08 Sovereign removal chopped
its tail off, so POST /v1/feature-suggestion returned null and the Improve
widget never got an id.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import api.feature_suggestions as fsmod
from api.db import SessionLocal
from api.feature_suggestions import FeatureSuggestion, find_open_duplicate

BOILER = ("[Proactive mind — prepared for your fleet's UX]\n"
          "Repeated UX friction notes — improve scannability everywhere.")


def _purge(db):
    for row in db.query(FeatureSuggestion).filter(
            FeatureSuggestion.text.like("%scannability everywhere%")).all():
        db.delete(row)
    for row in db.query(FeatureSuggestion).filter(
            FeatureSuggestion.tenant_id == "ten_dedupe_t1").all():
        db.delete(row)
    db.commit()


def _mk(db, *, tenant="ten_dedupe_t1", text=BOILER, status="new",
        reviewed_at=None):
    fs = FeatureSuggestion(text=text, tenant_id=tenant, status=status,
                           reviewed_at=reviewed_at)
    db.add(fs)
    db.commit()
    db.refresh(fs)
    return fs


def test_open_literal_repeat_is_a_duplicate():
    with SessionLocal() as db:
        try:
            _purge(db)
            fs = _mk(db)
            hit = find_open_duplicate(db, "ten_dedupe_t1", BOILER)
            assert hit is not None and hit.id == fs.id
            # whitespace / case variants are the same ask
            assert find_open_duplicate(
                db, "ten_dedupe_t1", "  " + BOILER.upper() + "\n") is not None
        finally:
            _purge(db)


def test_other_tenant_and_fresh_wording_still_file():
    with SessionLocal() as db:
        try:
            _purge(db)
            _mk(db)
            assert find_open_duplicate(db, "ten_dedupe_OTHER", BOILER) is None
            assert find_open_duplicate(
                db, "ten_dedupe_t1", "move the export button up top") is None
        finally:
            _purge(db)


def test_recently_reviewed_repeats_collapse_but_old_ones_expire():
    with SessionLocal() as db:
        try:
            _purge(db)
            _mk(db, status="reviewed",
                reviewed_at=datetime.utcnow() - timedelta(days=1))
            assert find_open_duplicate(db, "ten_dedupe_t1", BOILER) is not None, (
                "a theme reviewed yesterday must not be re-filed 4x/day")
            _purge(db)
            _mk(db, status="reviewed",
                reviewed_at=datetime.utcnow() - timedelta(days=8))
            assert find_open_duplicate(db, "ten_dedupe_t1", BOILER) is None, (
                "after a week a repeat is a fresh signal, not spam")
        finally:
            _purge(db)


def test_shipped_rows_never_block_a_new_ask():
    with SessionLocal() as db:
        try:
            _purge(db)
            _mk(db, status="shipped")
            assert find_open_duplicate(db, "ten_dedupe_t1", BOILER) is None, (
                "asking again for something that already shipped is a NEW ask "
                "(maybe it regressed) — it must reach review")
        finally:
            _purge(db)


def test_submit_endpoint_returns_ok_id_and_dedupes(client):
    text = "Please dedupe-test: make the totals row bold on reports"
    r1 = client.post("/v1/feature-suggestion", json={"text": text})
    assert r1.status_code == 200
    j1 = r1.json()
    assert j1 and j1["ok"] and isinstance(j1["id"], int), (
        "the Sovereign-removal regression: submit must return {ok, id}, not null"
    )
    r2 = client.post("/v1/feature-suggestion", json={"text": "  " + text})
    j2 = r2.json()
    assert j2["ok"] and j2["id"] == j1["id"] and j2.get("deduped") is True
    with SessionLocal() as db:
        try:
            n = db.query(FeatureSuggestion).filter(
                FeatureSuggestion.text.like("%dedupe-test%")).count()
            assert n == 1
        finally:
            for row in db.query(FeatureSuggestion).filter(
                    FeatureSuggestion.text.like("%dedupe-test%")).all():
                db.delete(row)
            db.commit()
