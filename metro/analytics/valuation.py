"""Valuation: multiples, own-history percentile, reverse DCF and a fair-value range (not a single number)."""
from __future__ import annotations

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
