"""Classify filings into events: 8-K item numbers and insider purchases (Form 4)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from xml.etree import ElementTree as ET

ITEMS: dict[str, tuple[str, str]] = {
    "1.01": ("הסכם מהותי חדש", "info"),
    "1.02": ("סיום הסכם מהותי", "watch"),
    "1.03": ("פשיטת רגל או כינוס נכסים", "veto"),
    "1.05": ("אירוע סייבר מהותי", "high"),
    "2.01": ("השלמת רכישה או מכירה", "info"),
    "2.02": ("תוצאות כספיות", "info"),
    "2.03": ("התחייבות פיננסית חדשה", "info"),
    "2.04": ("אירוע שמאיץ פירעון חוב", "high"),
    "2.05": ("עלויות ארגון מחדש", "watch"),
    "2.06": ("ירידת ערך מהותית", "high"),
    "3.01": ("הודעה על מחיקה מהמסחר", "veto"),
    "3.02": ("הנפקת מניות לא רשומה", "watch"),
    "3.03": ("שינוי בזכויות בעלי המניות", "watch"),
    "4.01": ("החלפת רואה החשבון המבקר", "high"),
    "4.02": ("אין להסתמך על דוחות קודמים", "veto"),
    "5.01": ("שינוי שליטה", "high"),
    "5.02": ("שינוי בהנהלה או בדירקטוריון", "watch"),
    "5.03": ("שינוי תקנון או שנת כספים", "info"),
    "5.07": ("הצבעת בעלי מניות", "info"),
    "7.01": ("גילוי לפי Reg FD", "info"),
    "8.01": ("אירוע אחר", "info"),
    "9.01": ("נספחים", "info"),
}
SEVERITY_ORDER = {"veto": 4, "high": 3, "watch": 2, "good": 1, "info": 0}

_EXEC = r"(chief executive officer|chief financial officer|\bceo\b|\bcfo\b|principal financial officer|principal executive officer)"
_LEAVE = r"(resign|resignation|retire|retirement|terminat|depart|step(?:ped|s)? down|will leave|separation)"


def classify_502(text: str) -> tuple[str, str]:
    """Escalate 5.02 when the CEO or CFO leaves (appointments alone stay 'watch')."""
    t = re.sub(r"\s+", " ", text.lower())
    for m in re.finditer(_LEAVE, t):
        window = t[max(0, m.start() - 220): m.end() + 220]
        if re.search(_EXEC, window):
            sudden = "effective immediately" in window or "effective today" in window
            return ("עזיבת מנכ״ל או סמנכ״ל כספים" + (" בתוקף מיידי" if sudden else ""), "veto" if sudden else "high")
    return ITEMS["5.02"]


@dataclass
class EventSpec:
    code: str
    title: str
    severity: str


def classify_8k(items: list[str], item_text: dict[str, str] | None = None) -> list[EventSpec]:
    out = []
    for it in items:
        if it == "9.01":
            continue
        title, sev = ITEMS.get(it, (f"סעיף {it}", "info"))
        code = it
        if it == "5.02" and item_text and it in item_text:
            title, sev = classify_502(item_text[it])
            if sev != "watch":
                code = "5.02-exec"
        out.append(EventSpec(code, title, sev))
    return out


def going_concern(text: str) -> bool:
    return bool(re.search(r"substantial doubt about (?:the company's|our|its) ability to continue as a going concern", text, re.I))


@dataclass
class InsiderTx:
    owner: str
    title: str
    day: date | None
    code: str
    shares: float
    price: float | None
    acquired: bool

    @property
    def value(self) -> float:
        return self.shares * (self.price or 0)


def parse_form4(xml_text: str) -> list[InsiderTx]:
    root = ET.fromstring(xml_text)
    owner = root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or ""
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    title = ""
    if rel is not None:
        title = rel.findtext("officerTitle") or ("Director" if (rel.findtext("isDirector") or "").strip() in ("1", "true") else "")
    out = []
    for tx in root.iter("nonDerivativeTransaction"):
        code = (tx.findtext("transactionCoding/transactionCode") or "").strip()
        sh = tx.findtext("transactionAmounts/transactionShares/value")
        px = tx.findtext("transactionAmounts/transactionPricePerShare/value")
        ad = (tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or "").strip()
        d = tx.findtext("transactionDate/value")
        out.append(InsiderTx(owner, title, date.fromisoformat(d[:10]) if d else None, code,
                             float(sh) if sh else 0.0, float(px) if px else None, ad == "A"))
    return out


def open_market_buys(txs: list[InsiderTx]) -> list[InsiderTx]:
    """Code P = open-market purchase with the insider's own money; grants (A) and option exercises (M) excluded."""
    return [t for t in txs if t.code == "P" and t.acquired]


def form4_xml_name(primary_doc: str) -> str:
    """Submissions list the rendered path ('xslF345X05/form4.xml'); the raw XML sits in the folder root."""
    return primary_doc.split("/")[-1]
