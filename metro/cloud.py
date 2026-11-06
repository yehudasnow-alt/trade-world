"""Running in the cloud: the code deploys from GitHub, the state is kept safe on GitHub too.

Nothing has to live on your computer:
- the hosting service (Railway, Render, Fly...) builds the Dockerfile from GitHub and redeploys on every push;
- the keys sit in the service's environment variables;
- the database lives on the service's disk, and every night a compressed copy goes to the releases of a
  private GitHub repository (BACKUP_GITHUB_REPO, with a fine-grained GITHUB_TOKEN that can write to it);
- a new machine with an empty disk restores the latest copy from there before it starts (`metro boot`).
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

import httpx

from .config import get_settings

log = logging.getLogger(__name__)
API = "https://api.github.com"
TAG = "backups"
CLOUD_MARKERS = ("RAILWAY_ENVIRONMENT", "RENDER", "FLY_APP_NAME", "K_SERVICE", "DYNO")


def on_cloud() -> bool:
    """True on a hosting service (or PUBLIC=true): the address is on the internet, so an access code is required."""
    return get_settings().public or any(os.environ.get(k) for k in CLOUD_MARKERS)


def github_ready() -> bool:
    st = get_settings()
    return bool(st.backup_github_repo and st.github_token and "/" in st.backup_github_repo)


class GitHubBackup:
    def __init__(self, repo: Optional[str] = None, token: Optional[str] = None, http: Optional[httpx.Client] = None):
        st = get_settings()
        self.repo = repo or st.backup_github_repo
        self.http = http or httpx.Client(timeout=httpx.Timeout(60, read=600), follow_redirects=True, headers={
            "Authorization": f"Bearer {token or st.github_token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "metropolin-backup"})

    def _release(self, create: bool = True) -> Optional[dict]:
        r = self.http.get(f"{API}/repos/{self.repo}/releases/tags/{TAG}")
        if r.status_code == 200:
            return r.json()
        if r.status_code != 404 or not create:
            r.raise_for_status()
            return None
        body = {"tag_name": TAG, "name": "גיבויי מסד הנתונים", "body": "גיבוי לילי של מטרופולין. כל קובץ הוא עותק דחוס של המסד; `metro restore --github` משחזר את האחרון."}
        r = self.http.post(f"{API}/repos/{self.repo}/releases", json=body)
        if r.status_code == 422:  # an empty repository has nothing to tag: give it a README first
            self.http.put(f"{API}/repos/{self.repo}/contents/README.md", json={
                "message": "Backups for Metropolin", "content": "IyBNZXRyb3BvbGluIGJhY2t1cHMKClNlZSB0aGUgcmVsZWFzZXMuCg=="}).raise_for_status()
            r = self.http.post(f"{API}/repos/{self.repo}/releases", json=body)
        r.raise_for_status()
        return r.json()

    def assets(self) -> list[dict]:
        rel = self._release(create=False)
        if not rel:
            return []
        out, page = [], 1
        while True:
            r = self.http.get(f"{API}/repos/{self.repo}/releases/{rel['id']}/assets", params={"per_page": 100, "page": page})
            r.raise_for_status()
            out += r.json()
            if len(r.json()) < 100:
                return sorted(out, key=lambda a: a["name"])
            page += 1

    def upload(self, path: Path, keep: int) -> str:
        rel = self._release()
        url = rel["upload_url"].split("{")[0]
        with open(path, "rb") as f:
            r = self.http.post(url, params={"name": path.name}, content=f.read(), headers={"Content-Type": "application/gzip"})
        r.raise_for_status()
        for a in self.assets()[:-keep] if keep > 0 else []:
            self.http.delete(f"{API}/repos/{self.repo}/releases/assets/{a['id']}")
        return r.json().get("browser_download_url", path.name)

    def download_latest(self, dest: Path) -> Optional[str]:
        files = [a for a in self.assets() if a["name"].endswith(".db.gz")]
        if not files:
            return None
        a = files[-1]
        with self.http.stream("GET", f"{API}/repos/{self.repo}/releases/assets/{a['id']}", headers={"Accept": "application/octet-stream"}) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return a["name"]


def restore(gz: Path, db: Path) -> None:
    """Unpack a backup over the database file (the server must not be running)."""
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_suffix(".restoring")
    with gzip.open(gz, "rb") as fi, open(tmp, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    for side in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        side.unlink(missing_ok=True)
    tmp.replace(db)


def restore_from_github(db: Path) -> Optional[str]:
    if not github_ready():
        return None
    tmp = db.parent / "github-restore.db.gz"
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        name = GitHubBackup().download_latest(tmp)
        if name:
            restore(tmp, db)
        return name
    finally:
        tmp.unlink(missing_ok=True)


def prune_disk(cache_days: int = 30, docs_days: int = 450) -> dict:
    """Keep the disk small: raw SEC downloads can be fetched again; parsed filings are kept long enough for quarter-on-quarter diffs."""
    import time
    st, now, freed = get_settings(), time.time(), 0
    for root, days in ((st.data_dir / "cache" / "sec", cache_days), (st.data_dir / "docs", docs_days)):
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and now - p.stat().st_mtime > days * 86400:
                freed += p.stat().st_size
                p.unlink(missing_ok=True)
        for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
    used = sum(p.stat().st_size for p in st.data_dir.rglob("*") if p.is_file())
    return {"freed_mb": round(freed / 1e6, 1), "data_mb": round(used / 1e6, 1)}
