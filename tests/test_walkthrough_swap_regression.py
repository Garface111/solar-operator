"""
Regression test: walkthrough swap (commit 73c08fc).

Asserts that the OLD WalkthroughOverlay modal is fully gone from the source
tree and the NEW SandboxWalkthrough callout tour is the only first-run guide.

This is a STATIC-SOURCE test — no server, no browser, no network required.
It reads TypeScript source files directly with Python file I/O + regex.
It is the PRIMARY safety net: free, deterministic, and runs in ~1ms.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "web" / "app" / "src"


def _all_ts_files() -> list[Path]:
    return list(SRC_ROOT.rglob("*.tsx")) + list(SRC_ROOT.rglob("*.ts"))


# ── 1. Deleted files must not exist ──────────────────────────────────────────

def test_walkthrough_overlay_tsx_deleted() -> None:
    path = SRC_ROOT / "components" / "WalkthroughOverlay.tsx"
    assert not path.exists(), (
        f"{path.relative_to(REPO_ROOT)} still exists — "
        "the old WalkthroughOverlay must be deleted (commit 73c08fc)."
    )


def test_walkthrough_lib_ts_deleted() -> None:
    path = SRC_ROOT / "lib" / "walkthrough.ts"
    assert not path.exists(), (
        f"{path.relative_to(REPO_ROOT)} still exists — "
        "the old walkthrough library must be deleted (commit 73c08fc)."
    )


# ── 2. No surviving imports of the deleted files ──────────────────────────────

def test_no_import_of_walkthrough_overlay() -> None:
    bad: list[str] = []
    for f in _all_ts_files():
        src = f.read_text(encoding="utf-8")
        if re.search(r'from\s+["\'].*WalkthroughOverlay["\']', src):
            bad.append(str(f.relative_to(REPO_ROOT)))
    assert not bad, (
        "These files still import WalkthroughOverlay and must be updated:\n"
        + "\n".join(f"  {p}" for p in bad)
    )


def test_no_import_of_lib_walkthrough() -> None:
    bad: list[str] = []
    for f in _all_ts_files():
        src = f.read_text(encoding="utf-8")
        # Match any import from the deleted lib/walkthrough module
        if re.search(r'from\s+["\'][^"\']*lib/walkthrough["\']', src):
            bad.append(str(f.relative_to(REPO_ROOT)))
    assert not bad, (
        "These files still import from lib/walkthrough and must be updated:\n"
        + "\n".join(f"  {p}" for p in bad)
    )


# ── 3. New walkthrough must exist and export the correct symbol ───────────────

def test_sandbox_walkthrough_exists() -> None:
    path = SRC_ROOT / "components" / "sandbox" / "SandboxWalkthrough.tsx"
    assert path.exists(), (
        f"{path.relative_to(REPO_ROOT)} does not exist — "
        "SandboxWalkthrough is missing."
    )


def test_sandbox_walkthrough_exports_symbol() -> None:
    path = SRC_ROOT / "components" / "sandbox" / "SandboxWalkthrough.tsx"
    src = path.read_text(encoding="utf-8")
    assert re.search(r"export\s+(function|const)\s+SandboxWalkthrough\b", src), (
        "SandboxWalkthrough.tsx does not export a 'SandboxWalkthrough' symbol."
    )


# ── 4. LS_KEY matches the documented value (invariant for localStorage compat) ─

def test_sandbox_walkthrough_ls_key_value() -> None:
    # Bumped sandbox-v2 → sandbox-v3 on Jun 6 2026 to re-fire the walkthrough
    # for returning operators whose v2 flag was set under the old "auto-done
    # at 3+ clients" rule. The bump is intentional, not a regression.
    expected_key = "so:walkthrough:sandbox-v3:done"
    path = SRC_ROOT / "components" / "sandbox" / "SandboxWalkthrough.tsx"
    src = path.read_text(encoding="utf-8")
    assert expected_key in src, (
        f"LS_KEY '{expected_key}' not found in SandboxWalkthrough.tsx — "
        "if the key changed again, update this test AND consider whether "
        "the bump was intentional (re-fires walkthrough for all users)."
    )


# ── 5. DashboardLayout must not reference WalkthroughOverlay ─────────────────

def test_dashboard_layout_clean() -> None:
    path = SRC_ROOT / "screens" / "DashboardLayout.tsx"
    src = path.read_text(encoding="utf-8")
    assert "WalkthroughOverlay" not in src, (
        "DashboardLayout.tsx still references WalkthroughOverlay."
    )
    assert "lib/walkthrough" not in src, (
        "DashboardLayout.tsx still references lib/walkthrough."
    )
