"""Background schedule. Every job checks that its source is configured and records its last run."""
from __future__ import annotations

import logging
import traceback
from datetime import date, datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from . import alerts, brief, clock, pipeline
from .config import get_settings
from .db import Holding, Watch, kv_get, kv_set, session, utcnow

log = logging.getLogger(__name__)


def _record(name: str, ok: bool, info: str = "") -> None:
    with session() as s:
        kv_set(s, f"job.{name}", {"ok": ok, "info": info[:500], "at": utcnow().isoformat()})


def safe(name: str, fn: Callable[[], object]) -> Callable[[], None]:
    def run() -> None:
        try:
            res = fn()
            _record(name, True, "" if res is None else str(res))
        except Exception as e:  # noqa: BLE001
            log.warning("job %s failed: %s", name, e)
            _record(name, False, f"{e}\n{traceback.format_exc()[-800:]}")
    run.__name__ = name
    return run


class Runner:
    def __init__(self) -> None:
        self.sched = BackgroundScheduler(timezone=clock.NY, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120})
        self.sec = self.alpaca = self.finnhub = self.engine = self.live = None

    # --- job bodies ---
    def sec_tick(self) -> str:
        new = pipeline.poll_current(self.sec)
        done = pipeline.process_new(self.sec)
        return f"new={len(new)} processed={done}"

    def sec_nightly(self) -> str:
        n = pipeline.sync_filings(self.sec, days=30)
        return f"new={n} processed={pipeline.process_new(self.sec, limit=400)}"

    def prices_tick(self) -> str:
        state = clock.session_state()
        if state == "closed" or (state in ("pre", "post") and clock.now_ny().minute % 5):
            return f"skip ({state})"
        return f"quotes={pipeline.refresh_snapshots(self.alpaca)}"

    def after_close(self) -> str:
        n = pipeline.update_bars(self.alpaca, days=10)
        self.score_tick(force=True)
        return f"bars={n}"

    def score_tick(self, force: bool = False) -> str:
        if not force and clock.session_state() != "open":
            return "skip"
        sc = pipeline.compute_scores()
        a = alerts.evaluate_rules()
        sent = alerts.dispatch()
        return f"scores={len(sc)} alerts={a} sent={sent}"

    def earnings_tick(self) -> str:
        return f"rows={pipeline.update_earnings(self.finnhub)}"

    def flex_tick(self) -> str:
        from .sources import ibkr
        n = pipeline.apply_flex(ibkr.parse_flex(ibkr.fetch_flex()))
        self.refresh_live_symbols()
        return f"positions={n}"

    def ai_tick(self) -> str:
        from .ai import runner
        stats = runner.run_jobs(self.engine)
        return f"{stats} batches_done={runner.poll_batches(self.engine)}"

    def alerts_tick(self) -> str:
        """Rules every 5 minutes, day and night: an 8-K filed after the close must not wait for the next session."""
        a = alerts.evaluate_rules()
        return f"alerts={a} sent={alerts.dispatch()}"

    def brief_tick(self) -> str:
        b = brief.build()
        sent = alerts.send_telegram(brief.to_text(b))
        alerts.mark_digest_sent(datetime.now(clock.IL).date())
        with session() as s:
            kv_set(s, "brief.last", b)
        return f"sent={sent}"

    def live_portfolio(self, pf: dict) -> None:
        """Called from the IB Gateway thread about every 30 seconds with the open positions."""
        try:
            pipeline.apply_flex(pf, source="ibkr_live")
            self.refresh_live_symbols()
        except Exception as e:  # noqa: BLE001
            log.warning("live portfolio: %s", e)

    def refresh_live_symbols(self) -> None:
        if self.live:
            with session() as s:
                syms = list(s.scalars(select(Holding.ticker))) + list(s.scalars(select(Watch.ticker)))
            self.live.set_symbols(list(dict.fromkeys(syms)))

    def universe_tick(self) -> str:
        """The S&P 500 changes a few times a quarter: newcomers get their history now, leavers drop off the map."""
        from datetime import timedelta

        from .db import Filing
        from .universe import refresh_universe
        joined, left = refresh_universe()
        if joined:
            if self.sec:
                pipeline.sync_filings(self.sec, joined, days=400)
                for t in joined:
                    try:
                        pipeline.refresh_facts(self.sec, t)
                    except Exception as e:  # noqa: BLE001
                        log.warning("facts %s: %s", t, e)
                with session() as s:  # history is context: only the last three weeks get parsed and carded
                    for f in s.scalars(select(Filing).where(Filing.ticker.in_(joined), Filing.status == "new", Filing.filed < date.today() - timedelta(days=21))):
                        f.status = "skipped"
            if self.alpaca:
                pipeline.update_bars(self.alpaca, days=6 * 365, tickers=joined)
            pipeline.compute_scores()
        if joined or left:
            now = utcnow()
            with session() as s:
                for t in joined:
                    pipeline.add_event(s, t, now, "index", "joined", "watch", "נכנסה למדד S&P 500", dedupe=f"index:{t}:in:{now:%Y%m%d}")
                for t in left:
                    pipeline.add_event(s, t, now, "index", "left", "watch", "יצאה ממדד S&P 500", dedupe=f"index:{t}:out:{now:%Y%m%d}")
        return f"joined={','.join(joined) or '-'} left={','.join(left) or '-'}"

    def backup_tick(self) -> str:
        from . import backup, cloud
        cloud.prune_disk()
        files = backup.backup()
        return " ".join(f.name for f in files[:1]) + (f" (+{len(files) - 1} copy)" if len(files) > 1 else "")

    # --- wiring ---
    def start(self) -> list[str]:
        st = get_settings()
        on = []
        add = self.sched.add_job
        if st.sec_ready:
            from .sources.sec import SecClient
            self.sec = SecClient()
            add(safe("sec", self.sec_tick), "cron", day_of_week="mon-fri", hour="6-21", minute="*/2")
            add(safe("sec_nightly", self.sec_nightly), "cron", hour=2, minute=30)
            on.append("SEC")
        if st.alpaca_ready:
            from .sources.alpaca import AlpacaClient
            self.alpaca = AlpacaClient()
            add(safe("prices", self.prices_tick), "cron", day_of_week="mon-fri", hour="4-19", minute="*")
            add(safe("after_close", self.after_close), "cron", day_of_week="mon-fri", hour=16, minute=25)
            on.append("Alpaca")
        add(safe("scores", self.score_tick), "cron", day_of_week="mon-fri", hour="9-15", minute="*/15")
        if st.finnhub_api_key:
            from .sources.finnhub import FinnhubClient
            self.finnhub = FinnhubClient()
            add(safe("earnings", self.earnings_tick), "cron", hour=17, minute=35)
            add(safe("news", lambda: f"added={pipeline.update_news(self.finnhub)}"), "cron", day_of_week="mon-fri", hour="7-19/2", minute=10)
            on.append("Finnhub")
        if st.flex_ready:
            add(safe("flex", self.flex_tick), "cron", day_of_week="mon-sat", hour=6, minute=40)
            on.append("IBKR Flex")
        if st.anthropic_api_key:
            from .ai.engine import Engine
            self.engine = Engine()
            add(safe("ai", self.ai_tick), "interval", minutes=10)
            on.append("Claude")
        hh, mm = (int(x) for x in st.brief_time.split(":"))
        add(safe("brief", self.brief_tick), "cron", hour=hh, minute=mm, timezone=clock.IL)
        add(safe("alerts", self.alerts_tick), "cron", minute="*/5")
        add(safe("backup", self.backup_tick), "cron", hour=3, minute=15)
        add(safe("universe", self.universe_tick), "cron", day_of_week="mon-fri", hour=5, minute=20)
        if st.ibkr_live:
            from .sources.ibkr import IbkrLive
            self.live = IbkrLive(lambda rows: pipeline.update_quotes(rows, "ibkr"), on_portfolio=self.live_portfolio)
            self.refresh_live_symbols()
            self.live.start()
            add(self.refresh_live_symbols, "interval", minutes=10)
            on.append("IBKR live")
        if self.needs_first_fill():
            # a fresh installation (in the cloud nobody is there to type `metro backfill`): fill it now, in the background
            from datetime import timedelta
            add(safe("bootstrap", self.bootstrap_tick), "date", run_date=datetime.now(clock.NY) + timedelta(seconds=20))
        self.sched.start()
        return on

    def needs_first_fill(self) -> bool:
        st = get_settings()
        if not (st.sec_ready or st.alpaca_ready):
            return False
        from .db import Company, PriceBar
        with session() as s:
            state = (kv_get(s, "bootstrap") or {}).get("state")
            has_bars = s.scalars(select(PriceBar.ticker).limit(1)).first() is not None
            has_live = s.scalars(select(Company.ticker).where(Company.in_universe.is_(True)).limit(1)).first() is not None
        return has_live and state != "done" and (not has_bars or state == "running")

    def bootstrap_tick(self) -> str:
        with session() as s:
            kv_set(s, "bootstrap", {"state": "running", "started": utcnow().isoformat()})
        out = pipeline.backfill_all()
        with session() as s:
            kv_set(s, "bootstrap", {"state": "done", "finished": utcnow().isoformat(), **{k: v for k, v in out.items() if isinstance(v, int)}})
        return " ".join(f"{k}={v}" for k, v in out.items())

    def stop(self) -> None:
        self.sched.shutdown(wait=False)
        if self.live:
            self.live.stop()
