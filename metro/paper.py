"""Paper trading: a practice account with virtual cash that trades on the real prices.

The point is to learn the system before risking money, so the account behaves like a cautious real one:
- a market order fills at the live price while the market is open; outside the session it waits and fills at the open;
- limit and stop orders are checked every minute during the session, and fill at the price the market actually gave
  (a stock that gaps through a stop fills below it, as it would for real);
- a buy can carry a protective stop and a target taken from the trade calculator; they wake up when the buy fills,
  and when one of them fills the other is cancelled;
- no shorting and no margin: a buy needs the cash, a sell needs the shares;
- a commission close to IBKR's fixed plan ($0.005 a share, $1 minimum, at most 1% of the trade).
Every order keeps the convergence score and valuation at the moment it was placed, so later you can see which
signals your winners and losers came from. The account is measured against the S&P 500 (SPY) from day one.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock
from .config import get_settings
from .db import (Company, PaperAccount, PaperDaily, PaperOrder, PaperPosition, Quote, Score, Thesis, kv_get,
                 utcnow)
from .universe import MARKET_ETF

FEE_PER_SHARE, FEE_MIN, FEE_MAX_PCT = 0.005, 1.0, 0.01
STALE_MIN = 15  # a price older than this is not traded on
OPEN_STATES = ("pending", "held")


class PaperError(ValueError):
    """A order that cannot be placed; the message is shown to the user as is."""


def fee(qty: float, px: float) -> float:
    return round(min(max(FEE_MIN, FEE_PER_SHARE * qty), FEE_MAX_PCT * qty * px), 2)


def _utc(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts


def _r(x: Optional[float], n: int = 2) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, n)


# ---------------- account ----------------
def account(s: Session) -> PaperAccount:
    a = s.scalars(select(PaperAccount).where(PaperAccount.active.is_(True)).order_by(PaperAccount.id.desc())).first()
    if a is None:
        cash = get_settings().paper_start_cash
        q = s.get(Quote, MARKET_ETF)
        a = PaperAccount(start_cash=cash, cash=cash, spy_start=q.price if q else None, active=True)
        s.add(a)
        s.flush()
    elif a.spy_start is None and (q := s.get(Quote, MARKET_ETF)):
        a.spy_start = q.price
    return a


def reset(s: Session, cash: Optional[float] = None) -> PaperAccount:
    """Start over. The old account stays in the database (inactive), so its history is never lost."""
    cash = cash or get_settings().paper_start_cash
    if not 1_000 <= cash <= 100_000_000:
        raise PaperError("סכום פתיחה בין $1,000 ל-$100,000,000")
    for a in s.scalars(select(PaperAccount).where(PaperAccount.active.is_(True))):
        a.active = False
        for o in s.scalars(select(PaperOrder).where(PaperOrder.account_id == a.id, PaperOrder.status.in_(OPEN_STATES))):
            o.status, o.reason = "cancelled", "החשבון אופס"
    q = s.get(Quote, MARKET_ETF)
    a = PaperAccount(start_cash=cash, cash=cash, spy_start=q.price if q else None, active=True)
    s.add(a)
    s.flush()
    return a


# ---------------- prices and the session ----------------
def _demo(s: Session) -> bool:
    return kv_get(s, "mode", "live") == "demo"


def market_fills(s: Session, now: Optional[datetime] = None) -> bool:
    """Orders fill only while the regular session is open (the demo database has frozen prices, so it always fills)."""
    return _demo(s) or clock.session_state(now) == "open"


def live_price(s: Session, ticker: str, now: Optional[datetime] = None) -> Optional[float]:
    """The price an order may fill at: a quote from this session, at most STALE_MIN minutes old."""
    q = s.get(Quote, ticker)
    if q is None or not q.price:
        return None
    if _demo(s):
        return q.price
    now = (now or clock.now_ny()).astimezone(clock.NY)
    opened = _utc(datetime.combine(now.date(), clock.OPEN, tzinfo=clock.NY))
    ts, at = _utc(q.ts), _utc(now)
    return q.price if ts >= opened and at - ts <= timedelta(minutes=STALE_MIN) else None


def mark(s: Session, ticker: str) -> Optional[float]:
    q = s.get(Quote, ticker)
    return q.price if q and q.price else None


# ---------------- orders ----------------
def _snapshot(s: Session, t: str) -> dict:
    sc = s.get(Score, t)
    m = (sc.metrics or {}) if sc else {}
    return {"score": round(sc.total) if sc else None, "parts": {"q": sc.quality, "d": sc.direction, "v": sc.valuation, "t": sc.timing} if sc else None,
            "pe_pct": _r(m.get("pe_pct_5y"), 0), "dd": _r(m.get("dd_pct"), 1), "price": mark(s, t)}


def _reserved(s: Session, a: PaperAccount) -> float:
    """Cash already promised to buy orders that are waiting."""
    out = 0.0
    for o in s.scalars(select(PaperOrder).where(PaperOrder.account_id == a.id, PaperOrder.side == "buy", PaperOrder.status == "pending")):
        px = o.trigger if o.kind != "market" and o.trigger else mark(s, o.ticker) or 0
        out += o.quantity * px + fee(o.quantity, px)
    return out


def place(s: Session, ticker: str, side: str, qty: float, kind: str = "market", trigger: Optional[float] = None,
          stop: Optional[float] = None, target: Optional[float] = None, note: str = "", now: Optional[datetime] = None) -> PaperOrder:
    t = ticker.upper().strip()
    if side not in ("buy", "sell"):
        raise PaperError("פעולה לא מוכרת")
    if kind not in ("market", "limit", "stop"):
        raise PaperError("סוג הוראה לא מוכר")
    if s.get(Company, t) is None and s.get(Quote, t) is None:
        raise PaperError(f"{t} לא נמצאה")
    qty = math.floor(qty or 0)
    if qty < 1:
        raise PaperError("כמות של מניה אחת לפחות")
    if kind != "market" and not (trigger and trigger > 0):
        raise PaperError("להוראת לימיט או סטופ צריך מחיר")
    a = account(s)
    ref = trigger if kind != "market" else mark(s, t)
    if ref is None:
        raise PaperError(f"אין עדיין מחיר ל-{t}. הוראות דמו צריכות מחירים אמיתיים (Alpaca או IBKR).")
    if side == "sell":
        pos = s.get(PaperPosition, (a.id, t))
        if pos is None:
            raise PaperError(f"אין לך {t} בתיק הדמו. אין מכירה בחסר.")
        if qty > pos.quantity:
            raise PaperError(f"יש לך רק {pos.quantity:g} מניות {t}")
        if stop or target:
            raise PaperError("סטופ ויעד מצורפים להוראת קנייה")
    else:
        need = qty * ref + fee(qty, ref)
        free = a.cash - _reserved(s, a)
        if need > free + 1e-6:
            raise PaperError(f"צריך ${need:,.0f} ויש ${max(free, 0):,.0f} מזומן פנוי")
        if stop is not None and not 0 < stop < ref:
            raise PaperError("הסטופ צריך להיות מתחת למחיר הכניסה")
        if target is not None and not target > ref:
            raise PaperError("היעד צריך להיות מעל מחיר הכניסה")
    o = PaperOrder(account_id=a.id, ticker=t, side=side, quantity=qty, kind=kind, trigger=trigger if kind != "market" else None,
                   status="pending", note=note[:2000], snapshot=_snapshot(s, t), ts=utcnow())
    s.add(o)
    s.flush()
    for k, px in (("stop", stop), ("limit", target)):
        if px:
            s.add(PaperOrder(account_id=a.id, ticker=t, side="sell", quantity=qty, kind=k, trigger=px, status="held",
                             parent_id=o.id, oco=f"b{o.id}", snapshot={}, ts=utcnow()))
    s.flush()
    process(s, now, only=[o.id])
    return o


def cancel(s: Session, order_id: int) -> PaperOrder:
    a = account(s)
    o = s.get(PaperOrder, order_id)
    if o is None or o.account_id != a.id:
        raise PaperError("ההוראה לא נמצאה")
    if o.status not in OPEN_STATES:
        raise PaperError("ההוראה כבר לא פתוחה")
    o.status, o.reason = "cancelled", "בוטלה"
    for ch in s.scalars(select(PaperOrder).where(PaperOrder.parent_id == o.id, PaperOrder.status.in_(OPEN_STATES))):
        ch.status, ch.reason = "cancelled", "הוראת האם בוטלה"
    return o


def _close_siblings(s: Session, o: PaperOrder, why: str) -> None:
    if not o.oco:
        return
    for x in s.scalars(select(PaperOrder).where(PaperOrder.account_id == o.account_id, PaperOrder.oco == o.oco,
                                                PaperOrder.status.in_(OPEN_STATES), PaperOrder.id != o.id)):
        x.status, x.reason = "cancelled", why


def _fill(s: Session, a: PaperAccount, o: PaperOrder, px: float, now: datetime) -> None:
    o.fill_ts = _utc(now)
    if o.side == "buy":
        f = fee(o.quantity, px)
        cost = o.quantity * px + f
        if cost > a.cash + 1e-6:
            o.status, o.reason = "rejected", f"אין מספיק מזומן (${cost:,.0f} מול ${a.cash:,.0f})"
            for ch in s.scalars(select(PaperOrder).where(PaperOrder.parent_id == o.id, PaperOrder.status.in_(OPEN_STATES))):
                ch.status, ch.reason = "cancelled", "הקנייה נדחתה"
            return
        pos = s.get(PaperPosition, (a.id, o.ticker))
        if pos is None:
            s.add(PaperPosition(account_id=a.id, ticker=o.ticker, quantity=o.quantity, avg_cost=cost / o.quantity, opened=o.fill_ts))
        else:
            pos.avg_cost = (pos.quantity * pos.avg_cost + cost) / (pos.quantity + o.quantity)
            pos.quantity += o.quantity
        a.cash -= cost
        for ch in s.scalars(select(PaperOrder).where(PaperOrder.parent_id == o.id, PaperOrder.status == "held")):
            ch.status = "pending"
    else:
        pos = s.get(PaperPosition, (a.id, o.ticker))
        if pos is None or pos.quantity <= 0:
            o.status, o.reason = "rejected", "אין מניות למכור"
            return
        o.quantity = min(o.quantity, pos.quantity)
        f = fee(o.quantity, px)
        o.realized = round((px - pos.avg_cost) * o.quantity - f, 2)
        a.cash += o.quantity * px - f
        a.realized += o.realized
        pos.quantity -= o.quantity
        if o.oco:
            _close_siblings(s, o, "ההוראה התאומה בוצעה")
        if pos.quantity > 1e-9:  # a partial sale: the stop and target protect what is left
            for x in s.scalars(select(PaperOrder).where(PaperOrder.account_id == a.id, PaperOrder.ticker == o.ticker, PaperOrder.side == "sell",
                                                        PaperOrder.status.in_(OPEN_STATES), PaperOrder.id != o.id)):
                x.quantity = min(x.quantity, pos.quantity)
        else:
            s.delete(pos)
            for x in s.scalars(select(PaperOrder).where(PaperOrder.account_id == a.id, PaperOrder.ticker == o.ticker, PaperOrder.side == "sell",
                                                        PaperOrder.status.in_(OPEN_STATES), PaperOrder.id != o.id)):
                x.status, x.reason = "cancelled", "הפוזיציה נסגרה"
    o.status, o.fill_price, o.fee = "filled", round(px, 4), f
    a.fees += f


def _triggered(o: PaperOrder, px: float) -> bool:
    if o.kind == "market":
        return True
    if o.kind == "limit":
        return px <= o.trigger if o.side == "buy" else px >= o.trigger
    return px >= o.trigger if o.side == "buy" else px <= o.trigger


def process(s: Session, now: Optional[datetime] = None, only: Optional[list[int]] = None) -> int:
    """Fill whatever the live prices allow. Runs every minute in the session, and right after an order is placed."""
    now = now or clock.now_ny()
    if not market_fills(s, now):
        return 0
    a = account(s)
    q = select(PaperOrder).where(PaperOrder.account_id == a.id, PaperOrder.status == "pending").order_by(PaperOrder.id)
    if only:
        q = q.where(PaperOrder.id.in_(only))
    n = 0
    for o in list(s.scalars(q)):
        if o.status != "pending":  # cancelled by a sibling earlier in this loop
            continue
        px = live_price(s, o.ticker, now)
        if px is None or not _triggered(o, px):
            continue
        _fill(s, a, o, px, now)
        s.flush()
        n += o.status == "filled"
    return n


def snapshot_day(s: Session, now: Optional[datetime] = None) -> Optional[PaperDaily]:
    """The value at the close, one row a day: the equity curve against the S&P 500."""
    now = now or clock.now_ny()
    day = clock.last_session(now)
    a = account(s)
    nav = summary(s, a)["nav"]
    row = s.get(PaperDaily, (a.id, day))
    if row is None:
        row = PaperDaily(account_id=a.id, day=day, nav=nav, cash=a.cash, spy=mark(s, MARKET_ETF))
        s.add(row)
    else:
        row.nav, row.cash, row.spy = nav, a.cash, mark(s, MARKET_ETF)
    return row


# ---------------- views ----------------
def positions(s: Session, a: PaperAccount) -> list[dict]:
    today = clock.now_ny().date()
    rows = []
    for p in s.scalars(select(PaperPosition).where(PaperPosition.account_id == a.id)):
        q = s.get(Quote, p.ticker)
        px = q.price if q and q.price else p.avg_cost
        base = p.avg_cost if _utc(p.opened).replace(tzinfo=timezone.utc).astimezone(clock.NY).date() == today else (q.prev_close if q and q.prev_close else px)
        rows.append({"t": p.ticker, "q": p.quantity, "cost": _r(p.avg_cost), "px": _r(px), "v": round(p.quantity * px, 2),
                     "pnl": _r((px / p.avg_cost - 1) * 100, 2), "pnl_usd": round((px - p.avg_cost) * p.quantity, 2),
                     "day_usd": round((px - base) * p.quantity, 2), "opened": p.opened.isoformat()})
    return rows


def summary(s: Session, a: Optional[PaperAccount] = None, pos: Optional[list[dict]] = None) -> dict:
    a = a or account(s)
    pos = positions(s, a) if pos is None else pos
    invested = sum(p["v"] for p in pos)
    nav = a.cash + invested
    spy = mark(s, MARKET_ETF)
    pending = s.scalars(select(PaperOrder.id).where(PaperOrder.account_id == a.id, PaperOrder.status == "pending")).all()
    return {"id": a.id, "created": a.created.isoformat(), "start_cash": a.start_cash, "cash": round(a.cash, 2), "nav": round(nav, 2),
            "invested": round(invested, 2), "ret_pct": _r((nav / a.start_cash - 1) * 100), "realized": round(a.realized, 2),
            "unrealized": round(sum(p["pnl_usd"] for p in pos), 2), "day_usd": round(sum(p["day_usd"] for p in pos), 2),
            "fees": round(a.fees, 2), "spy_ret_pct": _r((spy / a.spy_start - 1) * 100) if spy and a.spy_start else None, "spy_start": a.spy_start,
            "positions": len(pos), "pending": len(pending), "fills_now": market_fills(s)}


def city_rows(s: Session) -> tuple[dict[str, dict], dict]:
    """What the map needs: a 'paper' entry per company held, shaped like the real holdings, plus the account summary."""
    a = account(s)
    pos = positions(s, a)
    summ = summary(s, a, pos)
    nav = summ["nav"] or 1
    theses = {x.ticker: x.status for x in s.scalars(select(Thesis).where(Thesis.ticker.in_([p["t"] for p in pos])))} if pos else {}
    rows = {p["t"]: {"w": round(p["v"] / nav * 100, 1), "pnl": _r(p["pnl"], 1), "th": theses.get(p["t"]), "q": p["q"], "cost": p["cost"], "v": round(p["v"])}
            for p in pos}
    return rows, summ


def _order_row(o: PaperOrder) -> dict:
    return {"id": o.id, "ts": o.ts.isoformat(), "t": o.ticker, "side": o.side, "qty": o.quantity, "kind": o.kind, "trigger": o.trigger,
            "status": o.status, "parent": o.parent_id, "oco": o.oco, "fill_price": o.fill_price,
            "fill_ts": o.fill_ts.isoformat() if o.fill_ts else None, "fee": o.fee, "realized": o.realized, "reason": o.reason,
            "note": o.note, "score": (o.snapshot or {}).get("score")}


def stats(s: Session, a: PaperAccount) -> dict:
    sells = [o.realized for o in s.scalars(select(PaperOrder).where(PaperOrder.account_id == a.id, PaperOrder.side == "sell",
                                                                   PaperOrder.status == "filled")) if o.realized is not None]
    wins, losses = [x for x in sells if x > 0], [x for x in sells if x <= 0]
    return {"closed": len(sells), "wins": len(wins), "win_rate": _r(len(wins) / len(sells) * 100, 0) if sells else None,
            "avg_win": _r(sum(wins) / len(wins)) if wins else None, "avg_loss": _r(sum(losses) / len(losses)) if losses else None,
            "best": _r(max(sells)) if sells else None, "worst": _r(min(sells)) if sells else None}


def state(s: Session) -> dict:
    a = account(s)
    pos = positions(s, a)
    names = dict(s.execute(select(Company.ticker, Company.name).where(Company.ticker.in_([p["t"] for p in pos]))).all()) if pos else {}
    for p in pos:
        p["n"] = names.get(p["t"], p["t"])
    q = select(PaperOrder).where(PaperOrder.account_id == a.id)
    open_ = [_order_row(o) for o in s.scalars(q.where(PaperOrder.status.in_(OPEN_STATES)).order_by(PaperOrder.id.desc()))]
    done = [_order_row(o) for o in s.scalars(q.where(PaperOrder.status.not_in(OPEN_STATES)).order_by(PaperOrder.id.desc()).limit(60))]
    curve = [{"d": r.day.isoformat(), "nav": r.nav, "spy": r.spy} for r in
             s.scalars(select(PaperDaily).where(PaperDaily.account_id == a.id).order_by(PaperDaily.day))]
    return {"account": summary(s, a, pos), "positions": sorted(pos, key=lambda p: -p["v"]), "orders": open_, "trades": done,
            "curve": curve, "stats": stats(s, a), "session": clock.session_state()}
