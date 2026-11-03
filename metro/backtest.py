"""Backtest of the score, point in time.

Each month-end the universe is scored with only what was public then (first-reported
quarter values, prices up to that day), then forward returns are measured by score
quintile against SPY. Caveat printed with the result: the universe is today's index
members (survivorship bias) until historical membership is added.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta

from sqlalchemy import select

from .analytics import scoring
from .db import Company, Event, PriceBar, Quarter, session
from .pipeline import eps_points
from .universe import MARKET_ETF, SECTORS


def _load() -> tuple[dict, dict, dict, dict]:
    bars: dict[str, list] = {}
    with session() as s:
        comps = {c.ticker: c for c in s.scalars(select(Company).where(Company.in_universe.is_(True)))}
        for b in s.scalars(select(PriceBar).order_by(PriceBar.ticker, PriceBar.day)):
            bars.setdefault(b.ticker, []).append((b.day, b.c, b.h, b.l, b.v))
        qs: dict[str, list] = {}
        for q in s.scalars(select(Quarter).order_by(Quarter.ticker, Quarter.period_end)):
            qs.setdefault(q.ticker, []).append({"end": q.period_end, "values": q.first_values or q.values, "first_filed": q.first_filed})
        evs: dict[str, list] = {}
        for e in s.scalars(select(Event).where(Event.severity.in_(("veto", "high")))):
            evs.setdefault(e.ticker, []).append({"day": e.ts.date(), "code": e.code, "severity": e.severity, "title": e.title})
    return comps, bars, qs, evs


def run(start: date, end: date | None = None, horizons: tuple[int, ...] = (21, 63, 126)) -> dict:
    comps, bars, qs, evs = _load()
    spy = bars.get(MARKET_ETF, [])
    if not spy:
        return {"error": "אין היסטוריית מחירים. הרץ קודם: metro backfill"}
    spy_days = [b[0] for b in spy]
    end = end or spy_days[-1] - timedelta(days=max(horizons) * 1.5)
    months = []
    d = start
    while d <= end:
        i = bisect_right(spy_days, d) - 1
        if i > 260:
            months.append(spy_days[i])
        d = (d.replace(day=28) + timedelta(days=5)).replace(day=1)
    results = {h: [[] for _ in range(5)] for h in horizons}
    for asof in months:
        raws = {}
        for t, c in comps.items():
            b = bars.get(t)
            etf = bars.get(SECTORS.get(c.sector, ("", "SPY"))[1])
            if not b or not etf:
                continue
            days = [x[0] for x in b]
            k = bisect_right(days, asof)
            if k < 260:
                continue
            ed = {x[0]: x[1] for x in etf}
            sd = {x[0]: x[1] for x in spy}
            win = [x for x in b[max(0, k - 1300):k] if x[0] in ed and x[0] in sd]
            if len(win) < 200:
                continue
            q = [x for x in qs.get(t, []) if x["first_filed"] and x["first_filed"] <= asof]
            x = scoring.Inputs(t, c.sector, c.template, [w[0] for w in win], [w[1] for w in win], [w[2] for w in win],
                               [w[3] for w in win], [w[4] for w in win], [sd[w[0]] for w in win], [ed[w[0]] for w in win],
                               quarters=q, eps_points=eps_points(q), today=asof,
                               events=[e for e in evs.get(t, []) if e["day"] <= asof])
            raws[t] = scoring.raw_metrics(x)
        sc = scoring.score_universe(raws)
        ranked = sorted((t for t in sc if not sc[t]["veto"]), key=lambda t: sc[t]["total"])
        if len(ranked) < 25:
            continue
        si = bisect_right(spy_days, asof) - 1
        for h in horizons:
            if si + h >= len(spy):
                continue
            spy_ret = spy[si + h][1] / spy[si][1] - 1
            for qi in range(5):
                bucket = ranked[qi * len(ranked) // 5:(qi + 1) * len(ranked) // 5]
                rs = []
                for t in bucket:
                    b = bars[t]
                    days = [x[0] for x in b]
                    k = bisect_right(days, asof) - 1
                    if k + h < len(b):
                        rs.append(b[k + h][1] / b[k][1] - 1 - spy_ret)
                if rs:
                    results[h][qi].append(sum(rs) / len(rs))
    table = {}
    for h, qrows in results.items():
        table[h] = [round(sum(r) / len(r) * 100, 2) if r else None for r in qrows]
    return {"months": len(months), "excess_return_pct_by_quintile": table,
            "note": "Q1 = הציון הנמוך, Q5 = הגבוה. תשואה עודפת מול SPY. היקום הוא חברי המדד של היום (הטיית שורדים)."}
