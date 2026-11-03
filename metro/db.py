"""Database models. SQLite by default; any SQLAlchemy URL (e.g. PostgreSQL) works."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint,
    create_engine, event, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    sector: Mapped[str] = mapped_column(String(60))
    sub_industry: Mapped[str] = mapped_column(String(120))
    cik: Mapped[int] = mapped_column(Integer, index=True)
    alt_tickers: Mapped[list] = mapped_column(JSON, default=list)
    fiscal_year_end: Mapped[str | None] = mapped_column(String(4))  # MMDD
    filer_type: Mapped[str] = mapped_column(String(12), default="domestic")  # domestic | foreign
    template: Mapped[str] = mapped_column(String(12), default="standard")  # standard | bank | insurer | reit
    themes: Mapped[list] = mapped_column(JSON, default=list)
    in_universe: Mapped[bool] = mapped_column(Boolean, default=True)


class Fact(Base):
    """A single XBRL fact from SEC companyfacts, for the curated concept list only."""
    __tablename__ = "facts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cik: Mapped[int] = mapped_column(Integer, index=True)
    concept: Mapped[str] = mapped_column(String(120))
    unit: Mapped[str] = mapped_column(String(24))
    start: Mapped[date | None] = mapped_column(Date)
    end: Mapped[date] = mapped_column(Date)
    val: Mapped[float] = mapped_column(Float)
    accn: Mapped[str] = mapped_column(String(24))
    form: Mapped[str] = mapped_column(String(12))
    filed: Mapped[date] = mapped_column(Date)
    __table_args__ = (UniqueConstraint("cik", "concept", "unit", "start", "end", "accn", name="uq_fact"),)


class Quarter(Base):
    """Normalized fiscal quarter, recomputed from facts. Values are the latest filed (restated) figures."""
    __tablename__ = "quarters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    period_end: Mapped[date] = mapped_column(Date)
    period_start: Mapped[date | None] = mapped_column(Date)
    fiscal_label: Mapped[str] = mapped_column(String(32))  # "FQ2 FY26"
    calendar_label: Mapped[str] = mapped_column(String(10))  # "CY2025Q3"
    first_filed: Mapped[date | None] = mapped_column(Date)
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    first_values: Mapped[dict] = mapped_column(JSON, default=dict)  # as first reported (point in time)
    derived: Mapped[list] = mapped_column(JSON, default=list)  # concepts computed by subtraction
    restated: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint("ticker", "period_end", name="uq_quarter"),)


class PriceBar(Base):
    __tablename__ = "prices"
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    o: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    l: Mapped[float] = mapped_column(Float)
    c: Mapped[float] = mapped_column(Float)
    v: Mapped[float] = mapped_column(Float)


class Quote(Base):
    __tablename__ = "quotes"
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    price: Mapped[float] = mapped_column(Float)
    prev_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(16))


class Filing(Base):
    __tablename__ = "filings"
    accession: Mapped[str] = mapped_column(String(24), primary_key=True)
    cik: Mapped[int] = mapped_column(Integer, index=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    form: Mapped[str] = mapped_column(String(16))
    filed: Mapped[date] = mapped_column(Date)
    accepted: Mapped[datetime | None] = mapped_column(DateTime)
    report_date: Mapped[date | None] = mapped_column(Date)
    items: Mapped[list] = mapped_column(JSON, default=list)
    primary_doc: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="new")  # new | parsed | carded | skipped | error
    note: Mapped[str] = mapped_column(Text, default="")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # 8k | form4 | news | earnings | thesis
    code: Mapped[str] = mapped_column(String(16), default="")
    severity: Mapped[str] = mapped_column(String(8), default="info")  # veto | high | watch | info | good
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe: Mapped[str] = mapped_column(String(80), unique=True)


class Earnings(Base):
    __tablename__ = "earnings"
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    hour: Mapped[str] = mapped_column(String(4), default="")  # bmo | amc | dmh
    fiscal: Mapped[str] = mapped_column(String(16), default="")
    eps_est: Mapped[float | None] = mapped_column(Float)
    eps_act: Mapped[float | None] = mapped_column(Float)
    rev_est: Mapped[float | None] = mapped_column(Float)
    rev_act: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), default="finnhub")


class ReportCard(Base):
    __tablename__ = "report_cards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    accession: Mapped[str] = mapped_column(String(24))
    period_end: Mapped[date | None] = mapped_column(Date)
    stage: Mapped[str] = mapped_column(String(12))  # light | release | call | final
    model: Mapped[str] = mapped_column(String(40))
    prompt_version: Mapped[str] = mapped_column(String(16))
    created: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    card: Mapped[dict] = mapped_column(JSON)
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    review: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class AiJob(Base):
    __tablename__ = "ai_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    accession: Mapped[str] = mapped_column(String(24))
    stage: Mapped[str] = mapped_column(String(12))
    deep: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued | batched | done | error
    batch_id: Mapped[str] = mapped_column(String(80), default="")
    created: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    error: Mapped[str] = mapped_column(Text, default="")
    request: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("ticker", "accession", "stage", name="uq_job"),)


class Promise(Base):
    __tablename__ = "promises"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    metric: Mapped[str] = mapped_column(String(120))
    target: Mapped[str] = mapped_column(Text)
    deadline: Mapped[str] = mapped_column(String(60), default="")
    quote: Mapped[str] = mapped_column(Text, default="")
    source_accn: Mapped[str] = mapped_column(String(24), default="")
    made: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | on_track | kept | missed | dropped
    evidence: Mapped[str] = mapped_column(Text, default="")


class Holding(Base):
    __tablename__ = "holdings"
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    quantity: Mapped[float] = mapped_column(Float)
    avg_cost: Mapped[float | None] = mapped_column(Float)
    market_value: Mapped[float | None] = mapped_column(Float)
    weight_pct: Mapped[float | None] = mapped_column(Float)
    pnl_pct: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(6), default="USD")
    updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Thesis(Base):
    __tablename__ = "theses"
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[list] = mapped_column(JSON, default=list)  # [{name, op, value}]
    kills: Mapped[list] = mapped_column(JSON, default=list)  # [{type, value?}]
    horizon_weeks: Mapped[int] = mapped_column(Integer, default=12)
    status: Mapped[str] = mapped_column(String(8), default="ok")  # ok | watch | broken
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    checked: Mapped[datetime | None] = mapped_column(DateTime)


class JournalEntry(Base):
    __tablename__ = "journal"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    action: Mapped[str] = mapped_column(String(8))  # buy | add | trim | sell | pass | note
    price: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="")
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class Watch(Base):
    __tablename__ = "watchlist"
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    added: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    entry_low: Mapped[float | None] = mapped_column(Float)
    entry_high: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="")


class Score(Base):
    __tablename__ = "scores"
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    computed: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    quality: Mapped[float] = mapped_column(Float, default=0)
    direction: Mapped[float] = mapped_column(Float, default=0)
    valuation: Mapped[float] = mapped_column(Float, default=0)
    timing: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float, default=0)
    veto: Mapped[list] = mapped_column(JSON, default=list)
    beacon: Mapped[bool] = mapped_column(Boolean, default=False)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ticker: Mapped[str] = mapped_column(String(12), default="")
    level: Mapped[str] = mapped_column(String(10))  # urgent | important | digest
    kind: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    dedupe: Mapped[str] = mapped_column(String(120), unique=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


_engine = None
_Session: sessionmaker | None = None


def engine():
    global _engine, _Session
    if _engine is None:
        url = get_settings().database_url
        kw = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {}
        _engine = create_engine(url, future=True, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _pragmas(conn, _):  # noqa: ANN001
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
        _Session = sessionmaker(_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    """Used by tests that switch DATABASE_URL."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine, _Session = None, None


def init_db() -> None:
    Base.metadata.create_all(engine())


@contextmanager
def session() -> Iterator[Session]:
    engine()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def kv_get(s: Session, key: str, default: Any = None) -> Any:
    row = s.get(KV, key)
    return default if row is None else row.value


def kv_set(s: Session, key: str, value: Any) -> None:
    row = s.get(KV, key)
    if row is None:
        s.add(KV(key=key, value=value, updated=utcnow()))
    else:
        row.value, row.updated = value, utcnow()


def kv_age_seconds(s: Session, key: str) -> float | None:
    row = s.get(KV, key)
    return None if row is None else (utcnow() - row.updated).total_seconds()


def tickers(s: Session) -> list[str]:
    return list(s.scalars(select(Company.ticker).where(Company.in_universe.is_(True))))
