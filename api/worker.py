"""
Worker — does the actual scraping work the API queues up.

Two main entry points:
  pull_bills_for_tenant(tenant_id) -> pulls every enabled account's full bill
                                      history (JSON-first, PDF fallback)
  run_pending_jobs()              -> walks the Job table and dispatches

The scheduler in scheduler.py calls these on a cadence (or you can hit them
via /admin/jobs/run in the API).

Strategy (per account):
  1. PREFERRED: GET https://api.greenmountainpower.com/api/v2/accounts/{n}/bills
     using the stored JWT. Returns full history; we upsert every bill in one
     pass. Robust against PDF format changes.
  2. FALLBACK: the old per-account currentBillUrl → Utilitec redirector → PDF
     parse path. Only used if the JSON call raises (expired JWT, GMP API
     downtime, schema change).
"""
from __future__ import annotations
import logging
import pathlib, traceback
from datetime import datetime
from sqlalchemy import select
from .db import SessionLocal, DATA_DIR
from .models import Tenant, UtilityAccount, UtilitySession, Bill, Job, now
from .adapters import get_adapter
from .sessions import token_for_account

log = logging.getLogger(__name__)


BILLS_DIR = DATA_DIR / "bills"
BILLS_DIR.mkdir(exist_ok=True, parents=True)


def _tracker_append_for_account(db, tenant_id: str, account, result: dict) -> None:
    """When a pull CREATES a new bill for `account`, append the latest period to
    every offtaker's BYO generation spreadsheet bound to that account. Gated on
    SPREADSHEET_TRACKER_ENABLED inside the tracker module + IDEMPOTENT (never
    double-appends the same period). Best-effort — a tracker hiccup must never
    fail or block a bill pull, so all errors are swallowed (logged in-module)."""
    try:
        if not result or (result.get("created") or 0) <= 0:
            return
        from .billing.sheet_tracker import update_all_for_account, tracker_enabled
        if not tracker_enabled():
            return
        statuses = update_all_for_account(db, tenant_id, account.id)
        if statuses:
            result["tracker"] = statuses
    except Exception as e:  # noqa: BLE001
        result["tracker_error"] = f"{type(e).__name__}: {e}"


def _latest_session_token(db, tenant_id: str, provider: str) -> str | None:
    sess = db.execute(
        select(UtilitySession)
        .where(UtilitySession.tenant_id == tenant_id,
               UtilitySession.provider == provider)
        .order_by(UtilitySession.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return sess.api_token if sess else None


def _upsert_bill(db, tenant_id: str, account: UtilityAccount,
                 metrics: dict, source_path: str | None = None,
                 pdf_bytes: bytes | None = None,
                 pdf_content_type: str | None = None) -> str:
    """Upsert one bill row. Returns 'created' or 'updated'.

    `pdf_bytes` (+ content type) are the DURABLE bill-PDF bytes — persisted
    in-row so the auto-attach-GMP-bill feature can read them after a redeploy
    (pdf_path alone is ephemeral). Optional; null when the PDF wasn't captured.
    """
    existing = None
    if metrics.get("period_end"):
        # Tolerate DUPLICATE bill rows for the same (account, period_end). Some accounts
        # have accumulated hundreds-to-thousands from capture sprawl (Rick Lunt's had
        # 1001), and scalar_one_or_none() then RAISED MultipleResultsFound — which crashed
        # the WHOLE pull, so no new bill ever landed server-side (the real "GMP bill didn't
        # update" cause). Update the most-recent matching row instead of crashing; the
        # offtaker invoice selects by period_end DESC so the freshest row wins.
        # (Deduping the historical rows is a separate cleanup.)
        existing = db.execute(
            select(Bill).where(
                Bill.account_id == account.id,
                Bill.period_end == metrics["period_end"],
            ).order_by(Bill.id.desc())
        ).scalars().first()

    # Full energy-record fields — present on the JSON path (the sponge), absent
    # on the legacy PDF path (.get → None, columns nullable). raw_json is the
    # authoritative full bill so nothing GMP exposed is ever lost.
    _sponge = dict(
        kwh_consumed=metrics.get("kwh_consumed"),
        kwh_sent_to_grid=metrics.get("kwh_sent_to_grid"),
        kwh_gross_generated=metrics.get("kwh_gross_generated"),
        is_net_metered=metrics.get("is_net_metered"),
        total_cost=metrics.get("total_cost"),
        net_credit=metrics.get("net_credit"),
        solar_credit_usd=metrics.get("solar_credit_usd"),
        avg_rate_cents_kwh=metrics.get("avg_rate_cents_kwh"),
        supplier=metrics.get("supplier"),
        raw_json=metrics.get("raw_json"),
    )

    if existing:
        existing.kwh_generated = metrics["kwh_generated"]
        existing.billing_days  = metrics["billing_days"]
        existing.period_start  = metrics["period_start"]
        existing.bill_date     = metrics["bill_date"]
        existing.raw_text      = metrics.get("raw_text", "")
        existing.parse_status  = metrics["parse_status"]
        existing.pulled_at     = now()
        for k, v in _sponge.items():
            if v is not None:          # never overwrite a known value with None
                setattr(existing, k, v)
        if source_path:
            existing.pdf_path = source_path
        if pdf_bytes:
            existing.pdf_bytes = pdf_bytes
            existing.pdf_content_type = pdf_content_type or "application/pdf"
        if metrics.get("document_number"):
            existing.document_number = metrics["document_number"]
        return "updated"

    db.add(Bill(
        tenant_id=tenant_id, account_id=account.id,
        bill_date=metrics["bill_date"],
        period_start=metrics["period_start"],
        period_end=metrics["period_end"],
        billing_days=metrics["billing_days"],
        kwh_generated=metrics["kwh_generated"],
        pdf_path=source_path,
        pdf_bytes=pdf_bytes,
        pdf_content_type=(pdf_content_type or "application/pdf") if pdf_bytes else None,
        raw_text=metrics.get("raw_text", ""),
        parse_status=metrics["parse_status"],
        document_number=metrics.get("document_number"),
        **_sponge,
    ))
    return "created"


def _pull_via_json(db, tenant_id: str, account: UtilityAccount,
                   adapter, jwt: str) -> dict:
    """JSON-first path. Returns result dict."""
    bills = adapter.fetch_bills_json(account.account_number, jwt)
    created = updated = 0
    no_generation = 0
    for b in bills:
        metrics = adapter.bill_json_to_metrics(b)
        # DATA SPONGE: absorb EVERY bill into the energy record — consumption,
        # cost, rate, net credits — not just solar-generation periods. (Previously
        # we skipped any bill with no kWh generated, throwing away the rest of the
        # owner's energy history.) We still TRACK the no-generation count so the
        # NEPOOL generation signal is observable, but we persist the full record.
        if metrics["kwh_generated"] is None or metrics["kwh_generated"] <= 0:
            no_generation += 1
        action = _upsert_bill(db, tenant_id, account, metrics, source_path=None)
        if action == "created":
            created += 1
        else:
            updated += 1
    # Metrics: every period from the utility's bill history API (full sponge).
    # PDFs: aggressively backfill missing statement PDFs for as many periods as
    # the portal exposes via /transactions (not just the current bill). Cap per
    # run so a 15-year account doesn't hang the worker — the 6h scheduler fills
    # the rest. Best-effort: never fail the pull over a PDF fetch.
    pdf_capture = _capture_current_bill_pdf(db, tenant_id, account, adapter, jwt)
    pdf_backfill = _backfill_missing_bill_pdfs(db, tenant_id, account, adapter, jwt)
    return {
        "account": account.account_number, "nickname": account.nickname,
        "status": "ok", "source": "json",
        "bills_returned": len(bills),
        "created": created, "updated": updated,
        "no_generation": no_generation,
        "pdf_capture": pdf_capture,
        "pdf_backfill": pdf_backfill,
    }


# How many missing statement PDFs to fetch per account per pull.
# ~12y of monthly statements ≈ 150; 300 covers long histories in ONE pass so the
# utility-bill archive fills completely instead of trickling over many 6h ticks.
PDF_BACKFILL_MAX_PER_PULL = 300
# How far back to ask the portal for statement documents (years).
PDF_BACKFILL_LOOKBACK_DAYS = 365 * 12


def _txn_doc_date(t) -> "datetime.date | None":
    ds = str((t or {}).get("date") or "")[:10]
    try:
        return datetime(int(ds[0:4]), int(ds[5:7]), int(ds[8:10])).date()
    except Exception:
        return None


def _match_txn_doc_for_bill(bill: Bill, docs: list) -> dict | None:
    """Pick the best transactions[] document for a bill row (statement after period close)."""
    from datetime import timedelta
    if not docs:
        return None
    floor = (bill.period_end.date() - timedelta(days=1)) if bill.period_end else None
    # Prefer docs issued on/after period end (real statement), not prior months.
    cands = [t for t in docs if floor is None or (_txn_doc_date(t) and _txn_doc_date(t) >= floor)]
    if not cands:
        return None
    # Cap: statement more than ~90d after period_end is almost certainly a later bill.
    if bill.period_end:
        pe = bill.period_end.date()
        tight = [t for t in cands if _txn_doc_date(t) and (_txn_doc_date(t) - pe).days <= 90]
        if tight:
            cands = tight
    tgt = bill.bill_date or bill.period_end
    target = tgt.date() if tgt else datetime.utcnow().date()
    return min(cands, key=lambda t: abs((_txn_doc_date(t) - target).days) if _txn_doc_date(t) else 10 ** 6)


def _periods_with_pdf(db, account_id: int) -> set:
    """period_end values that already have durable PDF bytes on ANY bill row."""
    rows = db.execute(
        select(Bill.period_end).where(
            Bill.account_id == account_id,
            Bill.pdf_bytes.isnot(None),
            Bill.period_end.isnot(None),
        ).distinct()
    ).scalars().all()
    return set(rows)


def _pick_missing_period_bills(db, account_id: int, limit: int) -> list:
    """One representative bill row per period_end that still has no PDF on file.

    Capture sprawl created many duplicate (account, period_end) rows; downloading
    the same statement N times wastes portal calls and still leaves siblings bare.
    We attach to the freshest row (highest id) and propagate to siblings after."""
    have = _periods_with_pdf(db, account_id)
    # Newest periods first — archive + offtaker invoices care about recent first.
    candidates = db.execute(
        select(Bill).where(
            Bill.account_id == account_id,
            Bill.pdf_bytes.is_(None),
            Bill.period_end.isnot(None),
        ).order_by(Bill.period_end.desc(), Bill.id.desc())
        .limit(max(limit * 8, 500))  # dupe-heavy accounts need a wide pool
    ).scalars().all()
    seen: set = set()
    out: list = []
    for b in candidates:
        pe = b.period_end
        if pe in have or pe in seen:
            continue
        seen.add(pe)
        out.append(b)
        if len(out) >= limit:
            break
    return out


def _attach_pdf_to_period(db, account_id: int, period_end, data: bytes,
                          content_type: str | None, primary: Bill | None = None) -> int:
    """Write PDF bytes onto every bill row for this (account, period_end).

    Returns how many rows were updated. Includes `primary` even if the session
    hasn't flushed yet so we never miss the matched row."""
    ctype = content_type or "application/pdf"
    rows = list(db.execute(
        select(Bill).where(
            Bill.account_id == account_id,
            Bill.period_end == period_end,
            Bill.pdf_bytes.is_(None),
        )
    ).scalars().all())
    if primary is not None and not primary.pdf_bytes and primary not in rows:
        rows.insert(0, primary)
    n = 0
    for s in rows:
        if s.pdf_bytes:
            continue
        s.pdf_bytes = data
        s.pdf_content_type = ctype
        n += 1
    return n


def _backfill_missing_bill_pdfs(db, tenant_id: str, account: UtilityAccount,
                                adapter, jwt: str | None = None) -> dict:
    """Aggressively attach statement PDFs for every period the portal still serves.

    Uses /transactions over PDF_BACKFILL_LOOKBACK_DAYS, downloads up to
    PDF_BACKFILL_MAX_PER_PULL distinct periods per pull (one PDF per period),
    and stamps ALL duplicate bill rows for that period. Idempotent. Best-effort
    — never raises into the pull."""
    if not jwt:
        return {"saved": 0, "reason": "no jwt"}
    if not (hasattr(adapter, "fetch_transactions") and hasattr(adapter, "fetch_bill_pdf_binary")):
        return {"saved": 0, "reason": "adapter cannot fetch statement PDFs"}
    from datetime import timedelta
    missing = _pick_missing_period_bills(db, account.id, PDF_BACKFILL_MAX_PER_PULL)
    if not missing:
        return {"saved": 0, "reason": "all periods already have PDFs", "missing_periods": 0}
    try:
        txns = adapter.fetch_transactions(
            account.account_number, jwt,
            datetime.utcnow() - timedelta(days=PDF_BACKFILL_LOOKBACK_DAYS),
            datetime.utcnow() + timedelta(days=1),
            timeout=90,
        )
    except Exception as e:  # noqa: BLE001
        return {"saved": 0, "reason": f"transactions fetch failed: {type(e).__name__}: {e}",
                "missing_periods": len(missing)}
    docs = [t for t in (txns or []) if isinstance(t, dict) and t.get("urlBinary")]
    if not docs:
        return {"saved": 0, "reason": "no statement documents in portal window",
                "missing_periods": len(missing), "txns": len(txns or [])}
    # Cache downloads by urlBinary so two periods never re-fetch the same document.
    pdf_cache: dict[str, tuple[bytes, str]] = {}
    saved = 0           # distinct periods filled
    rows_stamped = 0    # includes sibling dupes
    tried = 0
    errors = 0
    unmatched = 0
    for bill in missing:
        if saved >= PDF_BACKFILL_MAX_PER_PULL:
            break
        chosen = _match_txn_doc_for_bill(bill, docs)
        if not chosen:
            unmatched += 1
            continue
        url = chosen["urlBinary"]
        tried += 1
        try:
            if url not in pdf_cache:
                data, ctype = adapter.fetch_bill_pdf_binary(url)
                if not data or data[:4] != b"%PDF":
                    errors += 1
                    continue
                pdf_cache[url] = (data, ctype or "application/pdf")
            data, ctype = pdf_cache[url]
            n = _attach_pdf_to_period(db, account.id, bill.period_end, data, ctype, primary=bill)
            if n:
                saved += 1
                rows_stamped += n
        except Exception:  # noqa: BLE001 — one bad doc never aborts the rest
            errors += 1
            continue
    return {
        "saved": saved,
        "rows_stamped": rows_stamped,
        "tried": tried,
        "errors": errors,
        "unmatched": unmatched,
        "missing_periods_before": len(missing),
        "docs_available": len(docs),
        "unique_pdfs_fetched": len(pdf_cache),
        "via": "transactions-backfill",
        "lookback_days": PDF_BACKFILL_LOOKBACK_DAYS,
        "cap": PDF_BACKFILL_MAX_PER_PULL,
    }


def _capture_current_bill_pdf(db, tenant_id: str, account: UtilityAccount,
                              adapter, jwt: str | None = None) -> dict:
    """Persist the CURRENT bill's PDF bytes onto its bill row, for auto-attach.

    PRIMARY (works for EVERY account, including the managed-customer / offtaker
    accounts whose extension capture never grabbed a per-account currentBillUrl):
    the GMP /transactions endpoint hands a per-bill PDF link (`urlBinary`) for any
    account the operator's JWT can see. FALLBACK: the account's own stored
    currentBillUrl (present only for the operator's primary accounts). Best-effort
    — never raises into the pull. Returns a small status dict.

    Historical periods without PDFs are filled by `_backfill_missing_bill_pdfs`
    (aggressive multi-year backfill, capped per run)."""
    from datetime import timedelta
    # Newest bill row for this account is where the current PDF belongs.
    bill = db.execute(
        select(Bill).where(Bill.account_id == account.id)
        .order_by(Bill.period_end.desc().nullslast(), Bill.bill_date.desc().nullslast())
    ).scalars().first()
    if bill is None:
        return {"saved": False, "reason": "no bill row to attach to"}
    # SETTLED bills (period closed >45d ago) with a PDF won't change — skip. But a RECENT
    # bill is re-checked even if it already has a PDF, so a stale statement captured before
    # GMP published the real one (the May-PDF-on-the-June-row bug) can SELF-HEAL.
    recent = bool(bill.period_end) and (datetime.utcnow().date() - bill.period_end.date()).days <= 45
    if bill.pdf_bytes and not recent:
        return {"saved": False, "reason": "already captured", "bill_id": bill.id}

    # ── PRIMARY: transactions → urlBinary (works for managed/offtaker accounts) ──
    if jwt and hasattr(adapter, "fetch_transactions") and hasattr(adapter, "fetch_bill_pdf_binary"):
        try:
            # Current-bill window only (full history is the backfill path).
            txns = adapter.fetch_transactions(
                account.account_number, jwt,
                datetime.utcnow() - timedelta(days=400), datetime.utcnow() + timedelta(days=1))
            docs = sorted(
                [t for t in txns if isinstance(t, dict) and t.get("urlBinary")],
                key=lambda t: t.get("date") or "", reverse=True)
            if docs:
                chosen = _match_txn_doc_for_bill(bill, docs)
                if not chosen:
                    return {"saved": False, "reason": "no statement issued for this period yet",
                            "bill_id": bill.id}
                data, ctype = adapter.fetch_bill_pdf_binary(chosen["urlBinary"])
                if not data or data[:4] != b"%PDF":
                    return {"saved": False, "reason": "not a PDF (auth redirect?)", "bill_id": bill.id}
                if bill.pdf_bytes and bytes(bill.pdf_bytes) == data:
                    return {"saved": False, "reason": "unchanged", "bill_id": bill.id}
                replaced = bool(bill.pdf_bytes)
                bill.pdf_bytes = data
                bill.pdf_content_type = ctype or "application/pdf"
                return {"saved": True, "bill_id": bill.id, "bytes": len(data), "replaced": replaced,
                        "via": "transactions", "doc_date": chosen.get("date")}
        except Exception:  # noqa: BLE001 — fall through to the legacy currentBillUrl path
            pass

    # ── FALLBACK: the account's own currentBillUrl (operator's primary accounts) ──
    current_bill_url = (account.extra or {}).get("currentBillUrlBinary") or \
                       (account.extra or {}).get("current_bill_url")
    if not current_bill_url:
        return {"saved": False, "reason": "no transactions doc and no current_bill_url"}
    if not hasattr(adapter, "fetch_bill_pdf"):
        return {"saved": False, "reason": "adapter has no fetch_bill_pdf"}
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    safe = (account.nickname or account.account_number).replace(" ", "_").replace("/", "_")
    pdf_path = BILLS_DIR / tenant_id / f"{ts}_{account.provider}_{safe}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _, content_type = adapter.fetch_bill_pdf(current_bill_url, pdf_path)
        data = pdf_path.read_bytes()
    except Exception as e:  # noqa: BLE001 — auth/format/transport; surface, don't fail the pull
        return {"saved": False, "reason": f"fetch failed: {type(e).__name__}: {e}"}
    if not data or data[:4] != b"%PDF":
        return {"saved": False, "reason": "not a PDF (auth redirect?)"}
    bill.pdf_bytes = data
    bill.pdf_content_type = content_type or "application/pdf"
    bill.pdf_path = str(pdf_path)
    return {"saved": True, "bill_id": bill.id, "bytes": len(data), "via": "currentBillUrl"}


def _pull_via_pdf(db, tenant_id: str, account: UtilityAccount, adapter) -> dict:
    """Fallback PDF-redirector path. Pulls only the CURRENT bill (the only
    one currentBillUrl points to), parses it, upserts one row."""
    current_bill_url = (account.extra or {}).get("currentBillUrlBinary") or \
                       (account.extra or {}).get("current_bill_url")
    if not current_bill_url:
        return {"account": account.account_number, "nickname": account.nickname,
                "status": "skipped", "reason": "no current_bill_url"}

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    safe = (account.nickname or account.account_number).replace(" ", "_").replace("/", "_")
    pdf_path = BILLS_DIR / tenant_id / f"{ts}_{account.provider}_{safe}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    path, content_type = adapter.fetch_bill_pdf(current_bill_url, pdf_path)
    metrics = adapter.extract_bill_metrics(pdf_path)
    metrics["source"] = "pdf"
    # Persist the actual PDF bytes durably (in-row) — pdf_path is ephemeral on
    # Railway, and the auto-attach-GMP-bill feature reads these bytes later.
    try:
        pdf_bytes = pathlib.Path(pdf_path).read_bytes()
    except OSError:
        pdf_bytes = None
    action = _upsert_bill(db, tenant_id, account, metrics,
                          source_path=str(pdf_path),
                          pdf_bytes=pdf_bytes,
                          pdf_content_type=content_type or "application/pdf")
    return {
        "account": account.account_number, "nickname": account.nickname,
        "status": "ok", "source": "pdf", "action": action,
        "kwh_generated": metrics["kwh_generated"],
        "billing_days": metrics["billing_days"],
        "pdf": pdf_path.name,
        "pdf_bytes_saved": bool(pdf_bytes),
    }


def pull_bills_for_tenant(tenant_id: str) -> dict:
    """Pull all available bills for every enabled account for one tenant.
    Returns a per-account result summary."""
    results: list[dict] = []
    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_id)
        if not tenant:
            return {"error": f"unknown tenant {tenant_id}"}
        # Demo tenants (e.g. the 800-offtaker Anna demo) carry fabricated utility
        # accounts that never authenticate; a real-utility pull just floods the
        # provider with 403s and pegs the web workers (prod outage 2026-07-05).
        # Their bills are seeded, never pulled — hard no-op on EVERY call path
        # (scheduled drain AND on-demand refresh), so a stale queued job is safe.
        if getattr(tenant, "is_demo", False):
            return {"skipped": "demo tenant", "tenant_id": tenant_id, "accounts": 0}

        accounts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant_id,
                UtilityAccount.enabled == True,
            )
        ).scalars().all()

        for acc in accounts:
            # Commit each account's writes BEFORE the next account's external API
            # call. Holding ONE transaction across the whole multi-account pull meant
            # a single slow/hanging vendor fetch kept row locks on already-updated
            # bills (e.g. their pulled_at stamp) for the rest of the loop — the
            # lock pile-up + connection-pool exhaustion that took prod down
            # 2026-07-09 (blocked UPDATE bills.pulled_at behind a 100s+
            # idle-in-transaction). Per-account commit bounds any lock hold to that
            # one account's own work, so a later hang can't freeze earlier bills.
            try:
                adapter = get_adapter(acc.provider)
                # Pick the token bound to THIS account's login identity, not just
                # the tenant's latest capture — so every client's login keeps
                # scraping even after the operator logs in as a different client.
                jwt = token_for_account(db, acc)

                # Try JSON path first
                json_attempted = False
                json_err = None
                if jwt and hasattr(adapter, "fetch_bills_json"):
                    json_attempted = True
                    try:
                        rj = _pull_via_json(db, tenant_id, acc, adapter, jwt)
                        _tracker_append_for_account(db, tenant_id, acc, rj)
                        results.append(rj)
                        continue
                    except Exception as e:
                        json_err = f"{type(e).__name__}: {e}"

                # Fallback: PDF
                try:
                    r = _pull_via_pdf(db, tenant_id, acc, adapter)
                    _tracker_append_for_account(db, tenant_id, acc, r)
                    if json_attempted:
                        r["json_fallback_reason"] = json_err
                    results.append(r)
                except Exception as e:
                    results.append({
                        "account": acc.account_number, "nickname": acc.nickname,
                        "status": "failed",
                        "json_error": json_err if json_attempted else None,
                        "pdf_error": f"{type(e).__name__}: {e}",
                        "trace": traceback.format_exc(limit=2),
                    })
            finally:
                # Persist this account's work + release its row locks before the
                # next (possibly slow) external fetch. A commit hiccup must not
                # abort the whole tenant pull.
                try:
                    db.commit()
                except Exception:
                    db.rollback()

        # Freshly-pulled bills → daily stream IMMEDIATELY (idempotent; only fills
        # bill_prorate gaps, real metered days always win). Without this a
        # bills-only tenant read ALL ZEROS on overview/trends until the nightly
        # 05:30 bill_to_daily cron — up to 24h of "broken" dashboards right after
        # a successful connect. Best-effort: a transform hiccup never fails the pull.
        try:
            from .jobs.bill_to_daily import transform_tenant_bills
            with db.begin_nested():   # savepoint: a hiccup can't poison the pull tx
                transform_tenant_bills(tenant_id, db=db)
        except Exception:
            log.warning("bill→daily transform after pull failed for %s",
                        tenant_id, exc_info=True)

        # Stamp last_pull_at so the dashboard can show the next-pull countdown.
        tenant.last_pull_at = now()
        db.commit()

    return {
        "tenant": tenant_id,
        "ran_at": datetime.utcnow().isoformat() + "Z",
        "accounts_processed": len(results),
        "results": results,
    }


def pull_account_bills(tenant_id: str, account_id: int) -> dict:
    """Pull bills for ONE account on demand — e.g. right before generating an offtaker
    invoice, so it reflects the LATEST GMP statement without waiting for the 6h
    scheduler. Same JSON-first / PDF-fallback path as the full tenant pull. Commits, so
    the caller's next (READ COMMITTED) query sees the fresh bill."""
    with SessionLocal() as db:
        acc = db.get(UtilityAccount, account_id)
        if not acc or acc.tenant_id != tenant_id or getattr(acc, "enabled", True) is False:
            return {"status": "skipped", "reason": "account-not-eligible"}
        adapter = get_adapter(acc.provider)
        jwt = token_for_account(db, acc)
        json_err = None
        # Surface fresh bill generation in the daily stream right away (only for
        # this account's array; idempotent bill_prorate fill, real days win).
        def _transform_after_pull():
            if not acc.array_id:
                return
            try:
                from .jobs.bill_to_daily import transform_array_bills
                with db.begin_nested():   # savepoint: never poison the pull tx
                    transform_array_bills(db, acc.array_id)
            except Exception:
                log.warning("bill→daily transform after account pull failed "
                            "(array %s)", acc.array_id, exc_info=True)

        if jwt and hasattr(adapter, "fetch_bills_json"):
            try:
                r = _pull_via_json(db, tenant_id, acc, adapter, jwt)
                _tracker_append_for_account(db, tenant_id, acc, r)
                _transform_after_pull()
                db.commit()
                return r
            except Exception as e:  # noqa: BLE001
                json_err = f"{type(e).__name__}: {e}"
        try:
            r = _pull_via_pdf(db, tenant_id, acc, adapter)
            _tracker_append_for_account(db, tenant_id, acc, r)
            _transform_after_pull()
            db.commit()
            if json_err:
                r["json_fallback_reason"] = json_err
            return r
        except Exception as e:  # noqa: BLE001
            db.rollback()
            return {"status": "failed", "json_error": json_err,
                    "pdf_error": f"{type(e).__name__}: {e}"}


def run_job(job_id: int) -> dict:
    """Execute one queued Job row."""
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return {"error": f"unknown job {job_id}"}
        if job.status != "queued":
            return {"error": f"job {job_id} not queued (status={job.status})"}
        job.status = "running"
        job.started_at = now()
        db.commit()

        try:
            if job.kind == "pull_bills":
                result = pull_bills_for_tenant(job.tenant_id)
            else:
                raise ValueError(f"unknown job kind: {job.kind}")
            job.status = "succeeded"
            job.result = result
        except Exception as e:
            job.status = "failed"
            job.error = f"{e}\n{traceback.format_exc(limit=4)}"
        finally:
            job.finished_at = now()
            db.commit()
            return {"job_id": job_id, "status": job.status, "result": job.result, "error": job.error}


def run_pending_jobs(limit: int = 20) -> list[dict]:
    out = []
    with SessionLocal() as db:
        pending = db.execute(
            select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(limit)
        ).scalars().all()
    for j in pending:
        out.append(run_job(j.id))
    return out
