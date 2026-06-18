"""Send Bruce the v1.9.3 EnergyAgent extension as a DOWNLOAD LINK (not an
attachment). GitHub release link, BCC Ford for delivery proof.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from api.notify import _send_via_resend  # noqa: E402

TO = "bruce.genereaux@gmail.com"
BCC_FORD = "ford.genereaux@gmail.com"
SUBJECT = "EnergyAgent extension v1.9.3 — download link"
DL = "https://github.com/Garface111/solar-operator/releases/download/ext-v1.9.3/solar-operator-extension-v1.9.3.zip"

HTML = f"""\
<div style="font-family: Georgia, serif; font-size: 15px; color: #1a1a1a; line-height: 1.55; max-width: 560px;">
<p>Hi Dad,</p>

<p>Latest EnergyAgent extension — v1.9.3. Here's the download link:</p>

<p style="margin: 18px 0;">
  <a href="{DL}" style="background:#2e7d32;color:#fff;padding:11px 18px;
     border-radius:6px;text-decoration:none;font-family:Arial,sans-serif;
     font-size:15px;">Download EnergyAgent v1.9.3 (.zip)</a>
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
      <code>solar-operator-extension-v1.9.3</code> folder.</li>
</ol>

<p><b>Pin it:</b> puzzle-piece icon (&#129513;) top-right of Chrome &rarr;
find <b>EnergyAgent</b> &rarr; click the pin.</p>

<p>This one fixes the SMA (Sunny Portal) connection &mdash; it now pulls in
<b>every</b> plant on your account automatically, not just one. SolarEdge,
Fronius, and Green Mountain Power all keep working exactly the same.</p>

<p>Text me if anything looks off.</p>
<p>&mdash; Ford</p>
</div>
"""

TEXT = f"""\
Hi Dad,

Latest EnergyAgent extension — v1.9.3. Download link:

{DL}

INSTALL (same drill as before):
1. Click the link to download the zip.
2. Right-click -> Extract All -> Extract.
3. In Chrome, paste chrome://extensions into the address bar.
4. Top-right: Developer mode ON.
5. Find the OLD EnergyAgent tile, click Remove first.
6. Top-left: Load unpacked -> pick the extracted
   solar-operator-extension-v1.9.3 folder.

PIN: puzzle-piece (top-right of Chrome) -> EnergyAgent -> pin.

This one fixes the SMA (Sunny Portal) connection -- it now pulls in EVERY plant
on your account automatically, not just one. SolarEdge, Fronius, and Green
Mountain Power all keep working the same.

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
