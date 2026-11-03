"""Interactive Brokers, read-only.

* Flex Web Service: positions and trades as a daily statement. No running software needed.
  Create a token (Account Management > Reporting > Flex Web Service) and an Activity Flex
  Query with the Open Positions and Trades sections.
* Live quotes (optional): IB Gateway running with "Read-Only API" enabled (the default), via ib_async.
  The free Cboe One + IEX stream covers US stocks; about 100 symbols at once.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Callable
from xml.etree import ElementTree as ET

import httpx

from ..config import get_settings
from ..universe import from_ibkr_symbol, ibkr_symbol

log = logging.getLogger(__name__)
FLEX_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"


class FlexError(RuntimeError):
    pass


def _num(v: str | None) -> float | None:
    if v in (None, "", "--"):
        return None
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return None


def parse_flex(xml_text: str) -> dict[str, Any]:
    """Positions, trades and net liquidation value from a Flex statement."""
    root = ET.fromstring(xml_text)
    positions, trades = [], []
    for p in root.iter("OpenPosition"):
        a = p.attrib
        if a.get("assetCategory", "STK") != "STK" or a.get("levelOfDetail", "SUMMARY") not in ("SUMMARY", ""):
            continue
        qty = _num(a.get("position")) or 0.0
        cost = _num(a.get("costBasisPrice")) or _num(a.get("openPrice"))
        mark = _num(a.get("markPrice"))
        positions.append({
            "ticker": from_ibkr_symbol(a.get("symbol", "")), "quantity": qty, "avg_cost": cost,
            "mark": mark, "market_value": _num(a.get("positionValue")) or (qty * mark if mark else None),
            "pnl_unrealized": _num(a.get("fifoPnlUnrealized")), "weight_pct": _num(a.get("percentOfNAV")),
            "currency": a.get("currency", "USD"),
        })
    for t in root.iter("Trade"):
        a = t.attrib
        if a.get("assetCategory", "STK") != "STK":
            continue
        d = a.get("tradeDate", "")
        trades.append({
            "ticker": from_ibkr_symbol(a.get("symbol", "")), "day": _flex_date(d), "side": a.get("buySell", ""),
            "quantity": _num(a.get("quantity")), "price": _num(a.get("tradePrice")), "id": a.get("tradeID", ""),
        })
    nav = None
    for e in root.iter("EquitySummaryByReportDateInBase"):
        nav = _num(e.attrib.get("total")) or nav
    for e in root.iter("ChangeInNAV"):
        nav = _num(e.attrib.get("endingValue")) or nav
    if nav is None:
        mv = [p["market_value"] for p in positions if p["market_value"]]
        nav = sum(mv) if mv else None
    for p in positions:
        if p["weight_pct"] is None and nav and p["market_value"]:
            p["weight_pct"] = p["market_value"] / nav * 100
        if p["avg_cost"] and p["mark"]:
            p["pnl_pct"] = (p["mark"] / p["avg_cost"] - 1) * 100 * (1 if p["quantity"] >= 0 else -1)
        else:
            p["pnl_pct"] = None
    return {"positions": positions, "trades": trades, "nav": nav}


def _flex_date(s: str) -> date | None:
    s = s.split(";")[0].strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def fetch_flex(token: str | None = None, query_id: str | None = None, max_wait: float = 120,
               transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> str:
    st = get_settings()
    token, query_id = token or st.ibkr_flex_token, query_id or st.ibkr_flex_query_id
    if not (token and query_id):
        raise FlexError("חסרים IBKR_FLEX_TOKEN ו-IBKR_FLEX_QUERY_ID.")
    http = httpx.Client(timeout=30, transport=transport, headers={"User-Agent": "Metropolin/0.2"})
    r = http.get(FLEX_URL, params={"t": token, "q": query_id, "v": "3"})
    root = ET.fromstring(r.text)
    if (root.findtext("Status") or "") != "Success":
        raise FlexError(f"IBKR Flex: {root.findtext('ErrorCode') or ''} {root.findtext('ErrorMessage') or r.text[:200]}")
    code, url = root.findtext("ReferenceCode"), root.findtext("Url")
    waited = 0.0
    while waited <= max_wait:
        sleep(2)
        waited += 2
        r = http.get(url, params={"q": code, "t": token, "v": "3"})
        text = r.text
        if "<FlexQueryResponse" in text:
            return text
        try:
            msg = ET.fromstring(text).findtext("ErrorMessage") or ""
        except ET.ParseError:
            msg = text[:200]
        if "progress" not in msg.lower():
            raise FlexError(f"IBKR Flex: {msg}")
    raise FlexError("IBKR Flex: הדוח לא הוכן בזמן. נסה שוב בעוד כמה דקות.")


class IbkrLive:
    """Streams quotes for holdings + watchlist into a callback. Never places orders: readonly=True."""

    def __init__(self, on_quotes: Callable[[dict[str, dict[str, Any]]], None]):
        self.on_quotes = on_quotes
        self.symbols: list[str] = []
        self.status = "stopped"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def set_symbols(self, tickers: list[str]) -> None:
        self.symbols = tickers[: get_settings().ibkr_max_lines]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ibkr-live", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        backoff = 5
        while not self._stop.is_set():
            try:
                loop.run_until_complete(self._session())
                backoff = 5
            except Exception as e:  # connection refused, gateway restart, 2FA timeout
                self.status = f"error: {e}"
                log.warning("IBKR live: %s", e)
            if self._stop.wait(backoff):
                break
            backoff = min(300, backoff * 2)
        self.status = "stopped"

    async def _session(self) -> None:
        from ib_async import IB, Stock  # optional dependency
        st = get_settings()
        ib = IB()
        await ib.connectAsync(st.ibkr_host, st.ibkr_port, clientId=st.ibkr_client_id, readonly=True, timeout=15)
        self.status = "connected"
        ib.reqMarketDataType(1)  # live where permitted; IB falls back to delayed otherwise
        subscribed: dict[str, Any] = {}
        try:
            while not self._stop.is_set() and ib.isConnected():
                want = set(self.symbols)
                for t in [t for t in subscribed if t not in want]:
                    ib.cancelMktData(subscribed.pop(t).contract)
                for t in want - set(subscribed):
                    c = Stock(ibkr_symbol(t), "SMART", "USD")
                    subscribed[t] = ib.reqMktData(c, "", False, False)
                await asyncio.sleep(3)
                out = {}
                for t, tk in subscribed.items():
                    px = tk.marketPrice()
                    if px and px == px and px > 0:
                        out[t] = {"price": float(px), "prev_close": float(tk.close) if tk.close == tk.close and tk.close else None,
                                  "volume": float(tk.volume) if tk.volume == tk.volume and tk.volume else None,
                                  "ts": datetime.now(timezone.utc).replace(tzinfo=None)}
                if out:
                    self.on_quotes(out)
        finally:
            ib.disconnect()
