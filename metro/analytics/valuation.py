"""Valuation: multiples, own-history percentile, reverse DCF and a fair-value range (not a single number)."""
from __future__ import annotations

import math

from bisect import bisect_right
from datetime import date


def percentile_rank(value: float, history: list[float]) -> float | None:
    """Share of history at or below value, 0..100."""
    h = sorted(x for x in history if x is not None)
    if len(h) < 8 or value is None:
        return None
    return bisect_right(h, value) / len(h) * 100


def pe_history(price_days: list[date], closes: list[float], eps_points: list[tuple[date, float]], step: int = 21) -> list[float]:
    """P/E sampled monthly using the trailing EPS that was public on each date (point in time).

    eps_points: (date the TTM EPS became known, TTM EPS), sorted by date.
    """
    out, j, eps = [], 0, None
    for i in range(0, len(price_days), step):
        d = price_days[i]
        while j < len(eps_points) and eps_points[j][0] <= d:
            eps = eps_points[j][1]
            j += 1
        if eps and eps > 0:
            out.append(closes[i] / eps)
    return out


def implied_growth(market_cap: float, fcf_ttm: float, discount: float = 0.09, terminal: float = 0.03, years: int = 10) -> float | None:
    """Annual FCF growth (10 years) that today's market cap prices in. None when FCF is not positive."""
    if not market_cap or not fcf_ttm or fcf_ttm <= 0:
        return None

    def value(g: float) -> float:
        pv, f = 0.0, fcf_ttm
        for t in range(1, years + 1):
            f *= 1 + g
            pv += f / (1 + discount) ** t
        tv = f * (1 + terminal) / (discount - terminal)
        return pv + tv / (1 + discount) ** years

    lo, hi = -0.3, 0.8
    if value(hi) < market_cap:
        return hi * 100
    if value(lo) > market_cap:
        return lo * 100
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if value(mid) < market_cap else (lo, mid)
    return (lo + hi) / 2 * 100


def fair_range(eps_ttm: float | None, pe_hist: list[float], growth_pct: float | None) -> dict | None:
    """Bear / base / bull price from the company's own P/E band applied to next-year EPS."""
    h = sorted(pe_hist)
    if not eps_ttm or eps_ttm <= 0 or len(h) < 12:
        return None
    g = max(-0.2, min(0.4, (growth_pct or 0) / 100))
    fwd = eps_ttm * (1 + g)
    q = lambda p: h[min(len(h) - 1, int(p * len(h)))]  # noqa: E731
    return {"bear": fwd * q(0.2), "base": fwd * q(0.5), "bull": fwd * q(0.8), "eps_fwd": fwd,
            "pe": {"p20": q(0.2), "p50": q(0.5), "p80": q(0.8)}, "growth_used_pct": g * 100}


def multiples(price: float, shares: float | None, q_ttm: dict) -> dict:
    out: dict[str, float | None] = {}
    cap = price * shares if shares else None
    out["market_cap"] = cap
    eps = q_ttm.get("eps_diluted")
    out["pe_ttm"] = price / eps if eps and eps > 0 else None
    rev, fcf, nd = q_ttm.get("revenue"), q_ttm.get("fcf"), q_ttm.get("net_debt")
    out["ps"] = cap / rev if cap and rev else None
    out["p_fcf"] = cap / fcf if cap and fcf and fcf > 0 else None
    ev = cap + nd if cap is not None and nd is not None else cap
    out["ev"] = ev
    out["ev_sales"] = ev / rev if ev and rev else None
    oi = q_ttm.get("operating_income")
    out["ev_ebit"] = ev / oi if ev and oi and oi > 0 else None
    return out


LONG_RUN_GROWTH = 4.0  # % a year: where the growth of a mature company tends to settle (about nominal GDP)
FADE_YEARS = 5.0
HORIZONS = (0.25, 0.5, 1.0, 2.0, 5.0)


def expected_growth(rev_yoy: float | None, rev_cagr5: float | None, eps_yoy: float | None, guidance: str | None) -> float | None:
    """A starting growth rate from the company's own numbers: last year's sales, five years of sales, and profit,
    nudged by what management said about the outlook. Extreme years are capped: they rarely repeat."""
    parts = [(v, w) for v, w in ((rev_yoy, 0.45), (rev_cagr5, 0.35), (eps_yoy, 0.20)) if v is not None]
    if not parts:
        return None
    g = sum(max(-30.0, min(60.0, v)) * w for v, w in parts) / sum(w for _, w in parts)
    g += {"raised": 2.0, "lowered": -3.0, "withdrawn": -3.0}.get(guidance or "", 0.0)
    return max(-20.0, min(40.0, g))


def eps_after(eps: float, g0: float, years: float) -> float:
    """Earnings per share after `years`, with growth fading in a straight line from g0 to the long-run rate over five years."""
    steps = max(1, int(round(years * 12)))
    dt, out = years / steps, eps
    for k in range(steps):
        t = (k + 0.5) * dt
        g = g0 + (LONG_RUN_GROWTH - g0) * min(t, FADE_YEARS) / FADE_YEARS
        out *= (1 + g / 100) ** dt
    return out


def outlook(eps_ttm: float | None, pe_hist: list[float], g0: float | None) -> dict | None:
    """Value per share now and later, by the analysis: earnings a year ahead of each date, times the P/E the market
    usually paid for this company (its five-year median). The market prices next year's profit, hence the year ahead."""
    h = sorted(x for x in pe_hist if x and x > 0)
    if not eps_ttm or eps_ttm <= 0 or len(h) < 12 or g0 is None:
        return None
    pe = h[len(h) // 2]
    return {"g0": g0, "pe": pe, "now": eps_after(eps_ttm, g0, 1.0) * pe,
            "path": {str(y): eps_after(eps_ttm, g0, y + 1.0) * pe for y in HORIZONS}}


GROWTH_CAP_VALUE = 25.0  # % a year: for valuing, faster growth counts as 25 (it is the part most likely to disappoint)
REQUIRED_RETURN, TERMINAL = 9.0, 3.0


def fair_multiple(own_hist: list[float], sector_pe: float | None) -> float | None:
    """Half the company's own five-year median P/E, half its sector's median today: a company's own history alone
    carries the excitement of the years it was priced in, the sector alone ignores what makes it different."""
    h = sorted(x for x in own_hist[-60:] if x and 0 < x < 300)
    own = h[len(h) // 2] if len(h) >= 12 else None
    anchors = [x for x in (own, sector_pe) if x and x > 0]
    if not anchors:
        return None
    return max(8.0, min(45.0, sum(anchors) / len(anchors)))


def dcf_per_share(fcf: float, g1: float, shares: float, net_debt: float = 0.0, r: float = REQUIRED_RETURN, t: float = TERMINAL) -> float | None:
    if not fcf or fcf <= 0 or not shares or r <= t:
        return None
    g2 = max(t, g1 / 2)
    v, cf = 0.0, fcf
    for y in range(1, 11):
        cf *= 1 + (g1 if y <= 5 else g2) / 100
        v += cf / (1 + r / 100) ** y
    v += cf * (1 + t / 100) / ((r - t) / 100) / (1 + r / 100) ** 10
    return (v - (net_debt or 0.0)) / shares


def outlook_v2(m: dict, sector_pe: float | None, market_pe: float | None, assume: dict | None = None) -> dict | None:
    """The one valuation of the system: the map's violet copies, the hunt, the card's bear/base/bull and the DCF defaults.

    Value per share by two independent methods, and how much to trust it:
    1. Earnings: profit a year ahead (growth fading to 4% over five years) times a fair P/E (own history + sector);
       a multiple above the market's fades toward it as the growth fades, because a slower company is not paid like a fast one.
    2. Cash: a ten-year DCF of free cash flow at the required return.
    Together: the geometric mean. The value path over time follows the earnings path. This is a value, not a price forecast.
    `assume` holds the investor's own assumptions from the card (growth, required return, terminal growth, fair P/E).
    """
    eps, g0 = m.get("eps_ttm"), m.get("growth_exp")
    a = assume or {}
    if not eps or eps <= 0 or (g0 is None and a.get("g") is None):
        return None
    pe0 = a.get("pe") or fair_multiple(m.get("pe_hist") or [], sector_pe)
    if pe0 is None:
        return None
    g_auto = min(g0, GROWTH_CAP_VALUE) if g0 is not None else None
    base = {"g": a.get("g", g_auto), "r": a.get("r", REQUIRED_RETURN), "t": a.get("t", TERMINAL), "pe": pe0}
    mk = market_pe or 18.0

    def value(g: float, r: float, t: float, pe: float) -> tuple[float, float | None, callable]:
        pe_at = lambda tt: pe if pe <= mk else mk + (pe - mk) * max(0.0, 1 - tt / FADE_YEARS)  # noqa: E731
        v_pe = lambda h: eps_after(eps, g, h + 1.0) * pe_at(h + 1.0)  # noqa: E731
        by_cash = dcf_per_share(m.get("fcf_ttm") or 0, g, m.get("shares") or 0, m.get("net_debt") or 0, r, t)
        by_pe = v_pe(0.0)
        now = (by_pe * by_cash) ** 0.5 if by_cash and by_cash > 0 else by_pe
        return now, by_cash, v_pe

    now, by_cash, v_pe = value(**base)
    by_pe = v_pe(0.0)
    bear = value(max(-10.0, base["g"] - 5), base["r"] + 1, base["t"], base["pe"] * 0.85)[0]
    bull = value(min(GROWTH_CAP_VALUE + 5, base["g"] + 5), max(base["t"] + 1, base["r"] - 1), base["t"], base["pe"] * 1.15)[0]
    why = []
    if not a and g0 is not None and g0 > GROWTH_CAP_VALUE:
        why.append(f"צמיחה של {g0:.0f}% בשנה נספרה כ-{GROWTH_CAP_VALUE:.0f}%")
    if by_cash and by_cash > 0 and abs(math.log(by_pe / by_cash)) > math.log(1.6):
        why.append("שתי שיטות ההערכה (רווח ותזרים) רחוקות זו מזו")
    h = sorted(x for x in (m.get("pe_hist") or [])[-60:] if x and x > 0)
    if len(h) >= 12 and h[3 * len(h) // 4] / max(h[len(h) // 4], 1e-9) > 2.2:
        why.append("המכפיל של החברה היה תנודתי מאוד בחמש השנים האחרונות")
    if not by_cash or by_cash <= 0:
        why.append("אין תזרים חופשי חיובי לבדיקה שנייה")
    conf = "high" if not why else "medium" if len(why) == 1 and not why[0].startswith("שתי") else "low"
    return {"g0": g0, "g": base["g"], "r": base["r"], "t": base["t"], "pe": base["pe"], "pe_mkt": mk, "sector_pe": sector_pe,
            "by_pe": by_pe, "by_cash": by_cash, "now": now, "bear": min(bear, now), "bull": max(bull, now), "conf": conf, "why": why,
            "mine": bool(a), "assume": a, "path": {str(y): now * v_pe(y) / by_pe for y in HORIZONS}}


def fair_from_outlook(o: dict, band: dict | None) -> dict:
    """The card's bear / base / bull, from the same valuation as the map."""
    return {"bear": o["bear"], "base": o["now"], "bull": o["bull"], "growth_used_pct": o["g"], "pe": (band or {}).get("pe"),
            "required": o["r"], "mine": o["mine"]}
