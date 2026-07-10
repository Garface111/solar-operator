"""The suggestion lifecycle's PUBLIC status endpoint (self-improving product,
Tier 1). Pins: status-only exposure (nothing minable from an id-guesser),
admin-gated lifecycle ticks, enum tightness, and the building→shipped flow the
widget's pill rides on."""
import api.feature_suggestions as fsmod


def _mk(client):
    r = client.post("/v1/feature-suggestion", json={"text": "make the pill nicer"})
    assert r.status_code == 200 and r.json()["ok"]
    return r.json()["id"]


def test_public_status_exposes_only_the_status(client, monkeypatch):
    monkeypatch.setattr(fsmod, "ADMIN_API_KEY", "k")
    sid = _mk(client)
    r = client.get(f"/v1/feature-suggestion/{sid}/status")
    assert r.status_code == 200
    assert r.json() == {"status": "new"}          # exactly one key, nothing else
    assert client.get("/v1/feature-suggestion/99999999/status").status_code == 404


def test_lifecycle_building_then_shipped(client, monkeypatch):
    monkeypatch.setattr(fsmod, "ADMIN_API_KEY", "k")
    sid = _mk(client)
    # lifecycle tick is admin-gated
    r = client.post(f"/admin/feature-suggestions/{sid}/status", json={"status": "building"})
    assert r.status_code == 403
    r = client.post(f"/admin/feature-suggestions/{sid}/status?key=k", json={"status": "building"})
    assert r.status_code == 200
    assert client.get(f"/v1/feature-suggestion/{sid}/status").json() == {"status": "building"}
    # bogus statuses never enter the lifecycle
    r = client.post(f"/admin/feature-suggestions/{sid}/status?key=k", json={"status": "hacked"})
    assert r.status_code == 400
    # the review post flips to shipped when the harness verified the deploy
    r = client.post(f"/admin/feature-suggestions/{sid}/review?key=k",
                    json={"review": "auto-shipped", "status": "shipped"})
    assert r.status_code == 200
    assert client.get(f"/v1/feature-suggestion/{sid}/status").json() == {"status": "shipped"}


def test_review_post_rejects_off_enum_status(client, monkeypatch):
    monkeypatch.setattr(fsmod, "ADMIN_API_KEY", "k")
    sid = _mk(client)
    r = client.post(f"/admin/feature-suggestions/{sid}/review?key=k",
                    json={"review": "ok", "status": "totally-bogus"})
    assert r.status_code == 200                    # review lands…
    # …but the status degrades to the safe default instead of a free-text write
    assert client.get(f"/v1/feature-suggestion/{sid}/status").json() == {"status": "reviewed"}
