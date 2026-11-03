"""SEC EDGAR client: fair-access rate limit, declared User-Agent, disk cache.

Endpoints used (all free, no key):
  data.sec.gov/submissions/CIK##########.json      filing history (+ 8-K item numbers)
  data.sec.gov/api/xbrl/companyfacts/CIK##########.json   every XBRL fact
  www.sec.gov/cgi-bin/browse-edgar?action=getcurrent...   live feed of new filings (Atom)
  www.sec.gov/Archives/edgar/data/<cik>/<acc>/...          documents
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

from ..config import get_settings

ATOM = "{http://www.w3.org/2005/Atom}"


class SecError(RuntimeError):
    pass


class RateLimiter:
    def __init__(self, rps: float):
        self.interval = 1.0 / max(0.1, rps)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                time.sleep(self.next_at - now)
                now = self.next_at
            self.next_at = now + self.interval


@dataclass
class FilingRef:
    accession: str
    form: str
    filed: date
    accepted: datetime | None
    report_date: date | None
    items: list[str]
    primary_doc: str
    cik: int

    @property
    def folder(self) -> str:
        return f"https://www.sec.gov/Archives/edgar/data/{self.cik}/{self.accession.replace('-', '')}"

    @property
    def url(self) -> str:
        return f"{self.folder}/{self.primary_doc}" if self.primary_doc else f"{self.folder}/{self.accession}-index.htm"

    @property
    def index_url(self) -> str:
        return f"{self.folder}/{self.accession}-index.htm"


def _d(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def parse_submissions(js: dict[str, Any]) -> list[FilingRef]:
    """Turn the column-oriented 'recent' block into FilingRef rows (newest first)."""
    rec = js.get("filings", {}).get("recent", {})
    n = len(rec.get("accessionNumber", []))
    cik = int(js.get("cik", 0))

    def col(k: str, i: int, d: Any = "") -> Any:
        arr = rec.get(k) or []
        return arr[i] if i < len(arr) else d

    out = []
    for i in range(n):
        items = [x.strip() for x in str(col("items", i)).split(",") if x.strip()]
        out.append(FilingRef(
            accession=col("accessionNumber", i), form=col("form", i), filed=_d(col("filingDate", i)) or date.min,
            accepted=_dt(col("acceptanceDateTime", i)), report_date=_d(col("reportDate", i)), items=items,
            primary_doc=col("primaryDocument", i), cik=cik,
        ))
    return out


_FEED_TITLE = re.compile(r"^(?P<form>[^ ]+) - (?P<name>.*?) \((?P<cik>\d{10})\)")


def parse_current_feed(xml_text: str) -> list[dict[str, Any]]:
    """Parse the EDGAR 'getcurrent' Atom feed."""
    out = []
    root = ET.fromstring(xml_text)
    for e in root.iter(f"{ATOM}entry"):
        title = (e.findtext(f"{ATOM}title") or "").strip()
        m = _FEED_TITLE.match(title)
        if not m:
            continue
        ident = e.findtext(f"{ATOM}id") or ""
        acc = ident.split("accession-number=")[-1] if "accession-number=" in ident else ""
        link = e.find(f"{ATOM}link")
        summary = BeautifulSoup(e.findtext(f"{ATOM}summary") or "", "lxml").get_text(" ", strip=True)
        items = re.findall(r"Item (\d+\.\d+)", summary)
        out.append({
            "form": m["form"], "name": m["name"], "cik": int(m["cik"]), "accession": acc,
            "url": link.get("href") if link is not None else "", "updated": e.findtext(f"{ATOM}updated") or "",
            "items": items,
        })
    return out


def parse_filing_index(html: str, base: str) -> list[dict[str, Any]]:
    """Documents table of a filing's -index.htm page: seq, description, document, type, size."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.select("table.tableFile"):
        for tr in table.select("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            a = tds[2].find("a")
            href = a.get("href", "") if a else ""
            if href.startswith("/ix?doc="):
                href = href[len("/ix?doc="):]
            url = ("https://www.sec.gov" + href) if href.startswith("/") else (f"{base}/{href}" if href else "")
            out.append({
                "seq": tds[0].get_text(strip=True), "description": tds[1].get_text(" ", strip=True),
                "document": tds[2].get_text(strip=True), "type": tds[3].get_text(strip=True),
                "size": tds[4].get_text(strip=True) if len(tds) > 4 else "", "url": url,
            })
    return out


class SecClient:
    def __init__(self, user_agent: str | None = None, rps: float | None = None, cache_dir: Path | None = None,
                 transport: httpx.BaseTransport | None = None):
        st = get_settings()
        ua = user_agent or st.sec_user_agent
        if not ua or "@" not in ua:
            raise SecError("SEC_USER_AGENT חסר. ה-SEC דורש שם ומייל ליצירת קשר, למשל: \"Dana Levi dana@example.com\".")
        self.limiter = RateLimiter(rps or st.sec_rps)
        self.cache = cache_dir or (st.data_dir / "cache" / "sec")
        self.cache.mkdir(parents=True, exist_ok=True)
        self.http = httpx.Client(headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
                                 timeout=httpx.Timeout(30, connect=10), follow_redirects=True, transport=transport)

    def close(self) -> None:
        self.http.close()

    # --- low level ---
    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode()).hexdigest()
        return self.cache / h[:2] / h

    def get(self, url: str, ttl: float | None = None) -> bytes:
        """ttl=None caches forever (archives are immutable); ttl=0 never caches."""
        p = self._cache_path(url)
        if ttl != 0 and p.exists() and (ttl is None or time.time() - p.stat().st_mtime < ttl):
            return p.read_bytes()
        for attempt in range(5):
            self.limiter.wait()
            try:
                r = self.http.get(url)
            except httpx.TransportError as e:
                if attempt == 4:
                    raise SecError(f"SEC לא זמין: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                if ttl != 0:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(r.content)
                return r.content
            if r.status_code == 404:
                raise SecError(f"לא נמצא ב-SEC: {url}")
            if r.status_code == 403:
                raise SecError("ה-SEC חסם את הבקשה (403). בדוק ש-SEC_USER_AGENT כולל שם ומייל אמיתיים, ושהקצב לא עולה על 10 בשנייה.")
            time.sleep(min(30, 2 ** attempt * (3 if r.status_code == 429 else 1)))
        raise SecError(f"SEC החזיר שגיאה חוזרת עבור {url}")

    def get_json(self, url: str, ttl: float | None = None) -> Any:
        return json.loads(self.get(url, ttl))

    # --- endpoints ---
    def submissions(self, cik: int, ttl: float = 300) -> dict[str, Any]:
        return self.get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ttl)

    def filings(self, cik: int, ttl: float = 300) -> list[FilingRef]:
        return parse_submissions(self.submissions(cik, ttl))

    def companyfacts(self, cik: int, ttl: float = 6 * 3600) -> dict[str, Any]:
        return self.get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", ttl)

    def current(self, form: str, count: int = 100) -> list[dict[str, Any]]:
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&CIK=&type="
               f"{form}&company=&dateb=&owner=include&start=0&count={count}&output=atom")
        return parse_current_feed(self.get(url, ttl=0).decode("utf-8", "replace"))

    def filing_documents(self, ref: FilingRef) -> list[dict[str, Any]]:
        html = self.get(ref.index_url).decode("utf-8", "replace")
        return parse_filing_index(html, ref.folder)

    def text(self, url: str) -> str:
        from .sections import html_to_text
        return html_to_text(self.get(url).decode("utf-8", "replace"))

    def ticker_map(self) -> dict[str, int]:
        js = self.get_json("https://www.sec.gov/files/company_tickers.json", ttl=86400)
        return {v["ticker"].upper(): int(v["cik_str"]) for v in js.values()}
