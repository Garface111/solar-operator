"""Render an offtaker invoice in the OPERATOR'S OWN format (Stage 2).

The operator's template is token-HTML: their exact layout with `{{ tokens }}` for
the dynamic fields. Jinja2 fills it with the period's real data and xhtml2pdf turns
the result into a PDF.

GRACEFUL FILL (Ford's rule): a token the operator references but we don't actually
have renders BLANK — never a fabricated value, never an error. `ChainableUndefined`
makes `{{ unknown }}` and even `{{ unknown.nested }}` resolve to "" instead of
raising, so a template can ask for data we don't track and we simply show nothing
there rather than breaking the invoice. We only ever fill in what we truly know.
"""
from __future__ import annotations

import io
import logging

from jinja2 import BaseLoader, ChainableUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

logger = logging.getLogger(__name__)

# The template HTML is fully operator-controlled (they upload/paste their own
# invoice layout), so this MUST be sandboxed: a plain Environment lets a template
# reach `{{ self.__init__.__globals__ }}` and read process env (Stripe/DB creds)
# or execute code on the shared host. ImmutableSandboxedEnvironment blocks access
# to dunder attributes / globals and mutation of builtin types, so a malicious
# template can only ever touch the tokens we put in `context`.
#   autoescape       — customer/operator names can't inject markup
#   ChainableUndefined — unknown tokens render blank, not errors (graceful-fill)
_env = ImmutableSandboxedEnvironment(
    loader=BaseLoader(), undefined=ChainableUndefined, autoescape=True)


def _money(v):
    try:
        return "${:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return ""


def _num(v, nd=0):
    try:
        return ("{:,.%df}" % nd).format(float(v))
    except (TypeError, ValueError):
        return ""


def build_token_context(match, sub=None, tenant=None) -> dict:
    """Tokens available to the template. ONLY real, known values — anything we don't
    have is omitted, so ChainableUndefined renders it blank (never fabricated)."""
    inv = (getattr(match, "computed_invoice", None) or {})
    cust = (getattr(match, "customer", None) or {})
    name = cust.get("name") or inv.get("customer_name") or ""
    tariff = inv.get("tariff")
    ctx = {
        "offtaker_name": name,
        "customer_name": name,
        "offtaker_email": cust.get("email") or "",
        "invoice_number": inv.get("invoice_number") or "",
        "invoice_date": inv.get("invoice_date") or "",
        "due_date": inv.get("due_date") or "",
        "period_start": inv.get("period_start") or "",
        "period_end": inv.get("period_end") or "",
        "kwh": _num(inv.get("kwh")),
        "rate": ("${:.5f}".format(float(tariff)) if tariff not in (None, "") else ""),
        "amount_due": _money(inv.get("amount_owed")),
        "solar_value": _money(inv.get("solar_value")),
        "billed_value": _money(inv.get("billed_value")),
        "solar_savings": _money(inv.get("solar_savings")),
        "operator_name": (getattr(tenant, "company_name", None)
                          or getattr(tenant, "operator_name", None)
                          or inv.get("operator") or ""),
        "company_name": (getattr(tenant, "company_name", None) or ""),
    }
    return ctx


# The tokens we advertise in the UI (so operators know what they can place).
AVAILABLE_TOKENS = [
    "offtaker_name", "offtaker_email", "invoice_number", "invoice_date", "due_date",
    "period_start", "period_end", "kwh", "rate", "amount_due", "solar_value",
    "billed_value", "solar_savings", "operator_name", "company_name",
]


def render_template_pdf(html: str, context: dict) -> bytes:
    """Fill the token-HTML with `context` (graceful) and render to PDF via xhtml2pdf.
    Raises on a hard render failure so callers can fall back to the standard PDF."""
    from xhtml2pdf import pisa
    filled = _env.from_string(html or "").render(**(context or {}))
    out = io.BytesIO()
    result = pisa.CreatePDF(src=filled, dest=out, encoding="utf-8")
    if result.err:
        raise RuntimeError("xhtml2pdf reported %s render error(s)" % result.err)
    data = out.getvalue()
    if not data:
        raise RuntimeError("xhtml2pdf produced no output")
    return data


# Sample data for the Preview button (no real customer needed).
SAMPLE_CONTEXT = {
    "offtaker_name": "Sunnybrook Apartments", "customer_name": "Sunnybrook Apartments",
    "offtaker_email": "billing@sunnybrook.com",
    "invoice_number": "1001", "invoice_date": "2026-06-30", "due_date": "2026-07-28",
    "period_start": "2026-05-20", "period_end": "2026-06-18",
    "kwh": "56,400", "rate": "$0.21000", "amount_due": "$10,659.60",
    "solar_savings": "$1,184.40", "operator_name": "Acme Solar Co.",
    "company_name": "Acme Solar Co.",
}

# A clean default token-HTML invoice. Works out of the box and shows the operator
# every token in context, so they can tune it toward their own format. Kept to the
# CSS subset xhtml2pdf supports (tables, simple inline styles).
DEFAULT_TEMPLATE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  @page { size: letter; margin: 1in; }
  body { font-family: Helvetica, Arial, sans-serif; color: #1a2230; font-size: 11pt; }
  h1 { font-size: 20pt; margin: 0 0 2pt; }
  .muted { color: #5a6678; font-size: 10pt; }
  table { width: 100%; border-collapse: collapse; margin-top: 18pt; }
  th, td { text-align: left; padding: 7pt 6pt; border-bottom: 0.5pt solid #d7deea; }
  td.r, th.r { text-align: right; }
  .total td { font-size: 13pt; font-weight: bold; border-top: 1pt solid #1a2230; border-bottom: none; }
  .head td { border: none; padding: 0; }
</style></head>
<body>
  <table class="head"><tr>
    <td><h1>{{ operator_name }}</h1><div class="muted">Solar Power Invoice</div></td>
    <td class="r"><div class="muted">Invoice {{ invoice_number }}</div>
      <div class="muted">Date {{ invoice_date }}</div>
      <div class="muted">Due {{ due_date }}</div></td>
  </tr></table>

  <table class="head" style="margin-top:16pt"><tr>
    <td><div class="muted">Bill to</div><div><b>{{ offtaker_name }}</b></div>
      <div class="muted">{{ offtaker_email }}</div></td>
    <td class="r"><div class="muted">Service period</div>
      <div>{{ period_start }} &ndash; {{ period_end }}</div></td>
  </tr></table>

  <table>
    <tr><th>Description</th><th class="r">Amount</th></tr>
    <tr><td>Solar generation &mdash; {{ kwh }} kWh @ {{ rate }}/kWh</td>
        <td class="r">{{ amount_due }}</td></tr>
    <tr><td class="muted">Your solar savings this period</td>
        <td class="r muted">{{ solar_savings }}</td></tr>
    <tr class="total"><td>Amount due</td><td class="r">{{ amount_due }}</td></tr>
  </table>

  <p class="muted" style="margin-top:24pt">Please make payment to {{ operator_name }}.</p>
</body></html>"""
