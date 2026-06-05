"""One-shot: email Bruce the latest extension zip with install instructions.

Run inside Railway:
    railway ssh "cd /app && python -m scripts.email_bruce_extension"

Reads the v1.4.0 zip uploaded alongside this script (placed there by
the runner step above). Uses api.notify.send_workbook_email — same
Resend-backed sender the report pipeline uses — to ensure delivery.
"""
import sys
import pathlib

# Adjust path so `api.notify` is importable when run as a module from /app.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from api.notify import send_workbook_email  # noqa: E402

TO = "bruce.genereaux@gmail.com"
SUBJECT = "Solar Operator extension — new version (install in 60 seconds)"

ZIP_PATH = "/tmp/solar-operator-extension-v1.4.0.zip"
ZIP_NAME = "solar-operator-extension-v1.4.0.zip"

HTML = """\
<div style="font-family: Georgia, serif; font-size: 15px; color: #1a1a1a; line-height: 1.55; max-width: 560px;">
<p>Hi Dad,</p>

<p>Attached is the latest version of the Solar Operator Chrome extension
(v1.4.0). It captures your GMP and Vermont Electric Co-op bills in the
background so the reports update themselves — no more logging in just
to download PDFs.</p>

<p><b>To install it (one time, takes about a minute):</b></p>

<ol>
  <li>Save the attached <code>solar-operator-extension-v1.4.0.zip</code> to
      your Desktop.</li>
  <li>Right-click the zip → <b>Extract All…</b> → click <b>Extract</b>.
      You'll end up with a folder of the same name.</li>
  <li>Open Chrome and go to
      <a href="chrome://extensions">chrome://extensions</a>
      (you may need to copy/paste — Chrome won't let me link there directly).</li>
  <li>In the <b>top-right corner</b> of that page, flip the
      <b>Developer mode</b> toggle <b>ON</b>.</li>
  <li>A new button appears on the top-left: <b>Load unpacked</b>.
      Click it, then pick the <b>extracted folder</b> from step 2
      (not the zip — the unzipped folder).</li>
  <li>You should see <b>"Solar Operator Sync"</b> appear in your
      extensions list. That's it.</li>
</ol>

<p><b>One last step — pin it so it stays visible:</b></p>
<ol>
  <li>Click the puzzle-piece icon (🧩) at the top-right of Chrome.</li>
  <li>Find <b>Solar Operator Sync</b> in the list.</li>
  <li>Click the little pin icon next to it. Now it lives in your
      Chrome toolbar.</li>
</ol>

<p><b>After installing:</b> just log into
<a href="https://greenmountainpower.com/account/login/">greenmountainpower.com</a>
once, like you normally would. The extension captures your bills
automatically — you don't have to click anything.</p>

<p>If anything looks off, text me and I'll screenshare.</p>

<p>— Ford</p>
</div>
"""

TEXT = """\
Hi Dad,

Attached is the latest version of the Solar Operator Chrome extension
(v1.4.0). It captures your GMP and Vermont Electric Co-op bills in the
background so the reports update themselves.

TO INSTALL (one time, ~1 minute):

1. Save the attached solar-operator-extension-v1.4.0.zip to your Desktop.
2. Right-click the zip → Extract All → Extract. You'll get a folder of
   the same name.
3. Open Chrome and go to chrome://extensions (copy/paste that into the
   address bar — Chrome blocks normal links there).
4. Top-right of that page: flip "Developer mode" ON.
5. Top-left: a "Load unpacked" button appears. Click it, then pick the
   EXTRACTED FOLDER from step 2 (not the zip — the unzipped folder).
6. "Solar Operator Sync" will appear in your extensions list. Done.

PIN IT so it stays visible:
1. Click the puzzle-piece (🧩) icon at top-right of Chrome.
2. Find "Solar Operator Sync".
3. Click the pin icon next to it.

AFTER INSTALLING: just log into greenmountainpower.com once like normal.
The extension captures your bills automatically — no clicking required.

Text me if anything looks off.

— Ford
"""


def main() -> int:
    p = pathlib.Path(ZIP_PATH)
    if not p.exists():
        print(f"ERROR: zip not found at {ZIP_PATH}", file=sys.stderr)
        return 1
    ok = send_workbook_email(
        to=TO,
        subject=SUBJECT,
        html=HTML,
        text=TEXT,
        workbook_path=str(p),
        filename=ZIP_NAME,
    )
    print("sent" if ok else "FAILED")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
