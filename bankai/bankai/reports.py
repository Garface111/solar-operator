"""The weekly printed report — the copilot's Saturday-morning page.

Every week the household gets one physical page on the printer: where the money
went this week against last week and the trailing month, net worth and the
balances behind it, the road ahead if nothing changes, and a short summary the
copilot writes itself. Data assembly is pure code (numbers must not depend on a
model call succeeding); only the narrative comes from the brain, and the report
still prints without it.

Printing goes through CUPS (`lp`) to the printer named by PRINTER_NAME — on
FordBrain that is the Epson ET-2800 at 10.0.0.59, registered as "household".
"""
from __future__ import annotations

import subprocess
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config
from .intelligence.forecast import cash_flow_forecast
from .intelligence.horizon import project_wealth
from .intelligence.insights import net_worth
from .models import Transaction

#: Same fixed seed as agent/tools.py — identical questions must get identical
#: projection bands, or the household reads the wobble as the copilot
#: contradicting itself.
HORIZON_SEED = 20260101

REPORTS_DIR = config.BASE_DIR / "reports"


def _window(session: Session, since: date, until: date) -> dict:
    """Spend/income totals and per-category outflows for [since, until)."""
    rows = session.execute(
        select(Transaction.category, func.sum(Transaction.amount))
        .where(
            Transaction.posted >= since,
            Transaction.posted < until,
            Transaction.pending.is_(False),
            Transaction.category != "transfer",
            Transaction.amount < 0,
        )
        .group_by(Transaction.category)
    ).all()
    by_category = {cat: round(-total, 2) for cat, total in rows if total}
    income = session.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
            Transaction.posted >= since,
            Transaction.posted < until,
            Transaction.pending.is_(False),
            Transaction.category != "transfer",
            Transaction.amount > 0,
        )
    ).scalar_one()
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "spend": round(sum(by_category.values()), 2),
        "income": round(income or 0.0, 2),
        "by_category": dict(
            sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
        ),
    }


def gather_weekly_data(session: Session, today: date | None = None) -> dict:
    """Everything the printed page needs, assembled without a model call."""
    today = today or date.today()
    this_week = _window(session, today - timedelta(days=7), today)
    last_week = _window(session, today - timedelta(days=14), today - timedelta(days=7))
    this_month = _window(session, today - timedelta(days=30), today)
    prior_month = _window(session, today - timedelta(days=60), today - timedelta(days=30))

    try:
        forecast = cash_flow_forecast(session, days=60)
    except Exception:
        forecast = {}
    try:
        projection = project_wealth(session, years=5, seed=HORIZON_SEED)
    except Exception:
        projection = {}

    return {
        "date": today.isoformat(),
        "net_worth": net_worth(session),
        "this_week": this_week,
        "last_week": last_week,
        "this_month": this_month,
        "prior_month": prior_month,
        "forecast": forecast,
        "projection": projection,
    }


# fpdf's core fonts are latin-1; the narrative comes from a model that likes
# em-dashes and curly quotes. Map the common ones instead of crashing the
# Saturday print over a punctuation mark.
_ASCII_MAP = str.maketrans(
    {"—": "-", "–": "-", "‘": "'", "’": "'",
     "“": '"', "”": '"', "…": "...", "→": "->",
     " ": " ", "•": "-"}
)


def _clean(text: str) -> str:
    return (text or "").translate(_ASCII_MAP).encode("latin-1", "replace").decode("latin-1")


def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def render_pdf(data: dict, narrative: str, path: Path) -> Path:
    """One page, printable, honest: numbers first, the copilot's words after."""
    from fpdf import FPDF

    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()

    def heading(text: str) -> None:
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(40, 40, 40)
        pdf.cell(0, 8, _clean(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    pdf.set_font("Helvetica", "B", 17)
    pdf.cell(0, 10, _clean(f"Household weekly report - {data['date']}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, "Prepared by the household copilot (BankAI)",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    nw = data.get("net_worth") or {}
    heading(f"Net worth: {_money(nw.get('total'))}")
    pdf.set_font("Helvetica", "", 9)
    for account in (nw.get("accounts") or [])[:12]:
        pdf.cell(0, 5, _clean(
            f"  {account.get('name', '?')}: {_money(account.get('balance'))}"
        ), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    tw, lw = data["this_week"], data["last_week"]
    tm, pm = data["this_month"], data["prior_month"]
    heading("The week and the month")
    pdf.set_font("Helvetica", "", 10)
    for line in (
        f"This week: spent {_money(tw['spend'])}, income {_money(tw['income'])}"
        f"   (last week: spent {_money(lw['spend'])})",
        f"Trailing 30 days: spent {_money(tm['spend'])}, income {_money(tm['income'])}"
        f"   (prior 30: spent {_money(pm['spend'])})",
    ):
        pdf.cell(0, 6, _clean(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    heading("Where this week's money went")
    categories = list(tw["by_category"].items())[:8]
    if categories:
        top = max(v for _, v in categories) or 1.0
        pdf.set_font("Helvetica", "", 9)
        for cat, amount in categories:
            y = pdf.get_y()
            pdf.set_xy(pdf.l_margin, y)
            pdf.cell(58, 5.5, _clean(f"{cat}: {_money(amount)}"))
            pdf.set_fill_color(70, 110, 170)
            pdf.rect(pdf.l_margin + 60, y + 1, max(2.0, 110 * amount / top), 3.5, "F")
            pdf.ln(5.5)
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 5, "No categorized spending recorded this week.",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    heading("If we stay on this track")
    pdf.set_font("Helvetica", "", 10)
    forecast, projection = data.get("forecast") or {}, data.get("projection") or {}
    if forecast.get("lowest_point"):
        low = forecast["lowest_point"]
        pdf.cell(0, 6, _clean(
            f"Next 60 days: liquid cash bottoms near {_money(low.get('balance'))}"
            f" around {low.get('date', '?')}."
        ), new_x="LMARGIN", new_y="NEXT")
    bands = projection.get("bands") or []
    p50 = bands[-1].get("p50") if bands and isinstance(bands[-1], dict) else None
    if isinstance(p50, (int, float)):
        pdf.cell(0, 6, _clean(
            f"Five-year projection at the current pace: about {_money(p50)}"
            " net worth (median path; assumptions inside the dashboard)."
        ), new_x="LMARGIN", new_y="NEXT")
    if not forecast.get("lowest_point") and not isinstance(p50, (int, float)):
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 5, "Not enough recurring history yet to project forward.",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    heading("From your copilot")
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5.5, _clean(narrative or (
        "I could not reach my brain while writing this page, so this week you "
        "get the numbers without the commentary. Ask me anything about them in "
        "the thread."
    )))

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path


def render_text_page(title: str, body: str, path: Path) -> Path:
    """Anything the copilot writes, as a clean printable page (or several) —
    shopping lists, dispute-letter drafts, a summary the household asked to
    hold in their hands. Auto page-breaks; latin-1 sanitized like the report."""
    from fpdf import FPDF

    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 9, _clean(title or "From your copilot"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, _clean(f"Printed by the household copilot - {date.today().isoformat()}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5.5, _clean(body))
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path


def print_pdf(path: Path, title: str = "household weekly report") -> str:
    """Send the page to the household printer via CUPS. Returns the job id."""
    proc = subprocess.run(
        ["lp", "-d", config.PRINTER_NAME, "-t", title, str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"lp failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}"
        )
    return (proc.stdout or "").strip()
