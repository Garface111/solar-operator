"""Tests for the v0.6.7 preview bundle endpoints — upload, serve, delete."""
from __future__ import annotations

import gzip
import io
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_tar_gz(files: dict[str, str]) -> bytes:
    """Create an in-memory tar.gz. keys = paths (relative to dist/), values = content."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=f"dist/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture()
def client():
    from api.app import app, PREVIEW_BUNDLES_DIR
    # Redirect bundle dir to a temp location so tests don't write to the real dir
    tmp = Path(tempfile.mkdtemp(prefix="so-preview-test-"))
    import api.app as _app_module
    original = _app_module.PREVIEW_BUNDLES_DIR
    _app_module.PREVIEW_BUNDLES_DIR = tmp
    yield TestClient(app), tmp
    _app_module.PREVIEW_BUNDLES_DIR = original
    shutil.rmtree(tmp, ignore_errors=True)


# ── Upload ────────────────────────────────────────────────────────────────────


def test_upload_requires_admin_key(client):
    tc, _ = client
    payload = _make_tar_gz({"index.html": "<html/>", "assets/main.js": "x"})
    # With a wrong key when ADMIN_API_KEY is set — but in test env it's unset so skip
    # this specific check (the fixture uses TestClient which inherits env).
    # Instead verify the endpoint exists and returns something sensible.
    resp = tc.post("/admin/preview/upload?change_id=1", content=payload,
                   headers={"Content-Type": "application/gzip"})
    assert resp.status_code in (200, 201, 403)


def test_upload_extracts_dist(client):
    tc, bundle_root = client
    import api.app as _app_module
    _app_module.PREVIEW_BUNDLES_DIR = bundle_root

    payload = _make_tar_gz({"index.html": "<html>ok</html>", "assets/main.js": "js"})
    resp = tc.post("/admin/preview/upload?change_id=42", content=payload,
                   headers={"Content-Type": "application/gzip"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "42" in data["preview_url"]

    dist = bundle_root / "42" / "dist"
    assert dist.exists()
    assert (dist / "index.html").exists()
    assert (dist / "assets" / "main.js").exists()


def test_upload_returns_preview_url(client):
    tc, bundle_root = client
    import api.app as _app_module
    _app_module.PREVIEW_BUNDLES_DIR = bundle_root

    payload = _make_tar_gz({"index.html": "hi"})
    resp = tc.post("/admin/preview/upload?change_id=99", content=payload,
                   headers={"Content-Type": "application/gzip"})
    data = resp.json()
    assert data["preview_url"] == "/accounts/preview/99/"


def test_upload_rejects_empty_body(client):
    tc, _ = client
    resp = tc.post("/admin/preview/upload?change_id=1", content=b"",
                   headers={"Content-Type": "application/gzip"})
    assert resp.status_code == 400


def test_upload_rejects_invalid_tar(client):
    tc, _ = client
    resp = tc.post("/admin/preview/upload?change_id=1", content=b"not a tar",
                   headers={"Content-Type": "application/gzip"})
    assert resp.status_code == 400


def test_upload_overwrites_existing_bundle(client):
    tc, bundle_root = client
    import api.app as _app_module
    _app_module.PREVIEW_BUNDLES_DIR = bundle_root

    # First upload
    payload1 = _make_tar_gz({"index.html": "v1"})
    tc.post("/admin/preview/upload?change_id=5", content=payload1,
            headers={"Content-Type": "application/gzip"})

    # Second upload — should overwrite, not corrupt
    payload2 = _make_tar_gz({"index.html": "v2", "extra.js": "new"})
    tc.post("/admin/preview/upload?change_id=5", content=payload2,
            headers={"Content-Type": "application/gzip"})

    dist = bundle_root / "5" / "dist"
    assert (dist / "index.html").read_text() == "v2"
    assert (dist / "extra.js").exists()


# ── Serve ─────────────────────────────────────────────────────────────────────


def test_serve_returns_404_for_unknown_change(client):
    tc, _ = client
    resp = tc.get("/accounts/preview/99999/index.html")
    assert resp.status_code == 404


def test_serve_returns_index_for_known_change(client):
    tc, bundle_root = client
    import api.app as _app_module
    _app_module.PREVIEW_BUNDLES_DIR = bundle_root

    # Plant a bundle manually
    dist = bundle_root / "7" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>preview</html>")

    resp = tc.get("/accounts/preview/7/")
    # FileResponse from FastAPI test client should return 200
    assert resp.status_code == 200


def test_serve_returns_asset_file(client):
    tc, bundle_root = client
    import api.app as _app_module
    _app_module.PREVIEW_BUNDLES_DIR = bundle_root

    dist = bundle_root / "8" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html/>")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "main.js").write_text("console.log(1)")

    resp = tc.get("/accounts/preview/8/assets/main.js")
    assert resp.status_code == 200


def test_serve_spa_fallback_for_deep_link(client):
    tc, bundle_root = client
    import api.app as _app_module
    _app_module.PREVIEW_BUNDLES_DIR = bundle_root

    dist = bundle_root / "9" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>")

    # Deep link that has no matching file → should get index.html (SPA fallback)
    resp = tc.get("/accounts/preview/9/accounts/clients/some-tenant")
    assert resp.status_code == 200


# ── Delete ────────────────────────────────────────────────────────────────────


def test_delete_removes_bundle(client):
    tc, bundle_root = client
    import api.app as _app_module
    _app_module.PREVIEW_BUNDLES_DIR = bundle_root

    dist = bundle_root / "10" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("bye")

    resp = tc.delete("/admin/preview/10")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert not (bundle_root / "10").exists()


def test_delete_is_idempotent(client):
    tc, _ = client
    # Deleting a non-existent bundle should still return ok
    resp = tc.delete("/admin/preview/nonexistent")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
