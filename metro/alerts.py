"""Alerts: few, sharp, actionable. Urgent ignores quiet hours; everything else waits for the morning brief."""
from __future__ import annotations

import html
import logging
from datetime import date, datetime, time, timedelta

import httpx
from sqlalchemy import select

from . import clock
from .config import get_settings
from .db import Alert, Earnings, Event, Holding, Quote, Score, Watch, session, utcnow

log = logging.getLogger(__name__)
LEVEL_HE = {"urgent": "דחוף", "important": "חשוב", "digest": "לסיכום"}


def raise_alert(s, ticker: str, level: str, kind: str, title: str, body: str, dedupe: str) -> bool:
    if s.scalar(select(Alert.id).where(Alert.dedupe == dedupe)):
        return False
    s.add(Alert(ticker=ticker, level=level, kind=kind, title=title, body=body, dedupe=dedupe[:120]))
    return True


def evaluate_rules(now: datetime | None = None) -> int:
    now = now or utcnow()
    today = now.date()
    n = 0
    with session() as s:
        held = {h.ticker: h for h in s.scalars(select(Holding))}
        watch = {w.ticker: w for w in s.scalars(select(Watch))}
        scores = {sc.ticker: sc for sc in s.scalars(select(Score))}
        recent = s.scalars(select(Event).where(Event.ts >= now - timedelta(days=2)))
        for e in recent:
            if e.ticker in held and e.severity in ("veto", "high"):
                n += raise_alert(s, e.ticker, "urgent", e.kind, f"{e.ticker}: {e.title}", "אירוע חמור במניה שבתיק.", f"ev:{e.dedupe}")
            elif e.ticker in held and e.kind == "thesis":
                n += raise_alert(s, e.ticker, "important", "thesis", f"{e.ticker}: {e.title}",
                                 "; ".join((e.data or {}).get("reasons", [])[:3]), f"ev:{e.dedupe}")
            elif e.ticker in watch and e.severity in ("veto", "high", "good"):
                n += raise_alert(s, e.ticker, "important", e.kind, f"{e.ticker}: {e.title}", "מניה ברשימת המעקב.", f"ev:{e.dedupe}")
        for t in held:
            sc = scores.get(t)
            sig = (sc.metrics or {}).get("sigma") if sc else None
            if sig is not None and abs(sig) >= 2.5:
                chg = sc.metrics.get("chg_pct", 0)
                n += raise_alert(s, t, "important", "move", f"{t} {chg:+.1f}% · {sig:+.1f}σ",
                                 _decomp_text(sc.metrics), f"move:{t}:{today}")
        for t, w in watch.items():
            q = s.get(Quote, t)
            if q and w.entry_low and w.entry_high and w.entry_low <= q.price <= w.entry_high:
                n += raise_alert(s, t, "important", "entry", f"{t} באזור הכניסה: ${q.price:.2f}",
                                 f"טווח שהגדרת: ${w.entry_low:.2f} עד ${w.entry_high:.2f}", f"entry:{t}:{today}")
        for t, sc in scores.items():
            if sc.beacon:
                n += raise_alert(s, t, "digest", "beacon", f"אלומה חדשה: {t} (ציון {sc.total:.0f})", "", f"beacon:{t}:{today.isocalendar()[1]}")
        soon = clock.add_trading_days(today, 2)
        for er in s.scalars(select(Earnings).where(Earnings.day >= today, Earnings.day <= soon)):
            if er.ticker in held:
                n += raise_alert(s, er.ticker, "digest", "earnings", f"{er.ticker} מדווחת ב-{er.day:%d.%m}",
                                 "דוח בתוך תקופת ההחזקה.", f"earn:{er.ticker}:{er.day}")
    return n


def _decomp_text(m: dict) -> str:
    d = m.get("decomp") or {}
    if not d:
        return ""
    return f"שוק {d.get('market', 0):+.2f}% · סקטור {d.get('sector', 0):+.2f}% · ייחודי {d.get('idio', 0):+.2f}%"


def in_quiet_hours(at: datetime | None = None) -> bool:
    st = get_settings()
    t = (at or datetime.now(clock.IL)).astimezone(clock.IL).time()
    a, b = time.fromisoformat(st.quiet_start), time.fromisoformat(st.quiet_end)
    return (t >= a or t < b) if a > b else (a <= t < b)


def send_telegram(text: str, transport: httpx.BaseTransport | None = None) -> bool:
    st = get_settings()
    if not st.telegram_ready:
        return False
    with httpx.Client(timeout=15, transport=transport) as http:
        r = http.post(f"https://api.telegram.org/bot{st.telegram_bot_token}/sendMessage",
                      json={"chat_id": st.telegram_chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})
    if r.status_code != 200:
        log.warning("telegram %s: %s", r.status_code, r.text[:200])
    return r.status_code == 200


def dispatch(at: datetime | None = None, transport: httpx.BaseTransport | None = None) -> int:
    quiet = in_quiet_hours(at)
    sent = 0
    with session() as s:
        for a in s.scalars(select(Alert).where(Alert.sent.is_(False), Alert.level.in_(("urgent", "important"))).order_by(Alert.ts)):
            if quiet and a.level != "urgent":
                continue
            text = f"<b>{LEVEL_HE[a.level]} · {html.escape(a.title)}</b>" + (f"\n{html.escape(a.body)}" if a.body else "")
            if send_telegram(text, transport):
                a.sent = True
                sent += 1
    return sent


def mark_digest_sent(day: date) -> None:
    with session() as s:
        for a in s.scalars(select(Alert).where(Alert.sent.is_(False), Alert.level == "digest")):
            a.sent = True
