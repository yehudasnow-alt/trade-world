"""Backups of the database: everything the system has learned lives in one SQLite file, so it gets a copy every night.

`metro backup` writes a consistent snapshot (SQLite's online backup, safe while the server runs), gzips it into data/backups/,
copies it to BACKUP_DIR when set (a folder that syncs to the cloud, an external drive) and keeps the newest BACKUP_KEEP in each place.
Restore: stop the server, gunzip the file over data/metropolin.db, start again.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import get_settings
from .db import kv_set, session

PREFIX = "metropolin-"


def db_path() -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise RuntimeError("backup works with the SQLite database only")
    return Path(url.removeprefix("sqlite:///"))


def _prune(folder: Path, keep: int) -> None:
    files = sorted(folder.glob(PREFIX + "*.db.gz"))
    for f in files[:-keep] if keep > 0 else []:
        f.unlink(missing_ok=True)


def backup(to: Optional[Path] = None, keep: Optional[int] = None) -> list[Path]:
    """Snapshot the database; returns the files written (local copy first)."""
    st = get_settings()
    keep = st.backup_keep if keep is None else keep
    src = db_path()
    if not src.exists():
        raise FileNotFoundError(f"no database at {src}")
    local = st.data_dir / "backups"
    local.mkdir(parents=True, exist_ok=True)
    name = f"{PREFIX}{datetime.now().strftime('%Y%m%d-%H%M%S%f')[:-3]}.db.gz"  # milliseconds: two runs in a second never collide
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "snap.db"
        a, b = sqlite3.connect(src), sqlite3.connect(snap)
        try:
            a.backup(b)
        finally:
            b.close(); a.close()
        with open(snap, "rb") as fi, gzip.open(local / name, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo)
    out = [local / name]
    _prune(local, keep)
    extra = to or st.backup_dir
    if extra:
        extra = Path(extra).expanduser()
        extra.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local / name, extra / name)
        out.append(extra / name)
        _prune(extra, keep)
    with session() as s:  # the status panel shows when the last backup ran
        kv_set(s, "backup", {"file": name, "mb": round(out[0].stat().st_size / 1e6, 1), "copies": len(out)})
        s.commit()
    return out


def latest() -> Optional[Path]:
    files = sorted((get_settings().data_dir / "backups").glob(PREFIX + "*.db.gz"))
    return files[-1] if files else None
