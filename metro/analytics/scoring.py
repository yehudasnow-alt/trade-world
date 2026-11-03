"""Convergence score: four components of 0-25, a veto layer, and beacons.

Phase 1 (`raw_metrics`) works on one company. Phase 2 (`score_universe`) ranks every
company against its sector, so a 30% gross margin means different things for a
retailer and a software company. Weights are deliberately simple and equal until
a backtest says otherwise (`metro backtest`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Any

from . import technicals as T
from . import valuation as V

QUALITY_METRICS = {
    "standard": [("gross_margin", 1), ("op_margin", 1), ("fcf_margin", 1), ("net_debt_to_oi", -1), ("sbc_pct", -1),
                 ("shares_yoy", -1), ("rev_yoy", 1)],
    "bank": [("roe", 1), ("rev_yoy", 1), ("provision_pct", -1), ("shares_yoy", -1)],
    "insurer": [("roe", 1), ("rev_yoy", 1), ("net_margin", 1), ("shares_yoy", -1)],
    "reit": [("rev_yoy", 1), ("fcf_margin", 1), ("net_debt_to_oi", -1), ("shares_yoy", -1)],
}


@dataclass
class Inputs:
    ticker: str
    sector: str
    template: str
    days: list[date]
    closes: list[float]
    highs: list[float]
    lows: list[float]
    volumes: list[float]
    market: list[float]  # SPY closes aligned with days
    sector_etf: list[float]
    price: float | None = None  # live price, appended as "today" when newer than the last bar
    prev_close: float | None = None
    volume_today: float | None = None
    session_progress: float = 1.0
    quarters: list[dict] = field(default_factory=list)  # [{end, values, first_filed}]
    eps_points: list[tuple[date, float]] = field(default_factory=list)
    shares: float | None = None
    events: list[dict] = field(default_factory=list)  # [{day, code, severity}]
    guidance: str | None = None  # raised | maintained | lowered | withdrawn
    guidance_change_pct: float | None = None
    surprise_pct: float | None = None
    going_concern: bool = False
    today: date | None = None


def _ttm(quarters: list[dict], key: str) -> float | None:
    win = quarters[-4:]
    if len(win) < 4 or any(key not in q["values"] for q in win):
        return None
    if any(not 80 <= (b["end"] - a["end"]).days <= 100 for a, b in zip(win, win[1:])):
        return None
    return sum(q["values"][key] for q in win)


def _yoy(quarters: list[dict], key: str, i: int = -1) -> float | None:
    if len(quarters) < 5 or abs(i) > len(quarters):
        return None
    cur = quarters[i]
    prev = next((q for q in quarters if abs((cur["end"] - q["end"]).days - 364) <= 20), None)
    if not prev or key not in cur["values"] or not prev["values"].get(key):
        return None
    p = prev["values"][key]
    return (cur["values"][key] - p) / abs(p) * 100


def raw_metrics(x: Inputs) -> dict[str, Any]:
    m: dict[str, Any] = {"ticker": x.ticker, "sector": x.sector, "template": x.template}
    closes, highs, lows, mkt, sec = list(x.closes), list(x.highs), list(x.lows), list(x.market), list(x.sector_etf)
    if not closes:
        return m
    price = x.price or closes[-1]
    prev = x.prev_close or (closes[-2] if len(closes) > 1 else closes[-1])
    m["price"], m["prev_close"] = price, prev
    m["chg_pct"] = (price / prev - 1) * 100 if prev else 0.0
    std = T.ret_std_pct(closes)
    m["std_pct"] = std
    m["sigma"] = m["chg_pct"] / std if std else None
    m["atr_pct"] = T.atr_pct(highs, lows, closes)
    if x.volume_today:
        m["rvol"] = T.rvol(x.volume_today, x.volumes, x.session_progress)  # volumes = full past sessions only
    else:
        m["rvol"] = T.rvol(x.volumes[-1], x.volumes[:-1]) if x.volumes else None
    m.update(T.drawdown(closes + ([price] if x.price else []), highs, lows))
    m["ma50"], m["ma200"] = T.sma(closes, 50), T.sma(closes, 200)
    m["dist_ma200"] = (price / m["ma200"] - 1) * 100 if m["ma200"] else None
    r_now, r_then = T.rsi(closes), T.rsi(closes[:-5])
    m["rsi"], m["rsi_slope"] = r_now, (r_now - r_then) if r_now is not None and r_then is not None else None
    m["rs63"] = T.rel_strength(closes, sec, 63)
    m["rs10"] = T.rel_strength(closes, sec, 10)
    d = T.decompose(closes, mkt, sec, m["chg_pct"])
    if d:
        m["beta"], m["beta_sector"] = d.beta_market, d.beta_sector
        m["decomp"] = {"market": d.market, "sector": d.sector, "idio": d.idio}
    m["volume_profile"] = T.volume_profile(closes, x.volumes)

    q = x.quarters
    if q:
        last = q[-1]["values"]
        for k in ("gross_margin", "op_margin", "net_margin", "sbc_pct"):
            m[k] = last.get(k)
        rev_t, fcf_t, oi_t, ni_t = (_ttm(q, k) for k in ("revenue", "fcf", "operating_income", "net_income"))
        m["rev_ttm"], m["fcf_ttm"], m["oi_ttm"], m["ni_ttm"] = rev_t, fcf_t, oi_t, ni_t
        m["fcf_margin"] = fcf_t / rev_t * 100 if fcf_t is not None and rev_t else last.get("fcf_margin")
        m["rev_yoy"], m["rev_yoy_prev"] = _yoy(q, "revenue"), _yoy(q, "revenue", -2)
        m["eps_yoy"] = _yoy(q, "eps_diluted")
        m["rpo_yoy"] = _yoy(q, "rpo")
        sh_now = last.get("diluted_shares") or last.get("shares_out")
        sh_old = next((qq["values"].get("diluted_shares") or qq["values"].get("shares_out") for qq in q
                       if abs((q[-1]["end"] - qq["end"]).days - 364) <= 20), None)
        m["shares_yoy"] = (sh_now / sh_old - 1) * 100 if sh_now and sh_old else None
        nd = last.get("net_debt")
        m["net_debt"] = nd
        m["net_debt_to_oi"] = nd / oi_t if nd is not None and oi_t and oi_t > 0 else (None if nd is None else 9.0)
        eq = last.get("equity")
        m["roe"] = ni_t / eq * 100 if ni_t and eq and eq > 0 else None
        prov = _ttm(q, "credit_provision")
        m["provision_pct"] = prov / rev_t * 100 if prov is not None and rev_t else None
        ttm_vals = {k: _ttm(q, k) for k in ("revenue", "fcf", "operating_income", "eps_diluted")}
        ttm_vals["net_debt"] = nd
        shares = x.shares or last.get("shares_out") or last.get("diluted_shares")
        m["shares"] = shares
        m.update(V.multiples(price, shares, ttm_vals))
        m["eps_ttm"] = ttm_vals["eps_diluted"]
        pe_h = V.pe_history(x.days, closes, x.eps_points)
        m["pe_hist"] = pe_h[-60:]
        m["pe_pct_5y"] = V.percentile_rank(m["pe_ttm"], pe_h[-60:]) if m.get("pe_ttm") else None
        m["implied_growth"] = V.implied_growth(m.get("market_cap"), fcf_t)
        m["fair"] = V.fair_range(m["eps_ttm"], pe_h[-60:], m.get("rev_yoy"))

    today = x.today or (x.days[-1] if x.days else date.today())
    veto, flags = [], []
    for e in x.events:
        age = (today - e["day"]).days
        if e["severity"] == "veto" and age <= 365:
            veto.append(e.get("title") or e["code"])
        elif e.get("code") == "5.02-exec" and age <= 90:
            flags.append(e.get("title") or "שינוי בהנהלה")
        elif e["severity"] == "high" and age <= 120:
            flags.append(e.get("title") or e["code"])
    if x.going_concern:
        veto.append("ספק לגבי המשך פעילות (going concern)")
    if (m.get("shares_yoy") or 0) > 8:
        veto.append(f"דילול של {m['shares_yoy']:.0f}% בשנה")
    if x.guidance == "lowered" and (x.guidance_change_pct or 0) <= -5:
        veto.append("הורדת תחזית חדה")
    elif x.guidance == "lowered":
        flags.append("הורדת תחזית")
    m["veto"], m["flags"] = veto, flags
    m["guidance"], m["surprise_pct"] = x.guidance, x.surprise_pct
    return m


def _rank_within(groups: dict[str, list[tuple[str, float]]]) -> dict[str, float]:
    out = {}
    for rows in groups.values():
        vals = sorted(v for _, v in rows)
        n = len(vals)
        for t, v in rows:
            if n == 1:
                out[t] = 0.5
            else:
                lo = sum(1 for x in vals if x < v)
                eq = sum(1 for x in vals if x == v)
                out[t] = (lo + (eq - 1) / 2) / (n - 1)
    return out


def _piece(x: float, pts: list[tuple[float, float]]) -> float:
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def timing_score(m: dict) -> float:
    dd = m.get("dd_pct")
    if dd is None:
        return 12.5
    s_dd = _piece(dd, [(-60, 0.15), (-40, 0.3), (-30, 0.9), (-8, 1.0), (-3, 0.45), (0, 0.15)])
    dist = m.get("dist_ma200")
    s_tr = 0.5 if dist is None else (1.0 if dist >= 0 else 0.6 if dist >= -6 else 0.2)
    r, slope = m.get("rsi"), m.get("rsi_slope") or 0
    s_rsi = 0.5 if r is None else (1.0 if r < 40 and slope > 0 else 0.45 if r < 40 else 0.6 if r <= 65 else 0.25)
    rs10 = m.get("rs10")
    s_rs = 0.5 if rs10 is None else (1.0 if rs10 > 0 else 0.4)
    sig = m.get("sigma") or 0
    crash = 0.6 if sig < -3 else 1.0  # do not catch a falling knife on the day
    return 25 * crash * (0.35 * s_dd + 0.25 * s_tr + 0.2 * s_rsi + 0.2 * s_rs)


def score_universe(raws: dict[str, dict]) -> dict[str, dict]:
    """Ranks every company within its sector and returns components, total, veto and beacon flags."""
    out: dict[str, dict] = {}
    # quality: within-sector percentile of each available metric
    q_parts: dict[str, list[float]] = {t: [] for t in raws}
    metrics_used: set[str] = {k for spec in QUALITY_METRICS.values() for k, _ in spec}
    for key in metrics_used:
        groups: dict[str, list[tuple[str, float]]] = {}
        for t, m in raws.items():
            spec = dict(QUALITY_METRICS.get(m.get("template", "standard"), QUALITY_METRICS["standard"]))
            if key in spec and m.get(key) is not None:
                groups.setdefault(m["sector"], []).append((t, spec[key] * m[key]))
        for t, r in _rank_within(groups).items():
            q_parts[t].append(r)
    # direction inputs ranked across the whole universe
    acc = _rank_within({"all": [(t, m["rev_yoy"] - m["rev_yoy_prev"]) for t, m in raws.items()
                                if m.get("rev_yoy") is not None and m.get("rev_yoy_prev") is not None]})
    gro = _rank_within({"all": [(t, m["rev_yoy"]) for t, m in raws.items() if m.get("rev_yoy") is not None]})
    # valuation vs the sub-industry street: lower P/E (or P/S when unprofitable) ranks better
    street: dict[str, list[tuple[str, float]]] = {}
    for t, m in raws.items():
        v = m.get("pe_ttm") or (m.get("ps") and m["ps"] * 15)
        if v:
            street.setdefault(m["sector"], []).append((t, -v))
    st_rank = _rank_within(street)
    gmap = {"raised": 1.0, "maintained": 0.55, "initiated": 0.6, "lowered": 0.0, "withdrawn": 0.1}
    for t, m in raws.items():
        qp = q_parts[t]
        quality = 25 * (sum(qp) / len(qp)) if len(qp) >= 3 else 12.5
        g = gmap.get(m.get("guidance") or "", 0.5)
        sp = m.get("surprise_pct")
        s_sp = 0.5 if sp is None else (1.0 if sp > 2 else 0.6 if sp >= 0 else 0.15)
        direction = 25 * (0.35 * acc.get(t, 0.5) + 0.25 * gro.get(t, 0.5) + 0.25 * g + 0.15 * s_sp)
        own = m.get("pe_pct_5y")
        s_own = 0.5 if own is None else 1 - own / 100
        ig, ry = m.get("implied_growth"), m.get("rev_yoy")
        s_ig = 0.5 if ig is None or ry is None else (1.0 if ig < ry - 3 else 0.55 if ig <= ry + 3 else 0.15)
        valuation = 25 * (0.5 * s_own + 0.3 * st_rank.get(t, 0.5) + 0.2 * s_ig)
        timing = timing_score(m)
        total = quality + direction + valuation + timing
        penalty = 4 * len(m.get("flags") or [])
        total = 0.0 if m.get("veto") else max(0.0, total - penalty)
        out[t] = {"quality": round(quality, 1), "direction": round(direction, 1), "valuation": round(valuation, 1),
                  "timing": round(timing, 1), "total": round(total, 1), "veto": m.get("veto") or [],
                  "flags": m.get("flags") or []}
    # beacons: strict rule, then keep only the strongest few so the signal stays a signal
    cands = [t for t, s in out.items() if not s["veto"] and s["total"] >= 66 and s["quality"] >= 15
             and s["valuation"] >= 13.5 and (raws[t].get("dd_pct") or 0) <= -7 and (raws[t].get("sigma") or 0) > -3]
    cands.sort(key=lambda t: out[t]["total"], reverse=True)
    for t in out:
        out[t]["beacon"] = t in cands[:8]
    return out


def street_medians(raws: dict[str, dict], subs: dict[str, str]) -> dict[str, float]:
    by: dict[str, list[float]] = {}
    for t, m in raws.items():
        if m.get("pe_ttm"):
            by.setdefault(subs.get(t, ""), []).append(m["pe_ttm"])
    return {k: median(v) for k, v in by.items() if v}

