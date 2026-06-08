"""One-shot: send Bruce the v1.4.6 extension zip with install instructions.

Why v1.4.6: fixes the GMP "pretty print / detail not found" black-page bug.
Older versions wiped cookies but not localStorage, so the GMP SPA read a
stale auth token and rendered a raw 404 JSON in a new tab. v1.4.6 adds a
content script that clears localStorage too. Chrome Web Store push for
v1.4.6 is still in review, so dad needs to side-load the dev build.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from api.notify import send_workbook_email  # noqa: E402

TO = "bruce.genereaux@gmail.com"
SUBJECT = "Solar Operator extension — please install v1.4.6"

ZIP_PATH = "/tmp/solar-operator-extension-v1.4.6.zip"
ZIP_NAME = "solar-operator-extension-v1.4.6.zip"

HTML = """\
<div style="font-family: Georgia, serif; font-size: 15px; color: #1a1a1a; line-height: 1.55; max-width: 560px;">
<p>Hi Dad,</p>

<p>One more extension update — sorry for the churn. This one (v1.4.6) fixes
a bug where clicking through to Green Mountain Power could land you on a
black error page (you may or may not have hit it; either way this version
prevents it). After this we should be stable.</p>

<p><b>Install (same drill as last time):</b></p>

<ol>
  <li>Save the attached <code>solar-operator-extension-v1.4.6.zip</code>
      to your Desktop.</li>
  <li>Right-click → <b>Extract All…</b> → <b>Extract</b>.</li>
  <li>In Chrome, paste this into the address bar:
      <a href="chrome://extensions">chrome://extensions</a></li>
  <li>Top-right: make sure <b>Developer mode</b> is ON.</li>
  <li>Find the old <b>Solar Operator Sync</b> tile and click
      <b>Remove</b> so the old and new versions don't fight.</li>
  <li>Top-left: <b>Load unpacked</b> → pick the newly-extracted folder
      (the one named <code>solar-operator-extension-v1.4.6</code>).</li>
</ol>

<p><b>Pin it</b> (so it stays visible):
puzzle-piece icon (🧩) at top-right of Chrome → find
<b>Solar Operator Sync</b> → click the pin.</p>

<p>Then everything works the same as before — log into
<a href="https://greenmountainpower.com/account/login/">greenmountainpower.com</a>
once per account and the extension grabs the bills automatically.</p>

<p>Text me if anything looks off.</p>

<p>— Ford</p>
</div>
"""

TEXT = """\
Hi Dad,

One more extension update — sorry for the churn. This one (v1.4.6) fixes
a bug where clicking through to Green Mountain Power could land you on a
black error page. After this we should be stable.

INSTALL (same drill as last time):
1. Save attached solar-operator-extension-v1.4.6.zip to your Desktop.
2. Right-click → Extract All → Extract.
3. In Chrome, paste chrome://extensions into the address bar.
4. Top-right: Developer mode ON.
5. Find the OLD Solar Operator Sync tile, click Remove first.
6. Top-left: Load unpacked → pick the newly-extracted folder
   (solar-operator-extension-v1.4.6).

PIN: puzzle-piece (🧩) at top-right of Chrome → find Solar Operator Sync
→ click the pin.

Then same as before — log into greenmountainpower.com once per account,
extension grabs the bills automatically.

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
