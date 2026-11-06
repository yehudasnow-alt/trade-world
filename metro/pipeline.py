"""Ingest and compute: filings -> facts/sections/events -> scores -> theses -> alerts.

Each step is a plain function so the scheduler, the CLI and tests call the same code.
Failures are isolated per company and recorded, never fatal to the whole run.
"""
from __future__ import annotations

import re

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, select

from . import clock
from .analytics import events as EV
from .analytics import scoring, thesis
from .config import get_settings
from .db import (AiJob, Company, Earnings, Event, Fact, Filing, Holding, PriceBar, Quarter, Quote, ReportCard, Score,
                 Thesis, Watch, kv_age_seconds, kv_get, kv_set, session, utcnow)
from .sources import sections as SX
from .sources import xbrl
from .universe import MARKET_ETF, SECTORS

log = logging.getLogger(__name__)
FORMS = ("10-Q", "10-K", "10-Q/A", "10-K/A", "8-K", "4", "20-F", "6-K")


# ---------------- storage helpers ----------------
def docs_dir(ticker: str, accession: str) -> Path:
    p = get_settings().data_dir / "docs" / ticker / accession
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_doc(ticker: str, accession: str, name: str, content: Any) -> None:
    p = docs_dir(ticker, accession) / name
    p.write_text(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False), encoding="utf-8")


def load_doc(ticker: str, accession: str, name: str, default: Any = None) -> Any:
    p = get_settings().data_dir / "docs" / ticker / accession / name
    if not p.exists():
        return default
    txt = p.read_text(encoding="utf-8")
    return json.loads(txt) if name.endswith(".json") else txt


def focus_tickers(s) -> set[str]:
    return set(s.scalars(select(Holding.ticker))) | set(s.scalars(select(Watch.ticker)))


def add_event(s, ticker: str, ts: datetime, kind: str, code: str, severity: str, title: str, url: str = "",
              data: dict | None = None, dedupe: str | None = None) -> bool:
    key = (dedupe or f"{kind}:{ticker}:{code}:{ts:%Y%m%d%H%M}")[:80]
    if s.scalar(select(Event.id).where(Event.dedupe == key)):
        return False
    s.add(Event(ticker=ticker, ts=ts, kind=kind, code=code, severity=severity, title=title, url=url, data=data or {}, dedupe=key))
    return True


# ---------------- SEC ----------------
def upsert_filings(s, company: Company, refs, since: date) -> list[Filing]:
    new = []
    known = set(s.scalars(select(Filing.accession).where(Filing.cik == company.cik)))
    for r in refs:
        if r.form not in FORMS or r.filed < since or r.accession in known:
            continue
        f = Filing(accession=r.accession, cik=company.cik, ticker=company.ticker, form=r.form, filed=r.filed,
                   accepted=r.accepted, report_date=r.report_date, items=r.items, primary_doc=r.primary_doc)
        s.add(f)
        new.append(f)
    return new


def sync_filings(client, tickers: list[str] | None = None, days: int = 400) -> int:
    """Full sweep of company filing histories (nightly safety net and first backfill)."""
    n = 0
    with session() as s:
        comps = list(s.scalars(select(Company).where(Company.in_universe.is_(True))))
    for c in comps:
        if tickers and c.ticker not in tickers:
            continue
        try:
            js = client.submissions(c.cik)
            from .sources.sec import parse_submissions
            refs = parse_submissions(js)
            with session() as s:
                comp = s.get(Company, c.ticker)
                comp.fiscal_year_end = js.get("fiscalYearEnd") or comp.fiscal_year_end
                n += len(upsert_filings(s, comp, refs, date.today() - timedelta(days=days)))
        except Exception as e:  # noqa: BLE001
            log.warning("filings %s: %s", c.ticker, e)
    return n


def poll_current(client) -> list[str]:
    """Fast path: EDGAR's live feed, matched to our CIKs. Returns new accession numbers."""
    with session() as s:
        by_cik = {c.cik: c.ticker for c in s.scalars(select(Company).where(Company.in_universe.is_(True)))}
        known = set(s.scalars(select(Filing.accession).where(Filing.filed >= date.today() - timedelta(days=10))))
    hits: dict[int, list[str]] = {}
    for form in ("8-K", "10-Q", "10-K", "4"):
        for row in client.current(form):
            if row["cik"] in by_cik and row["accession"] not in known:
                hits.setdefault(row["cik"], []).append(row["accession"])
    new = []
    for cik, accs in hits.items():
        from .sources.sec import parse_submissions
        refs = [r for r in parse_submissions(client.submissions(cik, ttl=0)) if r.accession in accs]
        with session() as s:
            comp = s.get(Company, by_cik[cik])
            new += [f.accession for f in upsert_filings(s, comp, refs, date.today() - timedelta(days=10))]
    with session() as s:
        kv_set(s, "sec.feed", {"new": len(new)})
    return new


def refresh_facts(client, ticker: str) -> int:
    with session() as s:
        c = s.get(Company, ticker)
        cik, fye = c.cik, c.fiscal_year_end
    facts = client.companyfacts(cik, ttl=0)
    rows = xbrl.store_rows(facts)
    quarters = xbrl.build_quarters(facts, fye, min_end=date.today() - timedelta(days=365 * 7))
    with session() as s:
        s.execute(delete(Fact).where(Fact.cik == cik))
        seen = set()
        for r in rows:
            k = (r["concept"], r["unit"], r["start"], r["end"], r["accn"])
            if k not in seen:
                seen.add(k)
                s.add(Fact(cik=cik, **r))
        write_quarters(s, ticker, quarters)
    return len(quarters)


def write_quarters(s, ticker: str, rows: list[xbrl.QuarterRow]) -> None:
    s.execute(delete(Quarter).where(Quarter.ticker == ticker))
    for r in rows:
        s.add(Quarter(ticker=ticker, period_end=r.end, period_start=r.start, fiscal_label=r.fiscal, calendar_label=r.calendar,
                      first_filed=r.first_filed, values=r.values, first_values=r.first_values, derived=r.derived,
                      restated=r.restated))


def _previous_sections(s, f: Filing) -> dict | None:
    prev = s.scalars(select(Filing).where(Filing.ticker == f.ticker, Filing.form == f.form, Filing.filed < f.filed,
                                          Filing.status.in_(("parsed", "carded"))).order_by(Filing.filed.desc())).first()
    return load_doc(prev.ticker, prev.accession, "sections.json") if prev else None


def process_filing(client, accession: str) -> str:
    """Parse one filing, create events, queue the report card. Returns a short status."""
    from .sources.sec import FilingRef
    with session() as s:
        f = s.get(Filing, accession)
        ref = FilingRef(f.accession, f.form, f.filed, f.accepted, f.report_date, f.items, f.primary_doc, f.cik)
        ticker, form, items = f.ticker, f.form, list(f.items)
        focus = ticker in focus_tickers(s)
    ts = ref.accepted or datetime.combine(ref.filed, datetime.min.time())
    status, note = "parsed", ""
    try:
        if form == "8-K":
            item_text = {}
            if "5.02" in items or "4.02" in items or "1.05" in items:
                item_text = SX.split_8k(client.text(ref.url))
            specs = EV.classify_8k(items, item_text)
            press = ""
            if "2.02" in items:
                docs = client.filing_documents(ref)
                ex = next((d for d in docs if d["type"].upper().startswith("EX-99")), None)
                if ex:
                    press = client.text(ex["url"])
                    save_doc(ticker, accession, "press_release.txt", press)
            with session() as s:
                for sp in specs:
                    add_event(s, ticker, ts, "8k", sp.code, sp.severity, sp.title, ref.index_url,
                              dedupe=f"8k:{accession}:{sp.code}")
                if press:
                    queue_ai(s, ticker, accession, "release", deep=focus)
        elif form.startswith(("10-Q", "10-K")):
            refresh_facts(client, ticker)
            text = client.text(ref.url)
            secs = SX.split_filing(form, text)
            save_doc(ticker, accession, "sections.json", secs)
            with session() as s:
                f = s.get(Filing, accession)
                prev = _previous_sections(s, f)
                if prev:
                    save_doc(ticker, accession, "diffs.json", SX.compare_filings(prev, secs))
                if EV.going_concern(text):
                    add_event(s, ticker, ts, "8k", "going-concern", "veto", "ספק לגבי המשך פעילות (going concern)",
                              ref.url, dedupe=f"gc:{accession}")
                queue_ai(s, ticker, accession, "final", deep=focus or get_settings().ai_deep_scope == "all")
        elif form == "4":
            xml = client.get(f"{ref.folder}/{EV.form4_xml_name(ref.primary_doc)}").decode("utf-8", "replace")
            buys = EV.open_market_buys(EV.parse_form4(xml))
            total = sum(b.value for b in buys)
            if total >= 100_000:
                who = ", ".join(sorted({f"{b.owner} ({b.title})" if b.title else b.owner for b in buys}))
                with session() as s:
                    add_event(s, ticker, ts, "form4", "P", "good", f"קנייה בשוק של בעל עניין: ${total:,.0f}", ref.url,
                              data={"who": who, "usd": total}, dedupe=f"f4:{accession}")
        else:
            status = "skipped"
    except Exception as e:  # noqa: BLE001
        status, note = "error", str(e)[:500]
        log.warning("filing %s %s: %s", ticker, accession, e)
    with session() as s:
        f = s.get(Filing, accession)
        f.status, f.note = status, note
    return status


def process_new(client, limit: int = 60) -> dict[str, int]:
    with session() as s:
        accs = list(s.scalars(select(Filing.accession).where(Filing.status == "new").order_by(Filing.filed.desc()).limit(limit)))
    out: dict[str, int] = {}
    for a in accs:
        st = process_filing(client, a)
        out[st] = out.get(st, 0) + 1
    return out


def queue_ai(s, ticker: str, accession: str, stage: str, deep: bool) -> None:
    exists = s.scalar(select(AiJob.id).where(AiJob.ticker == ticker, AiJob.accession == accession, AiJob.stage == stage))
    if not exists:
        s.add(AiJob(ticker=ticker, accession=accession, stage=stage, deep=deep))


# ---------------- prices, earnings, portfolio ----------------
def update_quotes(rows: dict[str, dict[str, Any]], source: str) -> int:
    with session() as s:
        for t, q in rows.items():
            row = s.get(Quote, t)
            ts = q.get("ts") or utcnow()
            if row is None:
                s.add(Quote(ticker=t, price=q["price"], prev_close=q.get("prev_close"), volume=q.get("volume"), ts=ts, source=source))
            elif source == "ibkr" or row.source != "ibkr" or (utcnow() - row.ts).total_seconds() > 120:
                row.price, row.ts, row.source = q["price"], ts, source
                if q.get("prev_close"):
                    row.prev_close = q["prev_close"]
                if q.get("volume"):
                    row.volume = q["volume"]
        kv_set(s, f"quotes.{source}", {"n": len(rows)})
    return len(rows)


def all_symbols() -> list[str]:
    with session() as s:
        from .db import tickers
        return tickers(s) + [MARKET_ETF] + [v[1] for v in SECTORS.values()]


def refresh_snapshots(alpaca) -> int:
    return update_quotes(alpaca.snapshots(all_symbols()), "alpaca")


def store_bars(bars: dict[str, list[dict]]) -> int:
    n = 0
    with session() as s:
        for t, rows in bars.items():
            for b in rows:
                s.merge(PriceBar(ticker=t, day=b["day"], o=b["o"], h=b["h"], l=b["l"], c=b["c"], v=b["v"]))
                n += 1
        kv_set(s, "prices.daily", {"rows": n})
    return n


def update_bars(alpaca, days: int = 10, tickers: list[str] | None = None) -> int:
    syms = tickers or all_symbols()
    return store_bars(alpaca.daily_bars(syms, date.today() - timedelta(days=days)))


def update_earnings(finnhub, ahead: int = 45, back: int = 14) -> int:
    rows = finnhub.earnings_calendar(date.today() - timedelta(days=back), date.today() + timedelta(days=ahead))
    with session() as s:
        from .db import tickers
        uni = set(tickers(s))
        n = 0
        for r in rows:
            if r["ticker"] not in uni:
                continue
            s.merge(Earnings(ticker=r["ticker"], day=r["day"], hour=r["hour"], fiscal=r["fiscal"], eps_est=r["eps_est"],
                             eps_act=r["eps_act"], rev_est=r["rev_est"], rev_act=r["rev_act"], source="finnhub"))
            n += 1
        kv_set(s, "earnings", {"n": n})
    return n


def apply_flex(parsed: dict[str, Any], source: str = "flex") -> int:
    """Replaces the holdings with a full snapshot of the account (Flex each morning, or IB Gateway live)."""
    with session() as s:
        if source == "flex" and kv_get(s, "ibkr.live_ok") and (kv_age_seconds(s, "ibkr.portfolio") or 1e9) < 900:
            # the live portfolio is fresher than last night's statement: keep it, only record the statement
            kv_set(s, "ibkr.flex", {"positions": len(parsed["positions"]), "nav": parsed["nav"], "skipped": True})
            return len(parsed["positions"])
        s.execute(delete(Holding))
        for p in parsed["positions"]:
            if not p["ticker"] or not p["quantity"]:
                continue
            s.add(Holding(ticker=p["ticker"], quantity=p["quantity"], avg_cost=p["avg_cost"], market_value=p["market_value"],
                          weight_pct=p["weight_pct"], pnl_pct=p["pnl_pct"], currency=p["currency"], updated=utcnow()))
        info = {"positions": len(parsed["positions"]), "nav": parsed["nav"], "cash": parsed.get("cash"), "source": source}
        kv_set(s, "ibkr.flex" if source == "flex" else "ibkr.portfolio", info)
        kv_set(s, "portfolio.last", info)
        if source != "flex":
            kv_set(s, "ibkr.live_ok", True)
    return len(parsed["positions"])


# ---------------- news ----------------
# headlines that can move a stock on their own; everything else stays out of the timeline
MAJOR_NEWS = re.compile(r"\b(launch|unveil|introduc|acqui|merger|takeover|buyout|spin.?off|FDA|approv|clearance|recall|lawsuit|sues|"
                        r"settle|investigat|probe|antitrust|guidance|outlook|CEO|CFO|resign|steps? down|appoint|partnership|"
                        r"contract|layoff|job cuts|stock split|buyback|repurchase|tariff|ban|halt)", re.I)


def update_news(client, tickers: list[str] | None = None, days: int = 3) -> int:
    """Major headlines for holdings and watchlist (Finnhub company news), stored as timeline events."""
    today = date.today()
    with session() as s:
        tickers = tickers or sorted(focus_tickers(s))
    n = 0
    for t in tickers:
        try:
            rows = client.news(t, today - timedelta(days=days), today)
        except Exception as e:  # noqa: BLE001
            log.warning("news %s: %s", t, e)
            continue
        with session() as s:
            for r in rows[:40]:
                if not r["headline"] or not MAJOR_NEWS.search(r["headline"]):
                    continue
                n += add_event(s, t, r["ts"], "news", "news", "watch", r["headline"][:300], r["url"],
                               data={"source": r["source"], "summary": (r["summary"] or "")[:400]}, dedupe=f"news:{t}:{r['id'] or r['headline'][:40]}")
    with session() as s:
        kv_set(s, "news", {"tickers": len(tickers), "added": n})
    return n


# ---------------- scoring ----------------
def _series(s, ticker: str, since: date) -> dict[date, PriceBar]:
    return {b.day: b for b in s.scalars(select(PriceBar).where(PriceBar.ticker == ticker, PriceBar.day >= since).order_by(PriceBar.day))}


def quarters_for(s, ticker: str, as_of: date | None = None) -> list[dict]:
    rows = list(s.scalars(select(Quarter).where(Quarter.ticker == ticker).order_by(Quarter.period_end)))
    out = []
    for q in rows:
        if as_of and (q.first_filed is None or q.first_filed > as_of):
            continue
        vals = (q.first_values or q.values) if as_of else q.values
        out.append({"end": q.period_end, "values": vals, "first_filed": q.first_filed, "fiscal": q.fiscal_label,
                    "calendar": q.calendar_label})
    return out


def eps_points(quarters: list[dict]) -> list[tuple[date, float]]:
    pts = []
    for i in range(3, len(quarters)):
        win = quarters[i - 3:i + 1]
        if all("eps_diluted" in q["values"] for q in win) and quarters[i]["first_filed"]:
            pts.append((quarters[i]["first_filed"], sum(q["values"]["eps_diluted"] for q in win)))
    return sorted(pts)


def latest_card(s, ticker: str) -> ReportCard | None:
    return s.scalars(select(ReportCard).where(ReportCard.ticker == ticker).order_by(ReportCard.created.desc())).first()


def guidance_change(card: dict | None) -> float | None:
    for g in (card or {}).get("guidance", []):
        if "revenue" in (g.get("metric") or "").lower() and g.get("prior_low") and g.get("low") and g.get("high") and g.get("prior_high"):
            return ((g["low"] + g["high"]) / (g["prior_low"] + g["prior_high"]) - 1) * 100
    return None


def _ny_day(ts: datetime) -> date:
    """Quotes are stored as naive UTC; the session they belong to is the New York date."""
    from datetime import timezone
    return ts.replace(tzinfo=timezone.utc).astimezone(clock.NY).date()


def build_inputs(s, c: Company, since: date, as_of: date | None = None, live: bool = True,
                 cache: dict | None = None) -> scoring.Inputs | None:
    etf = SECTORS.get(c.sector, ("", "SPY"))[1]
    cache = cache if cache is not None else {}
    for sym in (MARKET_ETF, etf):
        if sym not in cache:
            cache[sym] = _series(s, sym, since)
    st, mk, se = _series(s, c.ticker, since), cache[MARKET_ETF], cache[etf]
    days = [d for d in st if d in mk and d in se and (as_of is None or d <= as_of)]
    if len(days) < 60:
        return None
    closes = [st[d].c for d in days]
    highs, lows, vols = [st[d].h for d in days], [st[d].l for d in days], [st[d].v for d in days]
    mkt, sec = [mk[d].c for d in days], [se[d].c for d in days]
    vol_today, progress = None, 1.0
    if live and as_of is None:
        q, qm, qs = s.get(Quote, c.ticker), s.get(Quote, MARKET_ETF), s.get(Quote, etf)
        if q and qm and qs and _ny_day(q.ts) > days[-1]:
            closes.append(q.price)
            highs.append(max(q.price, closes[-2]))
            lows.append(min(q.price, closes[-2]))
            mkt.append(qm.price)
            sec.append(qs.price)
            vol_today = q.volume
            progress = clock.session_progress()
    today = as_of or date.today()
    ev = [{"day": e.ts.date(), "code": e.code, "severity": e.severity, "title": e.title}
          for e in s.scalars(select(Event).where(Event.ticker == c.ticker, Event.ts >= datetime.combine(today - timedelta(days=365), datetime.min.time())))
          if e.ts.date() <= today]
    card = None if as_of else latest_card(s, c.ticker)
    cd = card.card if card else None
    er = s.scalars(select(Earnings).where(Earnings.ticker == c.ticker, Earnings.day <= today, Earnings.eps_act.is_not(None))
                   .order_by(Earnings.day.desc())).first()
    surprise = ((er.eps_act / er.eps_est - 1) * 100) if er and er.eps_est and er.eps_est > 0 else None
    qs_ = quarters_for(s, c.ticker, as_of)
    return scoring.Inputs(c.ticker, c.sector, c.template, days, closes, highs, lows, vols, mkt, sec,
                          volume_today=vol_today, session_progress=progress, quarters=qs_, eps_points=eps_points(qs_),
                          events=ev, guidance=(cd or {}).get("guidance_direction") if cd else None,
                          guidance_change_pct=guidance_change(cd), surprise_pct=surprise,
                          going_concern=any(e["code"] == "going-concern" for e in ev), today=today)


def compute_scores(as_of: date | None = None, persist: bool = True) -> dict[str, dict]:
    since = (as_of or date.today()) - timedelta(days=365 * 6)
    raws: dict[str, dict] = {}
    with session() as s:
        comps = list(s.scalars(select(Company).where(Company.in_universe.is_(True))))
        cache: dict = {}
        for c in comps:
            try:
                x = build_inputs(s, c, since, as_of, cache=cache)
                if x:
                    raws[c.ticker] = scoring.raw_metrics(x)
            except Exception as e:  # noqa: BLE001
                log.warning("score %s: %s", c.ticker, e)
    scores = scoring.score_universe(raws)
    if persist and as_of is None:
        with session() as s:
            for t, sc in scores.items():
                m = {k: v for k, v in raws[t].items() if k not in ("pe_hist",)}
                row = s.get(Score, t) or Score(ticker=t)
                row.computed, row.quality, row.direction, row.valuation = utcnow(), sc["quality"], sc["direction"], sc["valuation"]
                row.timing, row.total, row.veto, row.beacon = sc["timing"], sc["total"], sc["veto"] + sc["flags"], sc["beacon"]
                row.metrics = _jsonable(m)
                s.merge(row)
            kv_set(s, "scores", {"n": len(scores)})
        check_theses(raws)
    for t in scores:
        scores[t]["raw"] = raws[t]
    return scores


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, float):
        return None if x != x else round(x, 6)
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    return x


def check_theses(raws: dict[str, dict]) -> list[tuple[str, str, str]]:
    """Evaluate every thesis; returns (ticker, old, new) for status changes."""
    changes = []
    with session() as s:
        for th in s.scalars(select(Thesis)):
            m = raws.get(th.ticker)
            if not m:
                continue
            ev = [{"code": e.code} for e in s.scalars(select(Event).where(
                Event.ticker == th.ticker, Event.ts >= utcnow() - timedelta(days=30)))]
            card = latest_card(s, th.ticker)
            res = thesis.evaluate({"metrics": th.metrics, "kills": th.kills}, m, ev,
                                  card.card.get("guidance_direction") if card else None)
            if res["status"] != th.status:
                changes.append((th.ticker, th.status, res["status"]))
                names = {"ok": "תקפה", "watch": "במעקב", "broken": "נשברה"}
                add_event(s, th.ticker, utcnow(), "thesis", res["status"], "high" if res["status"] == "broken" else "watch",
                          f"התזה עברה ממצב {names.get(th.status, th.status)} למצב {names[res['status']]}",
                          data={"reasons": res["reasons"]}, dedupe=f"th:{th.ticker}:{res['status']}:{date.today()}")
            th.status, th.reasons, th.checked = res["status"], res["reasons"], utcnow()
    return changes


# ---------------- first fill ----------------
def backfill_all(tickers: Optional[list[str]] = None, years: int = 6, echo=log.info) -> dict:
    """Everything a new installation needs: filing history and XBRL facts from the SEC, daily prices, the earnings
    calendar, the portfolio, then scores. Used by `metro backfill` and, in the cloud, on the first start."""
    from datetime import timedelta

    from .universe import MARKET_ETF, SECTOR_ETFS
    st = get_settings()
    out: dict[str, Any] = {}
    with session() as s:
        kv_set(s, "mode", "live")
        comps = [c.ticker for c in s.scalars(select(Company).where(Company.in_universe.is_(True))) if not tickers or c.ticker in tickers]
    if st.sec_ready:
        from .sources.sec import SecClient
        sec = SecClient()
        echo("SEC: היסטוריית דיווחים...")
        out["filings"] = sync_filings(sec, comps, days=400)
        echo(f"  {out['filings']} דיווחים חדשים. עובדות XBRL...")
        for i, t in enumerate(comps):
            try:
                refresh_facts(sec, t)
            except Exception as e:  # noqa: BLE001
                echo(f"  {t}: {e}")
            if i % 50 == 49:
                echo(f"  companyfacts {i + 1}/{len(comps)}")
                with session() as s:
                    kv_set(s, "bootstrap.progress", {"step": "facts", "done": i + 1, "of": len(comps)})
        with session() as s:  # history is context; only the latest filings get parsed and carded
            for f in s.scalars(select(Filing).where(Filing.status == "new", Filing.filed < date.today() - timedelta(days=21))):
                f.status = "skipped"
        out["processed"] = process_new(sec, limit=1000)
        echo(f"  מעבד דיווחים אחרונים: {out['processed']}")
    else:
        echo("SEC_USER_AGENT לא מוגדר: מדלג על ה-SEC.")
    if st.alpaca_ready:
        from .sources.alpaca import AlpacaClient
        al = AlpacaClient()
        echo("מחירים יומיים...")
        with session() as s:
            kv_set(s, "bootstrap.progress", {"step": "prices"})
        out["bars"] = update_bars(al, days=365 * years, tickers=comps + [MARKET_ETF] + SECTOR_ETFS)
        echo(f"  {out['bars']} נרות. ציטוטים: {refresh_snapshots(al)}")
    else:
        echo("מפתחות Alpaca לא מוגדרים: מדלג על מחירים.")
    if st.finnhub_api_key:
        from .sources.finnhub import FinnhubClient
        out["earnings"] = update_earnings(FinnhubClient(), ahead=60, back=120)
        echo(f"יומן דוחות: {out['earnings']} שורות")
    if st.flex_ready:
        from .sources import ibkr
        out["positions"] = apply_flex(ibkr.parse_flex(ibkr.fetch_flex()))
        echo(f"תיק IBKR: {out['positions']} פוזיציות")
    out["scores"] = len(compute_scores())
    echo(f"ציונים חושבו ל-{out['scores']} חברות.")
    return out

