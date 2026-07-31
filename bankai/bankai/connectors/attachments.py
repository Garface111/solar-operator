"""What to do with a file that arrives by email.

Two different things can land in the same inbox and they deserve different
homes. A deed or a policy is a DOCUMENT — it belongs in the vault, to be read
and annotated. A statement export is DATA — filing it as a document would leave
the household staring at a stored file while their balances stayed wrong.

The Apple Card is the case that forced this: no aggregator can reach it, so the
only way its transactions ever arrive is a Wallet export sent by hand. It has to
become real transactions, not an attachment nobody opens.

Everything is kept in the vault regardless, so there is always provenance for a
number that later looks odd.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import vault
from ..models import Account
from .csv_import import import_csv
from .ofx_import import import_ofx

log = logging.getLogger("bankai.attachments")

DATA_EXTENSIONS = (".csv", ".ofx", ".qfx")

#: Naming an account from a filename is a guess, and a wrong guess silently
#: splits one card's history across two accounts. Only patterns specific enough
#: to be safe live here; anything else asks.
#: Separators matter: exports get renamed, and "apple-card-2026-07.csv" is at
#: least as common as "Apple Card Transactions.csv".
_SEP = r"[\s_.-]*"

#: Boundaries are on LETTERS, not \b — "amex_activity.csv" has no word boundary
#: after "amex" because underscore is a word character, so \bamex\b misses the
#: most ordinary filename shape there is.
def _word(term: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z]){term}(?![a-z])", re.I)


ACCOUNT_HINTS = [
    (_word(rf"apple{_SEP}card"), ("Apple Card", "credit")),
    (_word(rf"amex|american{_SEP}express"), ("Amex", "credit")),
    (_word(r"chase"), ("Chase", "credit")),
    (_word(r"discover"), ("Discover", "credit")),
]


def looks_like_data(filename: str) -> bool:
    return filename.lower().endswith(DATA_EXTENSIONS)


def guess_account(*texts: str) -> tuple[str, str] | None:
    """(account name, kind) when the source is unmistakable, else None."""
    haystack = " ".join(t or "" for t in texts)
    for pattern, result in ACCOUNT_HINTS:
        if pattern.search(haystack):
            return result
    return None


def is_ofx(text: str, filename: str) -> bool:
    return filename.lower().endswith((".ofx", ".qfx")) or "<OFX" in text[:2000].upper()


def handle(
    session: Session,
    *,
    filename: str,
    data: bytes,
    sender: str,
    subject: str = "",
    category: str = "other",
) -> dict:
    """File an emailed attachment, and import it when it carries transactions."""
    doc, created = vault.add_document(
        session, filename=filename, data=data, category=category
    )
    result: dict = {
        "filename": filename,
        "document_id": doc.id,
        "filed": created,
        "imported": False,
    }
    if created:
        doc.summary = (
            f"Emailed in by {sender}"
            + (f' — "{subject}"' if subject else "")
            + ". Not yet reviewed in detail."
        )

    if not looks_like_data(filename):
        return result

    text = data.decode("utf-8-sig", errors="replace")
    guess = guess_account(filename, subject)
    if not guess:
        # Better an honest question than a phantom account nobody recognises.
        result["needs_account"] = True
        result["note"] = (
            "This looks like a statement export but the account it belongs to is "
            "not obvious from the filename or subject. Ask which account it is, "
            "then import it — do not guess."
        )
        return result

    account_name, kind = guess
    try:
        if is_ofx(text, filename):
            imported = import_ofx(session, text=text, account_name=account_name, kind=kind)
            fmt = "ofx"
        else:
            imported = import_csv(session, text=text, account_name=account_name, kind=kind)
            fmt = "csv"
    except Exception as exc:
        log.exception("could not import %s", filename)
        result["import_error"] = str(exc)[:300]
        result["note"] = (
            "The file was saved to the vault but its transactions could not be "
            "read. Say so plainly rather than implying the data is in."
        )
        return result

    result.update({
        "imported": True,
        "account": account_name,
        "format": fmt,
        "added": imported.added,
        "duplicates_skipped": imported.skipped,
    })

    # A CSV is a list of transactions, not a ledger — it carries no balance, so
    # the account would sit at zero and quietly understate what is owed. OFX/QFX
    # do carry one. Surface the difference instead of showing a confident $0.
    account = session.execute(
        select(Account).where(Account.name == account_name)
    ).scalar_one_or_none()
    if account is not None and account.balance is None:
        result["balance_unknown"] = True
        result["note"] = (
            f"'{account_name}' now has transactions but NO balance: a CSV export "
            "lists activity without the amount outstanding. Until someone gives "
            "the statement balance (or sends an OFX/QFX export, which carries "
            "one), this card contributes nothing to net worth — say so rather "
            "than implying the card is covered."
        )
    if created:
        doc.summary = (
            f"Emailed in by {sender}"
            + (f' — "{subject}"' if subject else "")
            + f". Imported into '{account_name}': {imported.added} transactions added, "
            f"{imported.skipped} already known."
        )
    return result
