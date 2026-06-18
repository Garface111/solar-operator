"""One-shot: send Bruce the v1.8.0 EnergyAgent extension zip with install instructions.

What's new in v1.8: adds SolarEdge inverter-account capture (for the Array
Operator monitoring side) on top of the existing Green Mountain Power / SmartHub
bill capture. GMP capture behaviour is unchanged — this is purely additive.
Not yet on the Chrome Web Store, so Dad side-loads the dev build as usual.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from api.notify import send_workbook_email  # noqa: E402

TO = "bruce.genereaux@gmail.com"
SUBJECT = "EnergyAgent extension — please install v1.8"

ZIP_PATH = ("/mnt/c/Users/fordg/Desktop/Solar Operator/"
            "Archives - Extension Builds/solar-operator-extension-v1.8.0.zip")
ZIP_NAME = "solar-operator-extension-v1.8.0.zip"

HTML = """\
<div style="font-family: Georgia, serif; font-size: 15px; color: #1a1a1a; line-height: 1.55; max-width: 560px;">
<p>Hi Dad,</p>

<p>New version of the EnergyAgent extension — v1.8. This one adds the ability
to capture your SolarEdge inverter monitoring (the array side), on top of the
Green Mountain Power bill capture you already have. Your GMP setup keeps working
exactly the same; this just adds to it.</p>

<p><b>Install (same drill as before):</b></p>

<ol>
  <li>Save the attached <code>solar-operator-extension-v1.8.0.zip</code>
      to your Desktop.</li>
  <li>Right-click &rarr; <b>Extract All&hellip;</b> &rarr; <b>Extract</b>.</li>
  <li>In Chrome, paste this into the address bar:
      <a href="chrome://extensions">chrome://extensions</a></li>
  <li>Top-right: make sure <b>Developer mode</b> is ON.</li>
  <li>Find the old <b>EnergyAgent</b> tile and click <b>Remove</b> so the old
      and new versions don't fight.</li>
  <li>Top-left: <b>Load unpacked</b> &rarr; pick the newly-extracted folder
      (the one named <code>solar-operator-extension-v1.8.0</code>).</li>
</ol>

<p><b>Pin it</b> (so it stays visible): puzzle-piece icon (&#129513;) at
top-right of Chrome &rarr; find <b>EnergyAgent</b> &rarr; click the pin.</p>

<p>Then everything works the same as before &mdash; log into
<a href="https://greenmountainpower.com/account/login/">greenmountainpower.com</a>
once per account and it grabs the bills automatically. For the SolarEdge piece,
just log into <a href="https://monitoring.solaredge.com/">monitoring.solaredge.com</a>
once and it links up.</p>

<p>Text me if anything looks off.</p>

<p>&mdash; Ford</p>
</div>
"""

TEXT = """\
Hi Dad,

New version of the EnergyAgent extension — v1.8. This one adds the ability to
capture your SolarEdge inverter monitoring (the array side), on top of the
Green Mountain Power bill capture you already have. GMP keeps working exactly
the same; this just adds to it.

INSTALL (same drill as before):
1. Save attached solar-operator-extension-v1.8.0.zip to your Desktop.
2. Right-click -> Extract All -> Extract.
3. In Chrome, paste chrome://extensions into the address bar.
4. Top-right: Developer mode ON.
5. Find the OLD EnergyAgent tile, click Remove first.
6. Top-left: Load unpacked -> pick the newly-extracted folder
   (solar-operator-extension-v1.8.0).

PIN: puzzle-piece at top-right of Chrome -> find EnergyAgent -> click the pin.

Then same as before — log into greenmountainpower.com once per account, it grabs
the bills automatically. For SolarEdge, log into monitoring.solaredge.com once
and it links up.

Text me if anything looks off.

— Ford
"""


def main() -> int:
    p = pathlib.Path(ZIP_PATH)
    if not p.exists():
        print(f"ERROR: zip not found at {ZIP_PATH}", file=sys.stderr)
        return 1
    ok = send_workbook_email(
        to=TO, subject=SUBJECT, html=HTML, text=TEXT,
        workbook_path=str(p), filename=ZIP_NAME,
    )
    print("sent" if ok else "FAILED")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
