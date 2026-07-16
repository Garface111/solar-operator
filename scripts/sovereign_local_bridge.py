#!/usr/bin/env python3
"""Sovereign local computer bridge — run on Ford's machine (WSL/Linux/Mac).

Polls the Sovereign desk for local_tool tasks and executes them with a tight
allowlist (read / list / glob / shell / write under HOME or repo roots).

Usage:
  export SOVEREIGN_BRIDGE_TOKEN='…'   # must match Railway env
  export SOVEREIGN_API_BASE='https://web-production-49c83.up.railway.app'
  # optional:
  export SOVEREIGN_BRIDGE_ROOTS='/root,/home/ford'  # writable/readable roots
  python3 scripts/sovereign_local_bridge.py

Or with session auth instead of token:
  export SOVEREIGN_SESSION='…'  # so_session cookie value / bearer
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = (os.getenv("SOVEREIGN_API_BASE") or "https://web-production-49c83.up.railway.app").rstrip("/")
TOKEN = (os.getenv("SOVEREIGN_BRIDGE_TOKEN") or "").strip()
SESSION = (os.getenv("SOVEREIGN_SESSION") or "").strip()
POLL = float(os.getenv("SOVEREIGN_BRIDGE_POLL", "2.5"))
SHELL_TIMEOUT = int(os.getenv("SOVEREIGN_BRIDGE_SHELL_TIMEOUT", "120"))
ROOTS = [
    Path(p).expanduser().resolve()
    for p in (os.getenv("SOVEREIGN_BRIDGE_ROOTS") or str(Path.home())).split(",")
    if p.strip()
]
# Always allow the monorepo workspaces used by this agent box
for extra in ("/root", "/root/solar-operator", "/root/array-operator"):
    p = Path(extra)
    if p.exists() and p.resolve() not in ROOTS:
        ROOTS.append(p.resolve())


def _headers() -> dict:
    h = {"Content-Type": "application/json", "User-Agent": "sovereign-local-bridge/1.0"}
    if TOKEN:
        h["X-Bridge-Token"] = TOKEN
    if SESSION:
        h["Authorization"] = f"Bearer {SESSION}"
    return h


def _http(method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API}{path}", data=data, headers=_headers(), method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code}: {err}") from e


def _allowed(path: Path) -> bool:
    try:
        rp = path.expanduser().resolve()
    except Exception:
        return False
    for root in ROOTS:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def run_task(tool: str, args: dict) -> dict:
    tool = (tool or "").lower()
    args = args or {}
    if tool == "shell":
        cmd = str(args.get("cmd") or args.get("command") or "").strip()
        if not cmd:
            return {"ok": False, "error": "empty cmd"}
        # Hard bans
        low = cmd.lower()
        for bad in ("rm -rf /", "mkfs", ":(){", "shutdown", "reboot", "dd if="):
            if bad in low:
                return {"ok": False, "error": f"blocked command pattern: {bad}"}
        cwd = Path(args.get("cwd") or Path.home()).expanduser()
        if not _allowed(cwd):
            return {"ok": False, "error": f"cwd not in allowed roots: {cwd}"}
        p = subprocess.run(
            cmd, shell=True, cwd=str(cwd),
            capture_output=True, text=True, timeout=SHELL_TIMEOUT,
        )
        return {
            "ok": p.returncode == 0,
            "summary": (p.stdout or p.stderr or "")[:4000],
            "stdout": (p.stdout or "")[:20000],
            "stderr": (p.stderr or "")[:8000],
            "returncode": p.returncode,
            "cwd": str(cwd),
        }
    if tool == "read":
        path = Path(str(args.get("path") or "")).expanduser()
        if not _allowed(path):
            return {"ok": False, "error": f"path not allowed: {path}"}
        if not path.is_file():
            return {"ok": False, "error": f"not a file: {path}"}
        data = path.read_bytes()[:200_000]
        try:
            text = data.decode("utf-8")
        except Exception:
            text = data.decode("latin-1", errors="replace")
        return {"ok": True, "path": str(path), "content": text[:100_000], "summary": text[:2000]}
    if tool == "list":
        path = Path(str(args.get("path") or ".")).expanduser()
        if not _allowed(path):
            return {"ok": False, "error": f"path not allowed: {path}"}
        if not path.is_dir():
            return {"ok": False, "error": f"not a dir: {path}"}
        entries = []
        for child in sorted(path.iterdir())[:200]:
            entries.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else None,
            })
        return {"ok": True, "path": str(path), "entries": entries, "summary": json.dumps(entries)[:2000]}
    if tool == "write":
        path = Path(str(args.get("path") or "")).expanduser()
        if not _allowed(path):
            return {"ok": False, "error": f"path not allowed: {path}"}
        content = str(args.get("content") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(path), "bytes": len(content.encode("utf-8")), "summary": f"wrote {path}"}
    if tool == "glob":
        import glob as _glob
        pattern = str(args.get("pattern") or "*")
        cwd = Path(args.get("cwd") or Path.home()).expanduser()
        if not _allowed(cwd):
            return {"ok": False, "error": f"cwd not allowed: {cwd}"}
        matches = _glob.glob(str(cwd / pattern), recursive=True)[:200]
        # filter to allowed roots
        safe = [m for m in matches if _allowed(Path(m))]
        return {"ok": True, "matches": safe, "summary": "\n".join(safe[:40])}
    return {"ok": False, "error": f"unknown tool: {tool}"}


def main() -> int:
    if not TOKEN and not SESSION:
        print("Set SOVEREIGN_BRIDGE_TOKEN (preferred) or SOVEREIGN_SESSION", file=sys.stderr)
        return 2
    print(f"Sovereign bridge → {API}")
    print(f"Allowed roots: {', '.join(str(r) for r in ROOTS)}")
    while True:
        try:
            data = _http("GET", "/v1/sovereign/desk/bridge/pending?limit=3")
            tasks = data.get("tasks") or []
            if not tasks:
                time.sleep(POLL)
                continue
            for t in tasks:
                tid = t.get("id")
                tool = t.get("tool") or "shell"
                args = t.get("args") or {}
                print(f"→ {tid} {tool} {json.dumps(args)[:120]}")
                try:
                    result = run_task(tool, args)
                    ok = bool(result.get("ok"))
                    _http("POST", "/v1/sovereign/desk/bridge/result", {
                        "task_id": tid,
                        "ok": ok,
                        "result": result,
                        "error": None if ok else result.get("error"),
                    })
                    print(f"  ✓ posted ({'ok' if ok else 'fail'})")
                except Exception as e:
                    print(f"  ✗ {e}")
                    try:
                        _http("POST", "/v1/sovereign/desk/bridge/result", {
                            "task_id": tid,
                            "ok": False,
                            "result": {},
                            "error": str(e)[:500],
                        })
                    except Exception:
                        pass
        except KeyboardInterrupt:
            print("bye")
            return 0
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
            time.sleep(max(POLL, 5))


if __name__ == "__main__":
    raise SystemExit(main())
