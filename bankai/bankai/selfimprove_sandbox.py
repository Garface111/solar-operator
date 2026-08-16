"""Running agent-authored tests without trusting them.

A proposal's tests are code the copilot wrote — under this whole system's threat
model, code that a subverted turn could have written. So evaluation happens in a
throwaway copy of the source, executed as an UNPRIVILEGED user with NO network:

* no network (`unshare --net`) → a malicious test cannot exfiltrate anything or
  phone home, so even if it read something it could not send it;
* unprivileged user (`setpriv --reuid …`) → it cannot read the live secrets or
  database (root-owned) or touch anything outside its throwaway run directory;
* throwaway copy → the worst it can do is thrash a directory that is deleted
  after the run.

Nothing here is trusted until VERIFIED on the box: verify_sandbox() actually
tries to open a socket and read the live .env from inside the jail and refuses
to certify a sandbox where either succeeds. Evaluation is wired to auto-run only
when config.SELFIMPROVE_SANDBOX is set — and it should be set only after
verify_sandbox() passes.
"""
from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from . import config, selfimprove
from .models import CodeProposal

log = logging.getLogger("bankai.selfimprove.sandbox")

EVAL_TIMEOUT_SECONDS = 600


def _sandbox_prefix() -> list[str]:
    raw = (config.SELFIMPROVE_SANDBOX or "").strip()
    return shlex.split(raw) if raw else []


def configured() -> bool:
    return bool(_sandbox_prefix() and config.SELFIMPROVE_REPO_DIR
                and config.SELFIMPROVE_VENV_PYTHON)


def _run(argv: list[str], cwd: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def verify_sandbox() -> dict:
    """Prove the jail actually isolates BEFORE we trust it to run agent code.

    Runs two probes inside the configured sandbox prefix: opening a TCP socket
    (must FAIL — network cut) and reading the live .env (must FAIL — unprivileged
    / out of reach). A sandbox that lets either through is NOT certified."""
    prefix = _sandbox_prefix()
    if not prefix:
        return {"ok": False, "detail": "SELFIMPROVE_SANDBOX not set"}
    py = config.SELFIMPROVE_VENV_PYTHON or "python3"
    env_path = str(Path(config.BASE_DIR) / ".env")

    net_probe = (
        "import socket,sys\n"
        "try:\n"
        "    s=socket.create_connection(('1.1.1.1',53),timeout=4); s.close()\n"
        "    print('NET_OK'); sys.exit(0)\n"
        "except Exception as e:\n"
        "    print('NET_BLOCKED', type(e).__name__); sys.exit(3)\n"
    )
    secret_probe = (
        "import sys\n"
        f"open({env_path!r}).read(); print('SECRET_READ'); sys.exit(0)\n"
    )
    try:
        net = _run(prefix + [py, "-c", net_probe], timeout=20)
        secret = _run(prefix + [py, "-c", secret_probe], timeout=20)
    except Exception as exc:
        return {"ok": False, "detail": f"probe failed to launch: {exc}"}

    network_blocked = "NET_OK" not in net.stdout
    secret_blocked = "SECRET_READ" not in secret.stdout
    return {
        "ok": network_blocked and secret_blocked,
        "network_blocked": network_blocked,
        "secret_blocked": secret_blocked,
        "net_probe": (net.stdout + net.stderr).strip()[:200],
        "secret_probe": (secret.stdout + secret.stderr).strip()[:200],
    }


def _prepare_run_dir(proposal: CodeProposal) -> Path:
    """A fresh throwaway copy of the base clone with the proposal applied.

    The base clone (SELFIMPROVE_REPO_DIR) is a trusted checkout kept current at
    deploy time. We copy it, overlay the proposed files, and hand the copy to
    the jail — the base is never mutated by agent code."""
    base = Path(config.SELFIMPROVE_REPO_DIR)
    run_dir = base.parent / f"run-{proposal.id}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    shutil.copytree(base, run_dir, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    for rel, content in selfimprove.files_of(proposal).items():
        selfimprove.safe_repo_path(rel)  # defense in depth: never write out of scope
        target = run_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return run_dir


def evaluate(session: Session, proposal_id: str) -> dict:
    """Apply a proposal in a throwaway copy and run its tests in the jail."""
    proposal = session.get(CodeProposal, proposal_id)
    if proposal is None:
        return {"error": "no such proposal"}
    if not configured():
        proposal.status = "awaiting_sandbox"
        proposal.test_output = (
            "No verified sandbox configured, so these tests were NOT run — "
            "agent-authored tests execute only in a network-cut, unprivileged "
            "jail. A trusted reviewer should run them, or configure "
            "SELFIMPROVE_SANDBOX after verify_sandbox() passes."
        )
        session.flush()
        return {"status": "awaiting_sandbox", "id": proposal_id}

    prefix = _sandbox_prefix()
    py = config.SELFIMPROVE_VENV_PYTHON
    tests = proposal.test_paths.replace(",", " ").split() or ["tests"]
    run_dir = _prepare_run_dir(proposal)
    try:
        # Make the throwaway readable+writable by the unprivileged jail user.
        if config.SELFIMPROVE_SANDBOX_USER:
            _run(["chown", "-R", config.SELFIMPROVE_SANDBOX_USER, str(run_dir)], timeout=60)
        proc = _run(
            prefix + [py, "-m", "pytest", *tests, "-q"],
            cwd=str(run_dir), timeout=EVAL_TIMEOUT_SECONDS,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        passed = proc.returncode == 0
    except subprocess.TimeoutExpired:
        output, passed = "TIMEOUT: tests exceeded the evaluation budget", False
    except Exception as exc:  # noqa: BLE001 — a broken eval must not crash the loop
        log.exception("sandbox evaluation error")
        output, passed = f"evaluation error: {exc}", False
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    proposal.status = "passed" if passed else "failed"
    proposal.test_output = output[-8000:]
    proposal.evaluated_at = datetime.utcnow()
    session.flush()
    return {"status": proposal.status, "id": proposal_id}
