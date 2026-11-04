"""Nightly backups: a consistent, restorable snapshot, a second copy when BACKUP_DIR is set, old ones pruned."""
import gzip
import sqlite3

from metro import backup, config
from metro.db import Company, kv_get, session


def test_backup_is_restorable_and_pruned(isolated_env, tmp_path, monkeypatch):
    with session() as s:
        s.add(Company(ticker="ZZZT", cik=1, name="Test Co", sector="Industrials", sub_industry="Machinery"))
    extra = tmp_path / "cloud"
    monkeypatch.setenv("BACKUP_DIR", str(extra))
    config.get_settings.cache_clear()

    files = backup.backup(keep=2)
    assert len(files) == 2 and files[0].parent == isolated_env / "backups" and files[1].parent == extra
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(files[0].read_bytes()))
    with sqlite3.connect(restored) as c:
        assert c.execute("select name from companies where ticker = 'ZZZT'").fetchone() == ("Test Co",)
    with session() as s:
        assert kv_get(s, "backup")["copies"] == 2

    # the newest `keep` survive in both places
    for i in range(3):
        for folder in (isolated_env / "backups", extra):
            (folder / f"metropolin-2000010{i}-000000.db.gz").write_bytes(b"old")
    backup.backup(keep=2)
    for folder in (isolated_env / "backups", extra):
        left = sorted(p.name for p in folder.glob("metropolin-*.db.gz"))
        assert len(left) == 2 and not any(n.startswith("metropolin-2000") for n in left)
    assert backup.latest().name == left[-1]
