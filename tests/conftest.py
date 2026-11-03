import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own database and data folder."""
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(ROOT / "data" / "sp500.csv", data / "sp500.csv")
    for f in ("relations.json", "themes.json"):
        if (ROOT / "data" / f).exists():
            shutil.copy(ROOT / "data" / f, data / f)
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{data / 'test.db'}")
    monkeypatch.setenv("SEC_USER_AGENT", "Test Runner test@example.org")
    for k in ("ANTHROPIC_API_KEY", "ALPACA_KEY_ID", "ALPACA_SECRET_KEY", "FINNHUB_API_KEY", "IBKR_FLEX_TOKEN",
              "IBKR_FLEX_QUERY_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ACCESS_TOKEN", "FMP_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    from metro import config, db
    config.get_settings.cache_clear()
    db.reset_engine()
    db.init_db()
    yield data
    db.reset_engine()
    config.get_settings.cache_clear()


def fact(start, end, val, filed, form="10-Q", accn=None):
    r = {"end": end, "val": val, "filed": filed, "form": form, "accn": accn or f"0000000000-{filed[2:4]}-{filed[5:7]}{filed[8:10]}"}
    if start:
        r["start"] = start
    return r


@pytest.fixture
def facts_fy_sep():
    """Apple-like 52/53-week fiscal year ending late September."""
    rev = [
        fact("2023-10-01", "2023-12-30", 100.0, "2024-02-02"),
        fact("2023-12-31", "2024-03-30", 90.0, "2024-05-03"),
        fact("2023-12-31", "2024-03-30", 92.0, "2025-05-02"),  # restated as a comparative a year later
        fact("2023-10-01", "2024-03-30", 190.0, "2024-05-03"),
        fact("2024-03-31", "2024-06-29", 85.0, "2024-08-02"),
        fact("2023-10-01", "2024-06-29", 275.0, "2024-08-02"),
        fact("2023-10-01", "2024-09-28", 380.0, "2024-11-01", "10-K"),
        fact("2024-09-29", "2024-12-28", 124.0, "2025-01-31"),
    ]
    old_tag = [fact("2022-09-25", "2022-12-31", 117.0, "2023-02-03")]  # older periods used another tag
    cfo = [  # year-to-date only, as in real 10-Q cash flow statements
        fact("2023-10-01", "2023-12-30", 40.0, "2024-02-02"),
        fact("2023-10-01", "2024-03-30", 62.0, "2024-05-03"),
        fact("2023-10-01", "2024-06-29", 91.0, "2024-08-02"),
        fact("2023-10-01", "2024-09-28", 118.0, "2024-11-01", "10-K"),
    ]
    capex = [
        fact("2023-10-01", "2023-12-30", 2.0, "2024-02-02"),
        fact("2023-10-01", "2024-03-30", 4.5, "2024-05-03"),
        fact("2023-10-01", "2024-06-29", 6.5, "2024-08-02"),
        fact("2023-10-01", "2024-09-28", 9.5, "2024-11-01", "10-K"),
    ]
    eps = [
        fact("2023-10-01", "2023-12-30", 2.18, "2024-02-02"), fact("2023-12-31", "2024-03-30", 1.53, "2024-05-03"),
        fact("2024-03-31", "2024-06-29", 1.40, "2024-08-02"), fact("2023-10-01", "2024-06-29", 5.11, "2024-08-02"),
        fact("2023-10-01", "2024-09-28", 6.08, "2024-11-01", "10-K"),
    ]
    cash = [fact(None, e, v, f) for e, v, f in [("2023-12-30", 40.0, "2024-02-02"), ("2024-03-30", 32.0, "2024-05-03"),
                                                ("2024-06-29", 25.0, "2024-08-02"), ("2024-09-28", 30.0, "2024-11-01")]]
    debt = [fact(None, e, v, f) for e, v, f in [("2024-06-29", 100.0, "2024-08-02"), ("2024-09-28", 97.0, "2024-11-01")]]
    shares = [fact(None, "2024-07-19", 15_222_259_000, "2024-08-02")]
    return {"cik": 320193, "entityName": "Test Co", "facts": {
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": shares}}},
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rev}},
            "SalesRevenueNet": {"units": {"USD": old_tag}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": cfo}},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": capex}},
            "EarningsPerShareDiluted": {"units": {"USD/shares": eps}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": cash}},
            "LongTermDebt": {"units": {"USD": debt}},
        }}}
