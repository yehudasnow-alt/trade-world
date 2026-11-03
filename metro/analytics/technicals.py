"""Price-based measures. Inputs are plain lists, oldest first."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def pct_returns(closes: list[float]) -> np.ndarray:
    c = np.asarray(closes, dtype=float)
    return c[1:] / c[:-1] - 1 if len(c) > 1 else np.array([])


def sma(closes: list[float], n: int) -> float | None:
    return float(np.mean(closes[-n:])) if len(closes) >= n else None


def atr_pct(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    h, l_, c = (np.asarray(x[-(n + 1):], dtype=float) for x in (highs, lows, closes))
    tr = np.maximum(h[1:] - l_[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l_[1:] - c[:-1])))
    return float(tr.mean() / c[-1] * 100)


def ret_std_pct(closes: list[float], n: int = 60) -> float | None:
    r = pct_returns(closes[-(n + 1):])
    return float(r.std(ddof=1) * 100) if len(r) >= 20 else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n * 3:
        return None
    d = np.diff(np.asarray(closes, dtype=float))
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:n].mean(), dn[:n].mean()
    for u, v in zip(up[n:], dn[n:]):  # Wilder smoothing
        au, ad = (au * (n - 1) + u) / n, (ad * (n - 1) + v) / n
    return 100.0 if ad == 0 else float(100 - 100 / (1 + au / ad))


def drawdown(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None, n: int = 252) -> dict:
    hs = (highs or closes)[-n:]
    ls = (lows or closes)[-n:]
    hi, lo, px = max(hs), min(ls), closes[-1]
    return {"high52": hi, "low52": lo, "dd_pct": (px / hi - 1) * 100, "from_low_pct": (px / lo - 1) * 100}


def rvol(volume_today: float | None, volumes: list[float], progress: float = 1.0, n: int = 20) -> float | None:
    """Volume relative to the 20-day average, scaled by how much of the session has passed."""
    if not volume_today or len(volumes) < n:
        return None
    avg = float(np.mean(volumes[-n:]))
    return volume_today / (avg * max(0.05, progress)) if avg else None


@dataclass
class Decomp:
    beta_market: float
    beta_sector: float
    market: float
    sector: float
    idio: float
    r2: float


def decompose(stock: list[float], market: list[float], sector: list[float], today_pct: float | None = None,
              n: int = 252) -> Decomp | None:
    """Two-factor split of a move: r = a + bm*r_mkt + bs*(r_sector - r_mkt) + e.

    Series must be aligned by date. The last element is today's move unless `today_pct` is given.
    """
    k = min(len(stock), len(market), len(sector), n + 1)
    if k < 60:
        return None
    rs, rm, rsec = (pct_returns(x[-k:]) for x in (stock, market, sector))
    x = np.column_stack([np.ones(len(rm) - 1), rm[:-1], (rsec - rm)[:-1]])
    y = rs[:-1]
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    fit = x @ coef
    ss_res, ss_tot = float(((y - fit) ** 2).sum()), float(((y - y.mean()) ** 2).sum())
    r_today = (today_pct / 100) if today_pct is not None else rs[-1]
    m_part = coef[1] * rm[-1]
    s_part = coef[2] * (rsec[-1] - rm[-1])
    return Decomp(float(coef[1]), float(coef[2]), float(m_part * 100), float(s_part * 100),
                  float((r_today - m_part - s_part) * 100), 1 - ss_res / ss_tot if ss_tot else 0.0)


def rel_strength(stock: list[float], bench: list[float], n: int) -> float | None:
    if len(stock) <= n or len(bench) <= n:
        return None
    return ((stock[-1] / stock[-1 - n]) - (bench[-1] / bench[-1 - n])) * 100


def volume_profile(closes: list[float], volumes: list[float], bins: int = 24, n: int = 252, top: int = 3) -> list[dict]:
    """Price levels where most volume traded over the last year (support / resistance zones)."""
    c, v = np.asarray(closes[-n:], dtype=float), np.asarray(volumes[-n:], dtype=float)
    if len(c) < 40:
        return []
    hist, edges = np.histogram(c, bins=bins, weights=v)
    order = np.argsort(hist)[::-1][:top]
    total = hist.sum() or 1
    return [{"low": float(edges[i]), "high": float(edges[i + 1]), "share": float(hist[i] / total)} for i in sorted(order)]


def earnings_reactions(days: list, closes: list[float], earn_days: list, hour: dict | None = None) -> list[dict]:
    """Close-to-close move on each past report (after-close reports move the next day)."""
    idx = {d: i for i, d in enumerate(days)}
    out = []
    for d in earn_days:
        i = idx.get(d)
        if i is None or i == 0:
            continue
        j = i + 1 if (hour or {}).get(d) == "amc" and i + 1 < len(closes) else i
        out.append({"day": d, "move_pct": (closes[j] / closes[j - 1] - 1) * 100})
    return out
