#!/usr/bin/env python3
"""
Local dev server for testing the Solar Operator Chrome extension.

Run: python3 dev_server.py
Then in the extension Options, set:
  API endpoint: http://localhost:8787/v1/sync
  Tenant key:   dev-test-tenant

Captures land in ./captures/<timestamp>.json so you can inspect what the
extension is actually sending. This is the same payload the production API
will receive.
"""
import json, pathlib, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

CAPTURES = pathlib.Path(__file__).parent / "captures"
CAPTURES.mkdir(exist_ok=True)

class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/v1/sync":
            self.send_response(404); self._cors(); self.end_headers(); return
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8")
        try:
            payload = json.loads(raw)
        except Exception as e:
            self.send_response(400); self._cors(); self.end_headers()
            self.wfile.write(json.dumps({"error": f"bad json: {e}"}).encode()); return

        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        tenant = self.headers.get("Authorization", "anonymous").replace("Bearer ", "")
        fname = CAPTURES / f"{ts}_{tenant}.json"
        fname.write_text(json.dumps(payload, indent=2))
        print(f"[{ts}] captured {payload.get('provider','?')} session: "
              f"{payload.get('user',{}).get('username','?')} "
              f"({len(payload.get('accounts',[]))} accounts) -> {fname.name}")

        self.send_response(200); self._cors()
        self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({
            "ok": True,
            "tenant": tenant,
            "captured": fname.name,
            "accounts": len(payload.get("accounts", [])),
        }).encode())

    def log_message(self, *a, **kw):
        pass  # quiet

if __name__ == "__main__":
    port = 8787
    print(f"Solar Operator dev server: http://localhost:{port}/v1/sync")
    print(f"Captures land in: {CAPTURES}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
