"""Command line: `metro <command>`. Run `metro --help` for the list."""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from typing import Optional

import typer
from sqlalchemy import select

from .config import get_settings
from .db import Company, Filing, init_db, kv_set, session

app = typer.Typer(help="מטרופולין: עיר תלת-ממדית של שוק ההון", no_args_is_help=True, add_completion=False)
GOLD = ["AAPL", "NVDA", "JPM", "PLD", "MSFT"]


def _log(verbose: bool) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command()
def init(refresh: bool = typer.Option(False, help="הורד את רשימת ה-S&P 500 העדכנית")) -> None:
    """צור את מסד הנתונים וטען את יקום החברות."""
    init_db()
    from .universe import sync_universe
    n, added = sync_universe(refresh)
    with session() as s:
        kv_set(s, "mode", "live")
    typer.echo(f"מסד הנתונים מוכן. {n} חברות ביקום ({added} חדשות).")


@app.command()
def demo() -> None:
    """מלא נתוני הדגמה (מחירים ודוחות מומצאים על רשימת החברות האמיתית)."""
    init_db()
    from . import demo as D
    t0 = time.time()
    out = D.generate()
    typer.echo(f"נתוני הדגמה מוכנים תוך {time.time() - t0:.0f} שניות: {out['companies']} חברות, {out['days']} ימי מסחר, אלומות: {', '.join(out['beacons']) or 'אין'}")


@app.command()
def backfill(tickers: Optional[str] = typer.Option(None, help="רשימה מופרדת בפסיקים; ברירת מחדל: כל היקום"),
             years: int = typer.Option(6, help="שנות היסטוריית מחירים"), verbose: bool = False) -> None:
    """משיכה ראשונית: דוחות ועובדות מה-SEC, מחירים יומיים, יומן דוחות."""
    _log(verbose)
    init_db()
    st = get_settings()
    from . import pipeline
    want = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    with session() as s:
        kv_set(s, "mode", "live")
        comps = [c.ticker for c in s.scalars(select(Company).where(Company.in_universe.is_(True))) if not want or c.ticker in want]
    if st.sec_ready:
        from .sources.sec import SecClient
        sec = SecClient()
        typer.echo("SEC: היסטוריית דיווחים...")
        n = pipeline.sync_filings(sec, comps, days=400)
        typer.echo(f"  {n} דיווחים חדשים. עובדות XBRL...")
        with typer.progressbar(comps, label="  companyfacts") as bar:
            for t in bar:
                try:
                    pipeline.refresh_facts(sec, t)
                except Exception as e:  # noqa: BLE001
                    typer.echo(f"  {t}: {e}")
        with session() as s:  # history is context; only the latest filings get parsed and carded
            for f in s.scalars(select(Filing).where(Filing.status == "new", Filing.filed < date.today() - timedelta(days=21))):
                f.status = "skipped"
        typer.echo(f"  מעבד דיווחים אחרונים: {pipeline.process_new(sec, limit=1000)}")
    else:
        typer.echo("SEC_USER_AGENT לא מוגדר: מדלג על ה-SEC.")
    if st.alpaca_ready:
        from .sources.alpaca import AlpacaClient
        al = AlpacaClient()
        typer.echo("מחירים יומיים...")
        syms = comps + ([] if want else [])
        from .universe import MARKET_ETF, SECTOR_ETFS
        n = pipeline.update_bars(al, days=365 * years, tickers=syms + [MARKET_ETF] + SECTOR_ETFS)
        typer.echo(f"  {n} נרות. ציטוטים: {pipeline.refresh_snapshots(al)}")
    else:
        typer.echo("מפתחות Alpaca לא מוגדרים: מדלג על מחירים.")
    if st.finnhub_api_key:
        from .sources.finnhub import FinnhubClient
        typer.echo(f"יומן דוחות: {pipeline.update_earnings(FinnhubClient(), ahead=60, back=120)} שורות")
    if st.flex_ready:
        from .sources import ibkr
        typer.echo(f"תיק IBKR: {pipeline.apply_flex(ibkr.parse_flex(ibkr.fetch_flex()))} פוזיציות")
    sc = pipeline.compute_scores()
    typer.echo(f"ציונים חושבו ל-{len(sc)} חברות.")


@app.command()
def run(host: Optional[str] = None, port: Optional[int] = None, jobs: bool = typer.Option(True, help="הפעל את התזמון"),
        verbose: bool = False) -> None:
    """הפעל את השרת (ממשק בדפדפן) ואת המשימות המתוזמנות."""
    _log(verbose)
    import uvicorn
    init_db()
    st = get_settings()
    from . import webbuild
    if webbuild.SRC.exists() and webbuild.stale():
        webbuild.build_live()
    runner = None
    if jobs:
        from .jobs import Runner
        runner = Runner()
        on = runner.start()
        typer.echo("מקורות פעילים: " + (", ".join(on) if on else "אין (מצב הדגמה או מפתחות חסרים)"))
    typer.echo(f"פתח בדפדפן: http://{host or st.host}:{port or st.port}")
    try:
        uvicorn.run("metro.api:app", host=host or st.host, port=port or st.port, log_level="warning")
    finally:
        if runner:
            runner.stop()


@app.command()
def web(demo: bool = typer.Option(False, "--demo", help="בנה גם קובץ הדגמה עצמאי ל-dist/ (צריך metro demo קודם)")) -> None:
    """בנה את ממשק הדפדפן מ-web/src."""
    from . import webbuild
    for p in webbuild.main(demo):
        typer.echo(f"נבנה {p.relative_to(webbuild.ROOT)} ({p.stat().st_size // 1024} KB)")


@app.command()
def card(ticker: str, dry_run: bool = typer.Option(False, help="הצג גודל ועלות משוערת בלי לשלוח"),
         deep: bool = True) -> None:
    """צור כרטיסיית דוח לדיווח האחרון של חברה (10-Q/10-K)."""
    from .ai import engine as E
    from .ai import runner as R
    from .db import AiJob
    from .pipeline import queue_ai
    t = ticker.upper()
    with session() as s:
        f = s.scalars(select(Filing).where(Filing.ticker == t, Filing.form.in_(("10-Q", "10-K")), Filing.status.in_(("parsed", "carded")))
                      .order_by(Filing.filed.desc())).first()
        if not f:
            raise typer.BadParameter(f"אין דיווח 10-Q/10-K מעובד עבור {t}. הרץ קודם: metro backfill --tickers {t}")
        queue_ai(s, t, f.accession, "final", deep)
    with session() as s:
        job = s.scalars(select(AiJob).where(AiJob.ticker == t, AiJob.accession == f.accession, AiJob.stage == "final")).first()
        job.status, job.deep = "queued", deep
        ctx, src, kind, model, _ = R.build(s, job)
        params = R.params_for(ctx, src, kind, model)
        est = E.estimate(params)
        typer.echo(f"{t} {ctx.period_label}: כ-{est['input_tokens']:,} טוקנים, עלות משוערת ${est['usd']:.2f} ({model})")
        typer.echo(f"מקורות: הודעה {len(src.press_release):,} תווים · דיווח {len(src.filing):,} · שיחה {len(src.transcript_prepared) + len(src.transcript_qa):,}")
        if dry_run:
            job.status = "error"
            job.error = "dry run"
            return
    stats = R.run_jobs(max_single=1)
    typer.echo(json.dumps(stats, ensure_ascii=False))


@app.command()
def gold(tickers: str = ",".join(GOLD)) -> None:
    """הרץ את סט הזהב והדפס את דוח האימות של כל כרטיסייה."""
    from .db import ReportCard
    for t in [x.strip().upper() for x in tickers.split(",")]:
        try:
            card(t, dry_run=False, deep=True)
        except Exception as e:  # noqa: BLE001
            typer.echo(f"{t}: {e}")
            continue
        with session() as s:
            rc = s.scalars(select(ReportCard).where(ReportCard.ticker == t).order_by(ReportCard.created.desc())).first()
            if rc:
                v = rc.verification
                typer.echo(f"{t}: ציטוטים תקינים {v['quote_ok_pct']}% · מספרים מאומתים {v['number_ok_pct']}% · "
                           f"סעיפי כיסוי {v['coverage_keys']}/15 · בעיות: {len(v['problems'])} · ביקורת: {rc.review.get('verdict')}")
                for p in v["problems"][:8]:
                    typer.echo(f"   - {p}")


@app.command()
def flex() -> None:
    """משוך עכשיו את התיק מ-IBKR (Flex)."""
    from . import pipeline
    from .sources import ibkr
    typer.echo(f"{pipeline.apply_flex(ibkr.parse_flex(ibkr.fetch_flex()))} פוזיציות עודכנו.")


@app.command()
def score() -> None:
    """חשב מחדש ציונים, תזות והתראות."""
    from . import alerts, pipeline
    sc = pipeline.compute_scores()
    typer.echo(f"{len(sc)} ציונים · אלומות: {', '.join(t for t, v in sc.items() if v['beacon']) or 'אין'} · התראות חדשות: {alerts.evaluate_rules()}")


@app.command()
def brief(send: bool = False) -> None:
    """הדפס את תדריך הבוקר (ושלח לטלגרם עם --send)."""
    from . import alerts
    from . import brief as B
    text = B.to_text(B.build())
    typer.echo(text.replace("<b>", "").replace("</b>", ""))
    if send:
        typer.echo("נשלח." if alerts.send_telegram(text) else "לא נשלח: בדוק TELEGRAM_BOT_TOKEN ו-TELEGRAM_CHAT_ID.")


@app.command()
def backtest(start: str = "2021-01-01") -> None:
    """בדיקה לאחור של הציון לפי חמישונים."""
    from . import backtest as BT
    out = BT.run(date.fromisoformat(start))
    typer.echo(json.dumps(out, ensure_ascii=False, indent=1))


@app.command()
def status() -> None:
    """מצב המקורות והמשימות."""
    from .api import status as st_
    typer.echo(json.dumps(st_(), ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    app()
