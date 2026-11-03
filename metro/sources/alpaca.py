"""Alpaca market data (free Basic plan works).

Free plan: real-time IEX trades, full-market (SIP) data delayed 15 minutes,
daily bars since 2016, 200 requests/minute. One snapshots call covers many
symbols, so the whole city refreshes in three calls.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from ..config import get_settings
from ..universe import alpaca_symbol

BASE = "https://data.alpaca.markets/v2"


class AlpacaError(RuntimeError):
    pass


def _chunks(xs: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


class AlpacaClient:
    def __init__(self, key: str | None = None, secret: str | None = None, feed: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        st = get_settings()
        key, secret = key or st.alpaca_key_id, secret or st.alpaca_secret_key
        if not (key and secret):
            raise AlpacaError("חסרים ALPACA_KEY_ID ו-ALPACA_SECRET_KEY (חשבון חינמי ב-alpaca.markets מספיק).")
        self.feed = feed or st.alpaca_feed
        self.http = httpx.Client(base_url=BASE, timeout=30, transport=transport,
                                 headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        for attempt in range(4):
            r = self.http.get(path, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            if r.status_code == 403 and params.get("feed") in ("sip", "delayed_sip"):
                params = {**params, "feed": "iex"}  # plan does not allow this feed: fall back
                continue
            raise AlpacaError(f"Alpaca {r.status_code}: {r.text[:200]}")
        raise AlpacaError("Alpaca: יותר מדי בקשות, נסה שוב בעוד דקה.")

    def snapshots(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        """ticker -> {price, prev_close, volume, ts}."""
        out: dict[str, dict[str, Any]] = {}
        sym = {alpaca_symbol(t): t for t in tickers}
        for chunk in _chunks(list(sym), 200):
            js = self._get("/stocks/snapshots", {"symbols": ",".join(chunk), "feed": self.feed})
            data = js.get("snapshots", js) if isinstance(js, dict) else {}
            for s, snap in data.items():
                if not snap or s not in sym:
                    continue
                parsed = parse_snapshot(snap)
                if parsed:
                    out[sym[s]] = parsed
        return out

    def daily_bars(self, tickers: list[str], start: date, end: date | None = None) -> dict[str, list[dict[str, Any]]]:
        """Split- and dividend-adjusted daily bars. End defaults to 20 minutes ago (free-plan SIP delay)."""
        end_dt = datetime.now(timezone.utc) - timedelta(minutes=20) if end is None else \
            datetime(end.year, end.month, end.day, 23, 59, tzinfo=timezone.utc)
        sym = {alpaca_symbol(t): t for t in tickers}
        out: dict[str, list[dict[str, Any]]] = {t: [] for t in tickers}
        feed = "sip" if self.feed in ("sip", "delayed_sip") else "iex"
        for chunk in _chunks(list(sym), 100):
            token = None
            while True:
                params = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": start.isoformat(),
                          "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "adjustment": "all", "feed": feed, "limit": 10000}
                if token:
                    params["page_token"] = token
                js = self._get("/stocks/bars", params)
                for s, bars in (js.get("bars") or {}).items():
                    if s in sym:
                        out[sym[s]].extend(parse_bar(b) for b in bars)
                token = js.get("next_page_token")
                if not token:
                    break
        return out


def parse_bar(b: dict[str, Any]) -> dict[str, Any]:
    return {"day": date.fromisoformat(b["t"][:10]), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v", 0)}


def parse_snapshot(snap: dict[str, Any]) -> dict[str, Any] | None:
    trade, daily, prev = snap.get("latestTrade") or {}, snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
    price = trade.get("p") or daily.get("c")
    if not price:
        return None
    return {"price": float(price), "prev_close": float(prev["c"]) if prev.get("c") else None,
            "volume": float(daily.get("v") or 0), "ts": parse_ts(trade.get("t") or daily.get("t")),
            "day": (daily.get("t") or "")[:10]}


def parse_ts(s: str | None) -> datetime | None:
    """RFC 3339 with up to nanoseconds ('2026-11-02T20:59:59.123456789Z') -> naive UTC."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    m = re.match(r"^(.*?T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", s)
    if m:
        frac = (m.group(2) or "")[:6]
        s = m.group(1) + (f".{frac}" if frac else "") + m.group(3)
    dt = datetime.fromisoformat(s)
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
