"""The index list refreshes itself: joiners appear, leavers drop off the map, a bad download changes nothing."""
import pytest
from sqlalchemy import select

from metro import universe
from metro.api import city
from metro.db import Company, Event, session
from metro.jobs import Runner


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _edited(isolated_env, drop: str, add_line: str) -> str:
    lines = (isolated_env / "sp500.csv").read_text(encoding="utf-8").splitlines()
    head = lines[0].split(",")
    sym = head.index("Symbol")
    return "\n".join([lines[0]] + [ln for ln in lines[1:] if ln.split(",")[sym] != drop] + [add_line]) + "\n"


def test_refresh_reports_joiners_and_leavers(isolated_env, monkeypatch):
    universe.sync_universe()
    head = (isolated_env / "sp500.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
    row = {"Symbol": "ZZNW", "Security": "Newco Industries", "GICS Sector": "Industrials", "GICS Sub-Industry": "Industrial Machinery & Supplies & Components",
           "Headquarters Location": "Austin, Texas", "Date added": "2026-11-06", "CIK": "0009999999", "Founded": "1999"}
    text = _edited(isolated_env, "MMM", ",".join(row.get(h, "") for h in head))
    monkeypatch.setattr(universe.httpx, "get", lambda *a, **k: _Resp(text))

    joined, left = universe.refresh_universe()
    assert joined == ["ZZNW"] and left == ["MMM"]
    assert universe.cached_path().exists() and (isolated_env / "sp500.csv").read_text(encoding="utf-8") != text  # the repo copy is untouched

    # the morning job records both changes on the timeline, and the map follows
    r = Runner()
    monkeypatch.setattr(universe, "refresh_universe", lambda: (["ZZNW"], ["MMM"]))
    out = r.universe_tick()
    assert "joined=ZZNW" in out and "left=MMM" in out
    with session() as s:
        kinds = {(e.ticker, e.code) for e in s.scalars(select(Event).where(Event.kind == "index"))}
    assert kinds == {("ZZNW", "joined"), ("MMM", "left")}
    tickers = {c["t"] for c in city()["companies"]}
    assert "ZZNW" in tickers and "MMM" not in tickers


def test_a_broken_download_changes_nothing(isolated_env, monkeypatch):
    universe.sync_universe()
    lines = (isolated_env / "sp500.csv").read_text(encoding="utf-8").splitlines()
    monkeypatch.setattr(universe.httpx, "get", lambda *a, **k: _Resp("\n".join(lines[:40]) + "\n"))
    with pytest.raises(ValueError):
        universe.refresh_universe()
    with session() as s:
        assert len(list(s.scalars(select(Company).where(Company.in_universe.is_(True))))) > 480
    assert not universe.cached_path().exists()
