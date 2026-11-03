"""Finnhub free plan: earnings calendar, EPS surprises (4 quarters), company news. 60 calls/minute."""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timezone
from typing import Any

import httpx

from ..config import get_settings

BASE = "https://finnhub.io/api/v1"


class FinnhubError(RuntimeError):
    pass


class FinnhubClient:
    def __init__(self, token: str | None = None, per_minute: int = 55, transport: httpx.BaseTransport | None = None):
        self.token = token or get_settings().finnhub_api_key
        if not self.token:
            raise FinnhubError("חסר FINNHUB_API_KEY (חשבון חינמי ב-finnhub.io).")
        self.http = httpx.Client(base_url=BASE, timeout=20, transport=transport)
        self.gap = 60.0 / per_minute
        self.lock = threading.Lock()
        self.last = 0.0

    def _get(self, path: str, **params: Any) -> Any:
        for attempt in range(4):
            with self.lock:
                wait = self.last + self.gap - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self.last = time.monotonic()
            r = self.http.get(path, params={**params, "token": self.token})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            raise FinnhubError(f"Finnhub {r.status_code}: {r.text[:200]}")
        raise FinnhubError("Finnhub: חריגה ממכסת הקריאות.")

    def earnings_calendar(self, start: date, end: date, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"from": start.isoformat(), "to": end.isoformat()}
        if symbol:
            params["symbol"] = symbol
        js = self._get("/calendar/earnings", **params)
        return [parse_calendar_row(r) for r in js.get("earningsCalendar", []) if r.get("symbol")]

    def surprises(self, symbol: str) -> list[dict[str, Any]]:
        return [{"period": r.get("period"), "actual": r.get("actual"), "estimate": r.get("estimate"),
                 "surprise_pct": r.get("surprisePercent"), "quarter": r.get("quarter"), "year": r.get("year")}
                for r in self._get("/stock/earnings", symbol=symbol) or []]

    def news(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        rows = self._get("/company-news", symbol=symbol, **{"from": start.isoformat(), "to": end.isoformat()}) or []
        return [{"id": r.get("id"), "ts": datetime.fromtimestamp(r.get("datetime", 0), timezone.utc).replace(tzinfo=None),
                 "headline": r.get("headline", ""), "source": r.get("source", ""), "url": r.get("url", ""),
                 "summary": r.get("summary", "")} for r in rows]


def parse_calendar_row(r: dict[str, Any]) -> dict[str, Any]:
    return {"ticker": r["symbol"], "day": date.fromisoformat(r["date"]), "hour": r.get("hour") or "",
            "fiscal": f"Q{r.get('quarter')} {r.get('year')}" if r.get("quarter") else "",
            "eps_est": r.get("epsEstimate"), "eps_act": r.get("epsActual"),
            "rev_est": r.get("revenueEstimate"), "rev_act": r.get("revenueActual")}
