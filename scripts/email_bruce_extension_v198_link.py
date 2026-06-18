"""Email Bruce the v1.9.8 EnergyAgent extension as a DOWNLOAD LINK, highlighting
that SMA / Sunny Portal capture now works. BCC Ford for delivery proof.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from api.notify import _send_via_resend  # noqa: E402

TO = "bruce.genereaux@gmail.com"
BCC_FORD = "ford.genereaux@gmail.com"
SUBJECT = "EnergyAgent v1.9.8 — SMA / Sunny Portal now works"
DL = "https://github.com/Garface111/solar-operator/releases/download/ext-v1.9.8/solar-operator-extension-v1.9.8.zip"

HTML = f"""\
<div style="font-family: Georgia, serif; font-size: 15px; color: #1a1a1a; line-height: 1.55; max-width: 560px;">
<p>Hi Dad,</p>

<p>New EnergyAgent extension &mdash; v1.9.8. <b>The big news: SMA (Sunny Portal)
now works.</b> It pulls in every plant and every inverter on your SMA account
automatically. SolarEdge, Fronius, and Green Mountain Power all keep working
exactly the same.</p>

<p style="margin: 18px 0;">
  <a href="{DL}" style="background:#2e7d32;color:#fff;padding:11px 18px;
     border-radius:6px;text-decoration:none;font-family:Arial,sans-serif;
     font-size:15px;">Download EnergyAgent v1.9.8 (.zip)</a>
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
      <code>solar-operator-extension-v1.9.8</code> folder.</li>
</ol>

<p><b>Pin it:</b> puzzle-piece icon (&#129513;) top-right of Chrome &rarr;
find <b>EnergyAgent</b> &rarr; click the pin.</p>

<p>To connect SMA: in Array Operator click <b>Add array &rarr; Log in with SMA</b>,
sign in to Sunny Portal in the tab that opens, then flip back &mdash; your plants
should appear on the canvas.</p>

<p>Text me if anything looks off.</p>
<p>&mdash; Ford</p>
</div>
"""

TEXT = f"""\
Hi Dad,

New EnergyAgent extension -- v1.9.8. The big news: SMA (Sunny Portal) now works.
It pulls in every plant and inverter on your SMA account automatically. SolarEdge,
Fronius, and Green Mountain Power all keep working the same.

Download link:
{DL}

INSTALL (same drill as before):
1. Click the link to download the zip.
2. Right-click -> Extract All -> Extract.
3. In Chrome, paste chrome://extensions into the address bar.
4. Top-right: Developer mode ON.
5. Find the OLD EnergyAgent tile, click Remove first.
6. Top-left: Load unpacked -> pick the extracted
   solar-operator-extension-v1.9.8 folder.

PIN: puzzle-piece (top-right of Chrome) -> EnergyAgent -> pin.

To connect SMA: in Array Operator click Add array -> Log in with SMA, sign in to
Sunny Portal in the tab that opens, then flip back -- your plants should appear.

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
