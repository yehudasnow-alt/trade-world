"""The cloud setup: backups go to a private GitHub repository and come back on an empty disk; a public address needs a code."""
import json

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from metro import backup, cloud, config
from metro.db import Company, kv_get, kv_set, session


class FakeGitHub:
    """Just enough of the GitHub releases API: an empty repository, then a release with assets."""

    def __init__(self):
        self.release = None
        self.readme = False
        self.assets = {}  # name -> bytes
        self.next_id = 1

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer tok"
        p, m = req.url.path, req.method
        if p == "/repos/me/data/releases/tags/backups" and m == "GET":
            return httpx.Response(200, json=self.release) if self.release else httpx.Response(404)
        if p == "/repos/me/data/releases" and m == "POST":
            if not self.readme:
                return httpx.Response(422, json={"message": "Repository is empty"})
            self.release = {"id": 7, "upload_url": "https://uploads.github.com/repos/me/data/releases/7/assets{?name,label}"}
            return httpx.Response(201, json=self.release)
        if p == "/repos/me/data/contents/README.md" and m == "PUT":
            self.readme = True
            return httpx.Response(201, json={})
        if p == "/repos/me/data/releases/7/assets" and req.url.host == "uploads.github.com" and m == "POST":
            name = req.url.params["name"]
            self.assets[name] = (self.next_id, req.content)
            self.next_id += 1
            return httpx.Response(201, json={"browser_download_url": f"https://github.com/me/data/releases/download/backups/{name}"})
        if p == "/repos/me/data/releases/7/assets" and m == "GET":
            return httpx.Response(200, json=[{"id": i, "name": n} for n, (i, _) in self.assets.items()])
        if p.startswith("/repos/me/data/releases/assets/"):
            aid = int(p.rsplit("/", 1)[1])
            name = next(n for n, (i, _) in self.assets.items() if i == aid)
            if m == "DELETE":
                del self.assets[name]
                return httpx.Response(204)
            return httpx.Response(200, content=self.assets[name][1])
        return httpx.Response(500, json={"unexpected": f"{m} {req.url}"})


def _github(monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setenv("BACKUP_GITHUB_REPO", "me/data")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    config.get_settings.cache_clear()
    real = cloud.GitHubBackup
    monkeypatch.setattr(cloud, "GitHubBackup", lambda: real(http=httpx.Client(transport=httpx.MockTransport(gh), headers={"Authorization": "Bearer tok"})))
    return gh


def test_backups_go_to_github_and_an_empty_disk_restores(isolated_env, monkeypatch):
    gh = _github(monkeypatch)
    with session() as s:
        s.add(Company(ticker="ZZZT", cik=1, name="Kept Co", sector="Industrials", sub_industry="Machinery"))
    for _ in range(3):
        backup.backup(keep=2)
    assert gh.readme and len(gh.assets) == 2  # the empty repository got a README, then a release; the oldest copy was pruned
    with session() as s:
        assert kv_get(s, "backup")["github"] == "me/data"

    # the disk is lost: the next start restores the latest copy before anything else
    from metro import db
    db.reset_engine()
    path = backup.db_path()
    path.unlink()
    assert cloud.restore_from_github(path)
    db.reset_engine()
    with session() as s:
        assert s.scalars(select(Company.name).where(Company.ticker == "ZZZT")).first() == "Kept Co"


def test_a_public_address_needs_an_access_code(isolated_env, monkeypatch):
    from metro.api import app
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    config.get_settings.cache_clear()
    c = TestClient(app)
    assert c.get("/healthz").json() == {"ok": True}
    r = c.get("/api/city")
    assert r.status_code == 503 and "ACCESS_TOKEN" in r.json()["detail"]
    monkeypatch.setenv("ACCESS_TOKEN", "s3cret-code")
    config.get_settings.cache_clear()
    assert c.get("/api/city").status_code == 401
    r = c.get("/?token=s3cret-code", follow_redirects=False, headers={"x-forwarded-proto": "https"})
    assert r.status_code in (302, 307) and "secure" in r.headers["set-cookie"].lower()
    assert c.get("/api/city", headers={"x-metro-token": "s3cret-code"}).status_code == 200
    assert c.get("/api/status", headers={"x-metro-token": "s3cret-code"}).json()["cloud"] is True


def test_a_fresh_install_fills_itself(isolated_env, monkeypatch):
    from metro import pipeline, universe
    from metro.jobs import Runner
    universe.sync_universe()
    monkeypatch.setenv("SEC_USER_AGENT", "")
    config.get_settings.cache_clear()
    r = Runner()
    assert not r.needs_first_fill()  # no keys: nothing to fill from
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    config.get_settings.cache_clear()
    assert r.needs_first_fill() == {"alpaca"}
    calls = []

    def fake(*a, only=None, **k):
        calls.append(only)
        with session() as s:
            bs = kv_get(s, "bootstrap") or {}
            kv_set(s, "bootstrap", {**bs, "done": sorted(set(bs.get("done", [])) | only)})
        return {"bars": 3}
    monkeypatch.setattr(pipeline, "backfill_all", fake)
    assert "bars=3" in r.bootstrap_tick()
    assert not r.needs_first_fill()
    # a key added a week later fills only its own part
    monkeypatch.setenv("FINNHUB_API_KEY", "f")
    config.get_settings.cache_clear()
    assert r.needs_first_fill() == {"finnhub"}
    r.bootstrap_tick()
    assert calls == [{"alpaca"}, {"finnhub"}]
    with session() as s:
        assert kv_get(s, "bootstrap")["state"] == "idle"


def test_prune_keeps_the_disk_small(isolated_env):
    import os
    import time
    old = isolated_env / "cache" / "sec" / "ab" / "old"
    new = isolated_env / "cache" / "sec" / "cd" / "new"
    for p in (old, new):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x" * 1000)
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    out = cloud.prune_disk()
    assert not old.exists() and not old.parent.exists() and new.exists() and out["freed_mb"] >= 0
    with session() as s:
        kv_set(s, "x", json.dumps(out))
