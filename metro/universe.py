"""The universe: S&P 500 constituents with GICS sector and sub-industry.

The bundled data/sp500.csv comes from the open dataset at
github.com/datasets/s-and-p-500-companies. The running system downloads the current
list every weekday morning (`metro init --refresh` does it by hand) into
data/cache/sp500.csv, outside Git; companies that join get their history pulled,
companies that leave drop off the map. Dual share classes (GOOGL/GOOG, FOXA/FOX,
NWSA/NWS) become one company with alternate tickers.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import select

from .config import get_settings
from .db import Company, session

SOURCE_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

SECTORS = {
    "Information Technology": ("טכנולוגיה", "XLK"),
    "Communication Services": ("תקשורת", "XLC"),
    "Consumer Discretionary": ("צריכה מחזורית", "XLY"),
    "Financials": ("פיננסים", "XLF"),
    "Health Care": ("בריאות", "XLV"),
    "Industrials": ("תעשייה", "XLI"),
    "Consumer Staples": ("צריכה בסיסית", "XLP"),
    "Energy": ("אנרגיה", "XLE"),
    "Utilities": ("תשתיות", "XLU"),
    "Real Estate": ("נדל״ן", "XLRE"),
    "Materials": ("חומרי גלם", "XLB"),
}
SECTOR_ETFS = [v[1] for v in SECTORS.values()]
MARKET_ETF = "SPY"

BANK_SUBS = {"Diversified Banks", "Regional Banks"}
INSURER_SUBS = {"Property & Casualty Insurance", "Life & Health Insurance", "Multi-line Insurance", "Reinsurance", "Insurance Brokers"}
FOREIGN_FILERS: set[str] = set()  # S&P 500 members file 10-K/10-Q; Nasdaq-100 extras may file 20-F/6-K


@dataclass
class Member:
    ticker: str
    name: str
    sector: str
    sub_industry: str
    cik: int
    alt: list[str] = field(default_factory=list)

    @property
    def template(self) -> str:
        if self.sector == "Real Estate" and "REIT" in self.sub_industry:
            return "reit"
        if self.sub_industry in BANK_SUBS:
            return "bank"
        if self.sub_industry in INSURER_SUBS:
            return "insurer"
        return "standard"


def parse_csv(text: str) -> list[Member]:
    merged: dict[str, Member] = {}
    for r in csv.DictReader(io.StringIO(text)):
        name = r["Security"].strip()
        base = re.sub(r"\s*\(Class [A-Z]\)\s*$", "", name)
        cik = int(r["CIK"]) if r.get("CIK", "").strip().isdigit() else 0
        key = f"{cik}:{base}"
        sym = r["Symbol"].strip()
        if key in merged:
            merged[key].alt.append(sym)
        else:
            merged[key] = Member(sym, base, r["GICS Sector"].strip(), r["GICS Sub-Industry"].strip(), cik)
    return list(merged.values())


def bundled_path() -> Path:
    return get_settings().data_dir / "sp500.csv"


def cached_path() -> Path:
    return get_settings().data_dir / "cache" / "sp500.csv"


MIN_MEMBERS = 480  # a broken download must never empty the map


def load_members(refresh: bool = False) -> list[Member]:
    if refresh:
        r = httpx.get(SOURCE_URL, timeout=30, follow_redirects=True)
        r.raise_for_status()
        members = parse_csv(r.text)
        if len(members) < MIN_MEMBERS or any(m.sector not in SECTORS for m in members):
            raise ValueError(f"the downloaded S&P 500 list looks wrong ({len(members)} companies); keeping the current one")
        cached_path().parent.mkdir(parents=True, exist_ok=True)
        cached_path().write_text(r.text, encoding="utf-8")
        return members
    path = cached_path() if cached_path().exists() else bundled_path()
    return parse_csv(path.read_text(encoding="utf-8"))


def load_themes() -> dict[str, list[str]]:
    p = get_settings().data_dir / "themes.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for theme, tickers in raw.get("themes", {}).items():
        for t in tickers:
            out.setdefault(t, []).append(theme)
    return out


def sync_universe(refresh: bool = False) -> tuple[int, int]:
    """Upsert members; companies that left the index stay in the DB with in_universe=False."""
    members = load_members(refresh)
    themes = load_themes()
    added = 0
    with session() as s:
        existing = {c.ticker: c for c in s.scalars(select(Company))}
        seen = set()
        for m in members:
            seen.add(m.ticker)
            c = existing.get(m.ticker)
            if c is None:
                c = Company(ticker=m.ticker)
                s.add(c)
                added += 1
            c.name, c.sector, c.sub_industry, c.cik = m.name, m.sector, m.sub_industry, m.cik
            c.alt_tickers, c.template, c.in_universe = m.alt, m.template, True
            c.themes = themes.get(m.ticker, [])
            c.filer_type = "foreign" if m.ticker in FOREIGN_FILERS else "domestic"
        for t, c in existing.items():
            if t not in seen:
                c.in_universe = False
    return len(members), added


def refresh_universe() -> tuple[list[str], list[str]]:
    """Download the current index list and sync it. Returns (joined, left)."""
    with session() as s:
        before = set(s.scalars(select(Company.ticker).where(Company.in_universe.is_(True))))
    sync_universe(refresh=True)
    with session() as s:
        after = set(s.scalars(select(Company.ticker).where(Company.in_universe.is_(True))))
    return sorted(after - before), sorted(before - after)


# Symbol conventions differ by vendor.
def sec_symbol(t: str) -> str:
    return t.replace(".", "-")


def alpaca_symbol(t: str) -> str:
    return t  # Alpaca uses BRK.B


def ibkr_symbol(t: str) -> str:
    return t.replace(".", " ")  # IBKR uses "BRK B"


def from_ibkr_symbol(sym: str) -> str:
    return sym.strip().replace(" ", ".")
