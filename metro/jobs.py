"""Background schedule. Every job checks that its source is configured and records its last run."""
from __future__ import annotations

import logging
import traceback
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from . import alerts, brief, clock, pipeline
from .config import get_settings
from .db import Holding, Watch, kv_set, session, utcnow

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

    def brief_tick(self) -> str:
        b = brief.build()
        sent = alerts.send_telegram(brief.to_text(b))
        alerts.mark_digest_sent(datetime.now(clock.IL).date())
        with session() as s:
            kv_set(s, "brief.last", b)
        return f"sent={sent}"

    def refresh_live_symbols(self) -> None:
        if self.live:
            with session() as s:
                syms = list(s.scalars(select(Holding.ticker))) + list(s.scalars(select(Watch.ticker)))
            self.live.set_symbols(list(dict.fromkeys(syms)))

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
        add(safe("alerts", lambda: alerts.dispatch()), "cron", minute="*/5")
        if st.ibkr_live:
            from .sources.ibkr import IbkrLive
            self.live = IbkrLive(lambda rows: pipeline.update_quotes(rows, "ibkr"))
            self.refresh_live_symbols()
            self.live.start()
            add(self.refresh_live_symbols, "interval", minutes=10)
            on.append("IBKR live")
        self.sched.start()
        return on

    def stop(self) -> None:
        self.sched.shutdown(wait=False)
        if self.live:
            self.live.stop()
