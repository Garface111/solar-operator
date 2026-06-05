"""Follow-up: send Bruce the v1.4.1 extension (replaces the v1.4.0 zip
sent earlier tonight). Same install instructions, new attachment.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from api.notify import send_workbook_email  # noqa: E402

TO = "bruce.genereaux@gmail.com"
SUBJECT = "Solar Operator extension — use this version instead (v1.4.1)"

ZIP_PATH = "/tmp/solar-operator-extension-v1.4.1.zip"
ZIP_NAME = "solar-operator-extension-v1.4.1.zip"

HTML = """\
<div style="font-family: Georgia, serif; font-size: 15px; color: #1a1a1a; line-height: 1.55; max-width: 560px;">
<p>Hi Dad,</p>

<p>Quick follow-up to the email I sent a few hours ago — I shipped a
better version of the extension since then. <b>Please use this one
(v1.4.1) instead</b> and toss the earlier zip. The new version makes
adding multiple clients in one sitting much smoother (it clears the
utility portal session between sign-ins so you never accidentally
re-capture the same account).</p>

<p>Same install steps as before:</p>

<ol>
  <li>Save the attached <code>solar-operator-extension-v1.4.1.zip</code> to
      your Desktop.</li>
  <li>Right-click → <b>Extract All…</b> → <b>Extract</b>.</li>
  <li>In Chrome, go to
      <a href="chrome://extensions">chrome://extensions</a>
      (copy/paste that into the address bar).</li>
  <li>Top-right: flip <b>Developer mode</b> ON.</li>
  <li>Top-left: <b>Load unpacked</b> → pick the extracted folder.</li>
  <li>If you already loaded the earlier v1.4.0 version, click
      <b>Remove</b> on it first so they don't both run.</li>
</ol>

<p><b>Pin it</b> (so it stays visible):
puzzle-piece icon (🧩) at top-right of Chrome → find
<b>Solar Operator Sync</b> → click the pin.</p>

<p><b>Then:</b> log into
<a href="https://greenmountainpower.com/account/login/">greenmountainpower.com</a>
once per account you want me to track. The extension captures bills
automatically — you don't have to click anything.</p>

<p>Text me if anything looks off.</p>

<p>— Ford</p>
</div>
"""

TEXT = """\
Hi Dad,

Quick follow-up to the email from earlier tonight — I shipped a better
version of the extension. PLEASE USE THIS ONE (v1.4.1) instead and
toss the earlier zip. The new version makes adding multiple clients in
one sitting much smoother (it clears the utility portal session between
sign-ins so you don't accidentally re-capture the same account).

INSTALL (same as before):
1. Save attached solar-operator-extension-v1.4.1.zip to your Desktop.
2. Right-click → Extract All → Extract.
3. Chrome → chrome://extensions (paste into address bar).
4. Top-right: Developer mode ON.
5. Top-left: Load unpacked → pick the extracted folder.
6. If you already loaded the earlier v1.4.0, click Remove on it first
   so they don't both run.

PIN: puzzle-piece (🧩) at top-right of Chrome → find Solar Operator Sync
→ click the pin.

Then: log into greenmountainpower.com once per account you want me to
track. Extension captures bills automatically.

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
