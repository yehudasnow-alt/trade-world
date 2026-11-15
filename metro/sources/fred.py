"""The economy around the market, from FRED (the St. Louis Fed's public data; the CSV download needs no key).

Why these series: the price of money (rates), the cost of living (inflation), jobs, how nervous lenders and
investors are (credit spread, VIX) and whether the bond market expects a slowdown (the yield curve).
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Optional

import httpx

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SERIES = {
    "DGS10": "תשואת אג\"ח ממשלתי ל-10 שנים",
    "DGS2": "תשואת אג\"ח ממשלתי לשנתיים",
    "DFF": "ריבית הפד",
    "T10Y2Y": "עקום התשואות (10 שנים פחות שנתיים)",
    "CPIAUCSL": "מדד המחירים לצרכן",
    "UNRATE": "שיעור האבטלה",
    "BAMLH0A0HYM2": "מרווח אג\"ח בסיכון גבוה",
    "VIXCLS": "VIX: מדד הפחד",
}


def parse_csv(text: str) -> list[tuple[str, float]]:
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 2 or not r[0][:1].isdigit():
            continue
        try:
            rows.append((r[0], float(r[1])))
        except ValueError:  # "." marks a missing day
            continue
    return rows


def fetch(series: str, years: int = 6, client: Optional[httpx.Client] = None) -> list[tuple[str, float]]:
    start = (date.today() - timedelta(days=365 * years)).isoformat()
    http = client or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "metropolin/1.0"})
    r = http.get(URL, params={"id": series, "cosd": start})
    r.raise_for_status()
    return parse_csv(r.text)


def fetch_all(client: Optional[httpx.Client] = None) -> dict[str, list[tuple[str, float]]]:
    out = {}
    for sid in SERIES:
        try:
            out[sid] = fetch(sid, client=client)
        except Exception:  # noqa: BLE001 - one missing series should not hide the others
            continue
    return out
