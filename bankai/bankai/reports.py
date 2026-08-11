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

    from . import pending as pending_lib
    from .intelligence.insights import net_worth_history

    nw = net_worth(session)
    # Assets vs liabilities split, for the composition bar on the page.
    assets = round(sum(a["balance"] for a in nw.get("accounts", []) if a["balance"] > 0), 2)
    liabilities = round(sum(-a["balance"] for a in nw.get("accounts", []) if a["balance"] < 0), 2)

    return {
        "date": today.isoformat(),
        "net_worth": nw,
        "assets_total": assets,
        "liabilities_total": liabilities,
        "net_worth_history": net_worth_history(session, months=6),
        "this_week": this_week,
        "last_week": last_week,
        "this_month": this_month,
        "prior_month": prior_month,
        "forecast": forecast,
        "projection": projection,
        # Spends the household mentioned that no statement has shown yet —
        # mostly Apple Card activity waiting on the next Wallet export. On the
        # page so the totals above are never mistaken for the whole story.
        "pending_mentions": pending_lib.open_items(session, today),
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


# RGB palette for the printed charts (fpdf draws with primitives, not CSS).
_GREEN = (15, 107, 87)
_BLUE = (59, 130, 246)
_PURPLE = (124, 58, 237)
_AMBER = (217, 119, 6)
_RED = (220, 38, 38)
_TEAL = (8, 145, 178)
_PINK = (190, 24, 93)
_INK = (26, 43, 60)
_MUTED = (107, 122, 139)
_TRACK = (238, 242, 246)
_CAT_COLORS = [_GREEN, _BLUE, _PURPLE, _AMBER, _TEAL, _PINK, (101, 163, 13), _RED]


def _stat_tiles(pdf, tiles: list[tuple]) -> None:
    """A row of big-number tiles: (value, label, rgb)."""
    if not tiles:
        return
    n = len(tiles)
    gap = 3.0
    w = (pdf.epw - gap * (n - 1)) / n
    h = 20.0
    y = pdf.get_y()
    for i, (val, label, rgb) in enumerate(tiles):
        x = pdf.l_margin + i * (w + gap)
        pdf.set_draw_color(228, 233, 239)
        pdf.set_fill_color(247, 249, 251)
        pdf.rect(x, y, w, h, "DF")
        pdf.set_fill_color(*rgb)
        pdf.rect(x, y, w, 1.6, "F")
        pdf.set_xy(x + 3, y + 4.5)
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(*_INK)
        pdf.cell(w - 6, 7, _clean(str(val)))
        pdf.set_xy(x + 3, y + 12.5)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*_MUTED)
        pdf.cell(w - 6, 4, _clean(label.upper()))
    pdf.set_text_color(0, 0, 0)
    pdf.set_xy(pdf.l_margin, y + h + 4)


def _bar_chart(pdf, items: list[tuple], label_w: float = 52.0, colored: bool = False) -> None:
    """Horizontal bars: (label, value). Magnitude sizes each bar."""
    if not items:
        return
    value_w = 24.0
    bar_w = pdf.epw - label_w - value_w - 4
    top = max((abs(v) for _, v in items), default=0.0) or 1.0
    for i, (label, value) in enumerate(items):
        y = pdf.get_y()
        x = pdf.l_margin
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*_INK)
        pdf.cell(label_w, 5.5, _clean(str(label)[:26]))
        pdf.set_fill_color(*_TRACK)
        pdf.rect(x + label_w, y + 1, bar_w, 3.6, "F")
        rgb = _CAT_COLORS[i % len(_CAT_COLORS)] if colored else _GREEN
        pdf.set_fill_color(*rgb)
        pdf.rect(x + label_w, y + 1, max(0.6, bar_w * abs(value) / top), 3.6, "F")
        pdf.set_xy(x + label_w + bar_w + 2, y)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(value_w, 5.5, _clean(_money(value)), align="R")
        pdf.ln(5.6)
    pdf.set_text_color(0, 0, 0)


def _trend(pdf, series: list[dict]) -> bool:
    """A mini bar chart of net worth over time. False if too few points."""
    pts = [(s.get("date", ""), s["total"]) for s in series
           if isinstance(s.get("total"), (int, float))]
    if len(pts) < 2:
        return False
    pts = pts[-10:]
    x0, y0, w, h = pdf.l_margin, pdf.get_y(), pdf.epw, 24.0
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(pts)
    gap = 2.0
    bw = (w - gap * (n - 1)) / n
    base = y0 + h
    for i, (_, v) in enumerate(pts):
        frac = (v - lo) / rng
        bh = 3.0 + frac * (h - 3.0)
        x = x0 + i * (bw + gap)
        pdf.set_fill_color(*_GREEN)
        pdf.rect(x, base - bh, bw, bh, "F")
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(*_MUTED)
    pdf.set_xy(x0, base + 1)
    pdf.cell(w / 2, 4, _clean(f"{pts[0][0]}: {_money(pts[0][1])}"))
    pdf.set_xy(x0 + w / 2, base + 1)
    pdf.cell(w / 2, 4, _clean(f"{pts[-1][0]}: {_money(pts[-1][1])}"), align="R")
    pdf.set_text_color(0, 0, 0)
    pdf.set_xy(x0, base + 6)
    return True


def _composition(pdf, assets: float, liabilities: float) -> None:
    """A single stacked bar: assets (green) vs liabilities (red)."""
    total = (assets or 0) + (liabilities or 0)
    if total <= 0:
        return
    x0, y, w, h = pdf.l_margin, pdf.get_y(), pdf.epw, 8.0
    aw = w * assets / total
    pdf.set_fill_color(*_GREEN)
    pdf.rect(x0, y, aw, h, "F")
    pdf.set_fill_color(*_RED)
    pdf.rect(x0 + aw, y, w - aw, h, "F")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_GREEN)
    pdf.set_xy(x0, y + h + 1)
    pdf.cell(w / 2, 4, _clean(f"Assets {_money(assets)}"))
    pdf.set_text_color(*_RED)
    pdf.set_xy(x0 + w / 2, y + h + 1)
    pdf.cell(w / 2, 4, _clean(f"Liabilities {_money(liabilities)}"), align="R")
    pdf.set_text_color(0, 0, 0)
    pdf.set_xy(x0, y + h + 6)


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
    tw, lw = data["this_week"], data["last_week"]
    tm, pm = data["this_month"], data["prior_month"]

    # A row of headline tiles up top.
    _stat_tiles(pdf, [
        (_money(nw.get("total")), "Net worth", _GREEN),
        (_money(-tw["spend"]) if tw["spend"] else "$0", "Spent this week", _AMBER),
        (_money(-tm["spend"]) if tm["spend"] else "$0", "Spent (30 days)", _BLUE),
        (_money(tm["income"]), "Income (30 days)", _TEAL),
    ])

    heading("Net worth")
    if _trend(pdf, data.get("net_worth_history") or []):
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*_MUTED)
        pdf.cell(0, 4, "Net worth over the last six months.", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(1)
    _composition(pdf, data.get("assets_total") or 0.0, data.get("liabilities_total") or 0.0)
    pdf.ln(1)

    heading("Where this week's money went")
    categories = list(tw["by_category"].items())[:8]
    if categories:
        _bar_chart(pdf, [(cat, amount) for cat, amount in categories], colored=True)
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 5, "No categorized spending recorded this week.",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    heading("This week vs last  -  this month vs prior")
    _bar_chart(pdf, [
        ("This week", tw["spend"]),
        ("Last week", lw["spend"]),
        ("This month", tm["spend"]),
        ("Prior month", pm["spend"]),
    ])
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

    pending_mentions = data.get("pending_mentions") or []
    if pending_mentions:
        total = sum(p["amount"] for p in pending_mentions)
        heading(f"Mentioned, awaiting statement: {_money(abs(total))}")
        pdf.set_font("Helvetica", "", 9)
        for p in pending_mentions[:6]:
            marker = "  (!) overdue - ask for a fresh export" if p.get("stale") else ""
            pdf.cell(0, 5, _clean(
                f"  {p['mentioned_on']}: {p['description']} ~{_money(abs(p['amount']))}"
                f" on {p.get('account_hint') or 'a card'}{marker}"
            ), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 5, "Spending you told me about that no statement has confirmed yet"
                 " - mostly Apple Card activity between Wallet exports.",
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


def render_report_pdf(title: str, sections: list[dict], path: Path) -> Path:
    """A titled, multi-section document the copilot composes on demand — a debt
    plan, a net-worth one-pager, a paid-vs-remaining ledger. Same latin-1
    sanitizing and auto page-breaks as the weekly report."""
    from fpdf import FPDF

    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 17)
    pdf.multi_cell(0, 9, _clean(title or "From your copilot"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, _clean(f"Prepared by the household copilot - {date.today().isoformat()}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)
    for section in sections:
        heading = _clean(str(section.get("heading", "")).strip())
        body = _clean(str(section.get("body", "")).strip())
        if heading:
            pdf.set_font("Helvetica", "B", 12)
            pdf.multi_cell(0, 7, heading, new_x="LMARGIN", new_y="NEXT")
        if body:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5.5, body, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
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
