"""Builds the frontend from web/src/index.src.html.

  web/index.html             served by `metro run`: three.js from /vendor, data from the API
  dist/metropolin-demo.html  page body for a hosted artifact: three.js from the CDN, demo data embedded
  site/index.html            the same demo as a full static page, for Vercel or any static host

Usage: `metro web` (live page only) or `metro web --demo` (also both demo files; needs `metro demo` first).
`metro run` rebuilds web/index.html by itself when the source is newer.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .config import ROOT

SRC = ROOT / "web" / "src" / "index.src.html"
CDN_MAP = '{ "imports": { "three": "https://cdn.jsdelivr.net/npm/three@0.169.0/build/three.module.js", "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.169.0/examples/jsm/" } }'
LOCAL_MAP = '{ "imports": { "three": "/vendor/three.module.js", "three/addons/controls/": "/vendor/addons/" } }'
HEAD = ('<!doctype html>\n<html lang="he" dir="rtl">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n<meta name="color-scheme" content="light dark">\n'
        '<meta name="description" content="עיר תלת-ממדית של מניות ה-S&amp;P 500: סקטורים כרובעים, חברות כבניינים, דוחות, ציונים ותזות.">\n'
        '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 28 28%27%3E'
        '%3Cpath d=%27M3 24V13l6-2.4V24z%27 fill=%27%233554F0%27 opacity=%27.45%27/%3E%3Cpath d=%27M10 24V6.5l7-3.5v21z%27 fill=%27%233554F0%27/%3E'
        '%3Cpath d=%27M18 24V10.5l7 2.2V24z%27 fill=%27%233554F0%27 opacity=%27.7%27/%3E%3C/svg%3E">\n')

METRIC_KEYS = ("high52", "low52", "dd_pct", "dist_ma200", "rsi", "decomp", "fair", "pe_ttm", "pe_pct_5y", "implied_growth",
               "rev_yoy", "atr_pct", "market_cap", "net_debt", "net_debt_to_oi", "ps", "ev_sales", "p_fcf", "fcf_margin",
               "shares_yoy", "roe")
QUARTER_KEYS = ("revenue", "gross_margin", "op_margin", "fcf", "sbc_pct", "eps_diluted")
N_CLOSES = 175  # 126 shown + 49 for the 50-day average


def _src() -> str:
    s = SRC.read_text(encoding="utf-8")
    assert "/*__IMPORTMAP__*/" in s and "/*__DEMO__*/null" in s, "placeholders missing in index.src.html"
    return s


def _round(x, n=4):
    if isinstance(x, float):
        return round(x, n)
    if isinstance(x, dict):
        return {k: _round(v, n) for k, v in x.items()}
    if isinstance(x, list):
        return [_round(v, n) for v in x]
    return x


def stale() -> bool:
    out = ROOT / "web" / "index.html"
    return not out.exists() or out.stat().st_mtime < SRC.stat().st_mtime


def build_live(out: Path | None = None) -> Path:
    s = _src().replace("/*__IMPORTMAP__*/" + CDN_MAP, LOCAL_MAP)
    assert LOCAL_MAP in s, "import map in the source no longer matches CDN_MAP"
    s = s.replace('<div id="app"', '</head>\n<body>\n<div id="app"', 1)
    out = out or ROOT / "web" / "index.html"
    out.write_text(HEAD + s + "\n</body>\n</html>\n", encoding="utf-8")
    return out


def demo_payload() -> dict:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from .api import app
    from .db import PriceBar, session
    from .universe import MARKET_ETF

    with TestClient(app) as cl:
        def get(path):
            r = cl.get(path)
            r.raise_for_status()
            return r.json()
        city = get("/api/city")
        if city["mode"] != "demo":
            raise SystemExit("The database is not in demo mode. Run `metro demo` (or point DATABASE_URL at a demo database) first.")
        with session() as s:
            days = [b.day.isoformat() for b in s.scalars(select(PriceBar).where(PriceBar.ticker == MARKET_ETF)
                                                          .order_by(PriceBar.day.desc()).limit(N_CLOSES))][::-1]
            closes: dict[str, list[float]] = {}
            for b in s.scalars(select(PriceBar).where(PriceBar.day >= days[0]).order_by(PriceBar.day)):
                closes.setdefault(b.ticker, []).append(round(b.c, 2))
        co = {}
        for row in city["companies"]:
            t = row["t"]
            js = get(f"/api/company/{t}")
            m = js.get("metrics") or {}
            entry = {
                "c": closes.get(t, [])[-N_CLOSES:],
                "m": _round({k: m[k] for k in METRIC_KEYS if m.get(k) is not None}, 3),
                "vp": _round(js.get("volume_profile") or [], 3),
                "e": js.get("earnings") or [],
                "q": [{"fiscal": q["fiscal"], "calendar": q["calendar"], "restated": q.get("restated") or [],
                       "values": _round({k: q["values"][k] for k in QUARTER_KEYS if k in q["values"]}, 4)} for q in js.get("quarters") or []],
                "card": js.get("card"), "pr": js.get("promises") or [], "ev": js.get("events") or [], "th": js.get("thesis"),
                "w": js.get("watch"), "h": js.get("holding"), "j": js.get("journal") or [], "tpl": js["company"].get("template"),
            }
            co[t] = {k: v for k, v in entry.items() if v not in (None, [], {}, "standard")}
        return {"city": city, "days": days, "co": co, "status": get("/api/status"), "brief": get("/api/brief"), "timeline": get("/api/timeline"),
                "portfolio": get("/api/portfolio"), "thesis_options": get("/api/thesis-options"), "journal": [],
                "built": time.strftime("%Y-%m-%d")}


def _demo_source(payload: dict | None = None) -> str:
    data = json.dumps(payload or demo_payload(), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return _src().replace("/*__IMPORTMAP__*/", "", 1).replace("/*__DEMO__*/null", data, 1)


def build_demo(out: Path | None = None, payload: dict | None = None) -> Path:
    """Page body only: the artifact host wraps it in its own document."""
    out = out or ROOT / "dist" / "metropolin-demo.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(_demo_source(payload), encoding="utf-8")
    return out


def build_site(out: Path | None = None, payload: dict | None = None) -> Path:
    """A complete static page (Vercel, GitHub Pages, any static host)."""
    s = _demo_source(payload).replace('<div id="app"', '</head>\n<body>\n<div id="app"', 1)
    out = out or ROOT / "site" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(HEAD + s + "\n</body>\n</html>\n", encoding="utf-8")
    return out


def main(demo: bool = False) -> list[Path]:
    out = [build_live()]
    if demo:
        payload = demo_payload()
        out += [build_demo(payload=payload), build_site(payload=payload)]
    return out


if __name__ == "__main__":
    for p in main("--demo" in sys.argv):
        print(f"built {p.relative_to(ROOT)} ({p.stat().st_size // 1024} KB)")
