"""Email Bruce the v1.9.40 EnergyAgent extension as a DOWNLOAD LINK.
BCC Ford for delivery proof.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from api.notify import _send_via_resend  # noqa: E402

TO = "bruce.genereaux@gmail.com"
BCC_FORD = "ford.genereaux@gmail.com"
SUBJECT = "EnergyAgent v1.9.40 — latest build"
DL = "https://github.com/Garface111/solar-operator/releases/download/ext-v1.9.40/energyagent-extension-v1.9.40.zip"
FOLDER = "energyagent-extension-v1.9.40"

HTML = f"""\
<div style="font-family: Georgia, serif; font-size: 15px; color: #1a1a1a; line-height: 1.55; max-width: 560px;">
<p>Hi Dad,</p>

<p>Here's the latest EnergyAgent extension &mdash; <b>v1.9.40</b>. This build adds
per-inverter history backfill and sparklines for Fronius and SMA, real
daily-history backfill for Chint, plus a bunch of capture reliability fixes.
SolarEdge, Fronius, SMA, Chint, and Green Mountain Power all keep working.</p>

<p style="margin: 18px 0;">
  <a href="{DL}" style="background:#2e7d32;color:#fff;padding:11px 18px;
     border-radius:6px;text-decoration:none;font-family:Arial,sans-serif;
     font-size:15px;">Download EnergyAgent v1.9.40 (.zip)</a>
</p>

<p>If the button doesn't work, copy-paste this into your browser:<br>
<span style="font-family:monospace;font-size:12px;word-break:break-all;">{DL}</span></p>

<p><b>Install (same drill as before):</b></p>
<ol>
  <li>Click the link above to download the zip (it'll go to your Downloads).</li>
  <li>Right-click it &rarr; <b>Extract All&hellip;</b> &rarr; <b>Extract</b>.</li>
  <li>In Chrome, paste <code>chrome://extensions</code> into the address bar.</li>
  <li>Top-right: make sure <b>Developer mode</b> is ON.</li>
  <li>Find the old <b>EnergyAgent</b> tile and click <b>Remove</b> first.</li>
  <li>Top-left: <b>Load unpacked</b> &rarr; pick the extracted
      <code>{FOLDER}</code> folder.</li>
</ol>

<p><b>Pin it:</b> puzzle-piece icon (&#129513;) top-right of Chrome &rarr;
find <b>EnergyAgent</b> &rarr; click the pin.</p>

<p>Text me if anything looks off.</p>
<p>&mdash; Ford</p>
</div>
"""

TEXT = f"""\
Hi Dad,

Here's the latest EnergyAgent extension -- v1.9.40. This build adds per-inverter
history backfill and sparklines for Fronius and SMA, real daily-history backfill
for Chint, plus capture reliability fixes. SolarEdge, Fronius, SMA, Chint, and
Green Mountain Power all keep working.

Download link:
{DL}

INSTALL (same drill as before):
1. Click the link to download the zip.
2. Right-click -> Extract All -> Extract.
3. In Chrome, paste chrome://extensions into the address bar.
4. Top-right: Developer mode ON.
5. Find the OLD EnergyAgent tile, click Remove first.
6. Top-left: Load unpacked -> pick the extracted {FOLDER} folder.

PIN: puzzle-piece (top-right of Chrome) -> EnergyAgent -> pin.

Text me if anything looks off.
-- Ford
"""


def main() -> int:
    ok = _send_via_resend(to=[TO, BCC_FORD], subject=SUBJECT, html=HTML, text=TEXT)
    err = getattr(_send_via_resend, "_last_error", None)
    print("sent" if ok else f"FAILED: {err}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
