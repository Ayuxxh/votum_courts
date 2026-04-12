import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import ddddocr
import fitz
import requests
from bs4 import BeautifulSoup
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from .order_storage import \
    persist_orders_to_storage as _persist_orders_to_storage

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

BASE_URL = "https://efiling.nclat.gov.in"
MAIN_URL = f"{BASE_URL}/mainPage.drt"
CASE_STATUS_URL = f"{BASE_URL}/nclat/case_status.php"
AJAX_URL = f"{BASE_URL}/nclat/ajax/ajax.php"
CAPTCHA_URL = f"{BASE_URL}/nclat/captcha.php"

# Cause List URL
CAUSE_LIST_URL = "https://nclat.nic.in/daily-cause-list"

# /nclat/order_view.php?path=... returns a PDF
ORDERS_VIEW_PREFIX = f"{BASE_URL}/nclat/order_view.php"

DEFAULT_UA = os.getenv(
    "NCLAT_SCRAPER_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
)

# Captcha is simple alpha-numeric in most cases.
CAPTCHA_TOKEN_RE = re.compile(r"[A-Z0-9]+", re.IGNORECASE)

CASE_TYPE_NAME_TO_ID: dict[str, str] = {
    "company appeal(at)": "32",
    "company appeal(at)(ins)": "33",
    "competition appeal(at)": "34",
    "interlocutory application": "35",
    "compensation application": "36",
    "contempt case(at)": "37",
    "review application": "38",
    "restoration application": "39",
    "transfer appeal": "40",
    "transfer original petition (mrtp-at)": "61",
}

# Compact version of the map: all spaces around parentheses stripped.
# Used as fallback when the portal adds/removes spaces around parentheses.
_CASE_TYPE_COMPACT_MAP: dict[str, str] = {
    re.sub(r"\s*\)\s*", ")", re.sub(r"\s*\(\s*", "(", k)): v
    for k, v in CASE_TYPE_NAME_TO_ID.items()
}

# Shorthand / abbreviation → canonical long name.
# Cause-list PDFs use abbreviated type names that won't hit the main map.
CASE_TYPE_SHORTHAND_TO_NAME: dict[str, str] = {
    "ia":                         "interlocutory application",
    "i.a.":                       "interlocutory application",
    "i.a":                        "interlocutory application",
    "ca(at)":                     "company appeal(at)",
    "ca(at)(ins)":                "company appeal(at)(ins)",
    "ca(at)(insolvency)":         "company appeal(at)(ins)",
    "comp.appeal(at)":            "company appeal(at)",
    "comp. appeal(at)":           "company appeal(at)",
    "comp.appeal(at)(ins)":       "company appeal(at)(ins)",
    "comp. appeal(at)(ins)":      "company appeal(at)(ins)",
    "competition appeal(at)":     "competition appeal(at)",  # already in map, kept for completeness
    "contempt(at)":               "contempt case(at)",
    "cont.(at)":                  "contempt case(at)",
    "ra":                         "review application",
    "r.a.":                       "review application",
    "roa":                        "restoration application",
    "ta":                         "transfer appeal",
    "t.a.":                       "transfer appeal",
    "top(mrtp-at)":               "transfer original petition (mrtp-at)",
    "comp.app.(at)":              "company appeal(at)",
    "comp.app.(at)(ins)":         "company appeal(at)(ins)",
}


def _normalize_location(location: str | None) -> str:
    """
    The portal expects schema_name/location as 'delhi' or 'chennai'.
    """
    value = (location or "").strip().lower()
    if not value:
        return "delhi"
    if "chennai" in value:
        return "chennai"
    return "delhi"


def _normalize_case_type(case_type: str | None) -> str | None:
    value = (case_type or "").strip()
    if not value:
        return None
    if value.isdigit():
        return value
    key = re.sub(r"\s+", " ", value.lower())
    result = CASE_TYPE_NAME_TO_ID.get(key)
    if result:
        return result
    # Fallback 1: strip all spaces around parentheses and look up in compact map.
    # Handles portal variants like "Company Appeal ( AT ) ( Ins )".
    key_compact = re.sub(r"\s*\)\s*", ")", re.sub(r"\s*\(\s*", "(", key)).strip()
    result = _CASE_TYPE_COMPACT_MAP.get(key_compact)
    if result:
        return result
    # Fallback 2: abbreviation/shorthand map (cause-list PDFs use short forms).
    # Expand to canonical long name first, then resolve to id.
    long_name = CASE_TYPE_SHORTHAND_TO_NAME.get(key) or CASE_TYPE_SHORTHAND_TO_NAME.get(key_compact)
    if long_name:
        return CASE_TYPE_NAME_TO_ID.get(long_name)
    return None


def _normalize_date(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Fallback: find dd-mm-yyyy-ish
    m = re.search(r"(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})", raw)
    if not m:
        return None
    d, mo, y = m.groups()
    y = f"20{y}" if len(y) == 2 else y
    try:
        return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _split_title(case_title: str | None) -> tuple[str | None, str | None]:
    text = re.sub(r"\s+", " ", (case_title or "")).strip()
    if not text:
        return None, None
    parts = re.split(r"\bVS\b|\bV/S\b|\bV\.S\.?\b", text, flags=re.IGNORECASE)
    if len(parts) >= 2:
        left = parts[0].strip() or None
        right = " ".join(p.strip() for p in parts[1:]).strip() or None
        return left, right
    return text, None


def _reformat_case_no(case_no: str | None) -> str | None:
    """
    Normalizes 'Company Appeal(AT)(Ins) - 69/2026' to 'Company Appeal(AT)(Ins)/69/2026'.
    Replaces common separators like '-' or 'No.' with '/'.
    """
    if not case_no:
        return None
    # Normalize spaces
    text = re.sub(r"\s+", " ", case_no).strip()
    # Replace ' - ' (spaced hyphen separator) or ' No. ' / ' No ' with '/'.
    # Require at least one space on each side of '-' so hyphens embedded in
    # type names like "(MRTP-AT)" are NOT replaced.
    text = re.sub(r"\s+-\s+|\s+No\.?\s+", "/", text, flags=re.IGNORECASE)
    # Ensure only single slashes
    text = re.sub(r"/+", "/", text)
    return text


def _extract_type_name(case_no: str | None) -> str | None:
    """
    Extracts the type part from 'Company Appeal(AT)(Ins)/69/2026'.
    Returns None when the first segment is purely numeric (no type prefix present).
    """
    if not case_no:
        return None
    parts = case_no.split("/")
    if len(parts) > 1:
        raw = parts[0].strip()
        # Pure number means there is no type prefix (e.g. "559/2021")
        if not raw or raw.isdigit():
            return None
        return raw
    return None


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Origin": BASE_URL,
            "Accept": "text/html, */*; q=0.01",
        }
    )
    return session


def _bootstrap_case_status(session: requests.Session) -> None:
    """
    The case status page blocks "direct access"; bootstrap by:
    1) GET main page to obtain srfCaseStatus token.
    2) POST it to /nclat/case_status.php to establish PHPSESSID and allow access.
    """
    resp = session.get(MAIN_URL, timeout=30, headers={"Referer": MAIN_URL})
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    token_el = soup.select_one("form#form_casestatus input[name=srfCaseStatus]")
    token = token_el.get("value") if token_el else None
    if not token:
        raise RuntimeError("NCLAT bootstrap failed: missing srfCaseStatus token.")

    post = session.post(
        CASE_STATUS_URL,
        data={"srfCaseStatus": token},
        timeout=30,
        headers={"Referer": MAIN_URL},
    )
    post.raise_for_status()
    if "Direct access not allowed" in post.text:
        raise RuntimeError("NCLAT bootstrap failed: case_status still blocked.")


def _ensure_ready(session: requests.Session) -> None:
    # We consider having PHPSESSID a good proxy that the bootstrap completed.
    if session.cookies.get("PHPSESSID"):
        return
    _bootstrap_case_status(session)


def _solve_captcha(session: requests.Session) -> str:
    ocr = ddddocr.DdddOcr(show_ad=False)
    # Try a few times; captcha refreshes on each request.
    for attempt in range(8):
        url = f"{CAPTCHA_URL}?_={int(time.time() * 1000)}_{attempt}"
        resp = session.get(url, timeout=30, headers={"Referer": CASE_STATUS_URL})
        resp.raise_for_status()
        raw = (ocr.classification(resp.content) or "").strip()
        token = "".join(CAPTCHA_TOKEN_RE.findall(raw)).strip()
        if token:
            return token
    raise RuntimeError("NCLAT captcha OCR failed after multiple attempts.")


def _ajax_post(session: requests.Session, data: dict[str, Any]) -> str:
    _ensure_ready(session)
    headers = {
        "Referer": CASE_STATUS_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    resp = session.post(AJAX_URL, data=data, timeout=30, headers=headers)
    resp.raise_for_status()
    return resp.text or ""


def _parse_search_results(html: str, location: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table")
    if not table:
        return []

    results: list[dict] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue

        filing_no = tds[1].get_text(" ", strip=True)
        case_no = tds[2].get_text(" ", strip=True)
        title = tds[3].get_text(" ", strip=True)
        reg_date_raw = tds[4].get_text(" ", strip=True)

        if not filing_no or not re.fullmatch(r"\d{10,}", filing_no):
            continue

        pet, res = _split_title(title)
        fmt_case_no = _reformat_case_no(case_no)
        results.append(
            {
                "cino": filing_no,
                "filing_no": filing_no,
                "case_no": fmt_case_no or None,
                "case_title": title or None,
                "pet_name": pet,
                "res_name": res,
                "date_of_decision": None,
                "registration_date": _normalize_date(reg_date_raw) or reg_date_raw or None,
                "type_name": _extract_type_name(fmt_case_no),
                "bench": location,
                "court_name": "NCLAT",
            }
        )
    return results


def _parse_details(html: str, location: str, filing_no: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    tables = soup.find_all("table")

    title_text = None
    if tables:
        title_text = tables[0].get_text(" ", strip=True) or None
    pet_title, res_title = _split_title(title_text)

    filing_date: str | None = None
    registration_date: str | None = None
    case_no: str | None = None
    status: str | None = None
    next_listing_date: str | None = None

    def _norm_key(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").strip().lower()).strip()

    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    petitioners: list[str] = []
    respondents: list[str] = []
    pet_advs: list[str] = []
    res_advs: list[str] = []

    def _collect_from_two_col_table(table, header_substr: str) -> list[str]:
        out: list[str] = []
        if not table:
            return out
        # Filter ths that belong strictly to this table
        ths = [th for th in table.find_all("th") if th.find_parent("table") == table]
        headers = [_clean(th.get_text(" ", strip=True)).lower() for th in ths]
        
        if not any(header_substr in h for h in headers):
            return out
        
        # Filter trs that belong strictly to this table
        trs = [tr for tr in table.find_all("tr") if tr.find_parent("table") == table]
        for tr in trs:
            tds = tr.find_all("td", recursive=False)
            if len(tds) >= 2:
                value = _clean(tds[1].get_text(" ", strip=True))
                if value and value.lower() != "no data":
                    out.append(value)
        return out

    orders: list[dict] = []
    hearings: list[dict] = []
    ia_details: list[dict[str, Any]] = []

    def _header_indexes(headers: list[str]) -> dict[str, int]:
        idx: dict[str, int] = {}
        for i, header in enumerate(headers):
            # "case no" / "case number" maps to ia_number (portal uses "Case No" column for IA case number)
            if "ia" in header or "interlocutory" in header or "application no" in header or "case no" in header or "case number" in header:
                idx.setdefault("ia_number", i)
            if "filing no" in header or "filing number" in header:
                idx.setdefault("ia_filing_no", i)
            if any(k in header for k in ["party", "applicant", "respondent", "petitioner", "filed by"]):
                idx.setdefault("party", i)
            if "filing date" in header or "date of filing" in header:
                idx.setdefault("filing_date", i)
            if "registration date" in header or "reg date" in header:
                idx.setdefault("registration_date", i)
            if any(k in header for k in ["next date", "next hearing", "next listing", "hearing date"]):
                idx.setdefault("next_date", i)
            if "status" in header or "stage" in header:
                idx.setdefault("status", i)
            if "purpose" in header:
                idx.setdefault("purpose", i)
            if "disposal date" in header or "disposed on" in header:
                idx.setdefault("disposal_date", i)
        return idx

    def _extract_ia_rows(table, force_ia: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not table or table.find("table"):
            return rows

        ths = [th for th in table.find_all("th") if th.find_parent("table") == table]
        headers = [_norm_key(_clean(th.get_text(" ", strip=True))) for th in ths]
        indexes = _header_indexes(headers)
        # force_ia=True when called from inside an IA card — skip the signal check entirely
        has_ia_signal = force_ia or bool(
            indexes.get("ia_number") is not None
            or any("interlocutory" in h or h == "ia" or h.startswith("ia ") for h in headers)
        )

        trs = [tr for tr in table.find_all("tr") if tr.find_parent("table") == table]
        for tr in trs:
            if tr.find("th"):
                continue
            tds = tr.find_all("td", recursive=False) or tr.find_all("td")
            if len(tds) < 2:
                continue

            cells = [_clean(td.get_text(" ", strip=True)) for td in tds]
            if not any(cells):
                continue

            ia_idx = indexes.get("ia_number")
            ia_number = cells[ia_idx] if ia_idx is not None and ia_idx < len(cells) else cells[0]
            if not ia_number:
                continue

            # If headers are weak/absent, still accept IA-looking rows from IA sections.
            if not has_ia_signal and not re.search(r"\b(?:I\.?A\.?|IA|INTERLOCUTORY)\b", ia_number, re.IGNORECASE):
                continue

            party_idx = indexes.get("party")
            filing_idx = indexes.get("filing_date")
            reg_idx = indexes.get("registration_date")
            next_idx = indexes.get("next_date")
            status_idx = indexes.get("status")
            purpose_idx = indexes.get("purpose")
            disposal_idx = indexes.get("disposal_date")
            ia_filing_idx = indexes.get("ia_filing_no")

            filing_raw = cells[filing_idx] if filing_idx is not None and filing_idx < len(cells) else None
            reg_raw = cells[reg_idx] if reg_idx is not None and reg_idx < len(cells) else None
            next_raw = cells[next_idx] if next_idx is not None and next_idx < len(cells) else None
            disposal_raw = cells[disposal_idx] if disposal_idx is not None and disposal_idx < len(cells) else None

            rows.append(
                {
                    "ia_number": ia_number,
                    "ia_filing_no": (cells[ia_filing_idx] if ia_filing_idx is not None and ia_filing_idx < len(cells) else None) or None,
                    "party": (cells[party_idx] if party_idx is not None and party_idx < len(cells) else None) or None,
                    "filing_date": _normalize_date(filing_raw) or filing_raw or None,
                    "registration_date": _normalize_date(reg_raw) or reg_raw or None,
                    "next_date": _normalize_date(next_raw) or next_raw or None,
                    "status": (cells[status_idx] if status_idx is not None and status_idx < len(cells) else None) or None,
                    "purpose": (cells[purpose_idx] if purpose_idx is not None and purpose_idx < len(cells) else None) or None,
                    "disposal_date": _normalize_date(disposal_raw) or disposal_raw or None,
                    "raw_row": cells,
                }
            )

        return rows

    # Iterate through each "card" in the accordion structure
    for card in soup.select(".card"):
        header = card.select_one(".card-header")
        if not header:
            continue
        h_text = _clean(header.get_text(" ", strip=True)).lower()
        body = card.select_one(".card-body")
        if not body:
            continue

        if "case detail" in h_text:
            table = body.find("table")
            if table:
                trs = [tr for tr in table.find_all("tr") if tr.find_parent("table") == table]
                for tr in trs:
                    cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"], recursive=False)]
                    if not cells:
                        continue
                    if len(cells) == 2 and _norm_key(cells[0]) == "status":
                        status = cells[1].strip() or None
                        continue
                    i = 0
                    while i + 1 < len(cells):
                        kn = _norm_key(cells[i])
                        v = cells[i + 1].strip()
                        if kn in {"filing no", "filing number"}:
                            filing_no = v or filing_no
                        elif kn == "date of filing":
                            filing_date = _normalize_date(v) or v
                        elif kn in {"case no", "case number"}:
                            case_no = v or case_no
                        elif kn == "registration date":
                            registration_date = _normalize_date(v) or v
                        i += 2
        
        elif "party details" in h_text:
            for pt in body.find_all("table"):
                petitioners.extend(_collect_from_two_col_table(pt, "applicant/appellant"))
                respondents.extend(_collect_from_two_col_table(pt, "respodent"))
        
        elif "legal representative" in h_text:
            for lt in body.find_all("table"):
                pet_advs.extend(_collect_from_two_col_table(lt, "applicant/appellant"))
                # Respondent legal rep table has "Respodent Legal Representative Name"
                ths = [th for th in lt.find_all("th") if th.find_parent("table") == lt]
                headers = [_clean(th.get_text(" ", strip=True)).lower() for th in ths]
                if any("respodent" in h and "legal representative" in h for h in headers):
                    trs = [tr for tr in lt.find_all("tr") if tr.find_parent("table") == lt]
                    for tr in trs:
                        tds = tr.find_all("td", recursive=False)
                        if len(tds) >= 2:
                            value = _clean(tds[1].get_text(" ", strip=True))
                            if value and value.lower() != "no data":
                                res_advs.append(value)
        
        elif "next hearing details" in h_text:
            trs = [tr for tr in body.find_all("tr") if tr.find_parent("table") == tr.find_parent("table", recursive=True)] 
            # Actually simpler: just find all tables and check if they have hearing date
            for t in body.find_all("table"):
                if t.find("table"): continue # Skip outer
                for tr in t.find_all("tr"):
                    cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"], recursive=False)]
                    for i in range(len(cells) - 1):
                        if _norm_key(cells[i]) == "hearing date":
                            next_listing_date = _normalize_date(cells[i + 1]) or cells[i + 1]
                            break
                    if next_listing_date: break
                if next_listing_date: break

        elif "case history" in h_text:
            for table in body.find_all("table"):
                if table.find("table"): continue
                ths = [_clean(th.get_text(" ", strip=True)).lower() for th in table.find_all("th") if th.find_parent("table") == table]
                if "hearing date" in " ".join(ths) and "purpose" in " ".join(ths):
                    trs = [tr for tr in table.find_all("tr") if tr.find_parent("table") == table]
                    for tr in trs:
                        tds = tr.find_all("td", recursive=False)
                        if len(tds) >= 4:
                            hearings.append(
                                {
                                    "hearing_date": _normalize_date(_clean(tds[1].get_text(" ", strip=True)))
                                    or _clean(tds[1].get_text(" ", strip=True))
                                    or None,
                                    "court_no": _clean(tds[2].get_text(" ", strip=True)) or None,
                                    "purpose": _clean(tds[3].get_text(" ", strip=True)) or None,
                                }
                            )

        elif "order history" in h_text:
            for table in body.find_all("table"):
                if table.find("table"): continue
                ths = [_clean(th.get_text(" ", strip=True)).lower() for th in table.find_all("th") if th.find_parent("table") == table]
                if "order date" in " ".join(ths) and "order type" in " ".join(ths):
                    trs = [tr for tr in table.find_all("tr") if tr.find_parent("table") == table]
                    for tr in trs:
                        tds = tr.find_all("td", recursive=False)
                        if len(tds) >= 3:
                            order_date_raw = _clean(tds[1].get_text(" ", strip=True))
                            order_type = _clean(tds[2].get_text(" ", strip=True))
                            href = None
                            link = tr.find("a", href=True)
                            if link:
                                href = link["href"]
                            document_url = urljoin(f"{BASE_URL}/nclat/", href) if href else None
                            orders.append(
                                {
                                    "date": _normalize_date(order_date_raw) or order_date_raw or None,
                                    "description": order_type or "Order",
                                    "document_url": document_url,
                                    "source_document_url": document_url,
                                    "order_type": order_type or None,
                                }
                            )
        elif any(
            k in h_text
            for k in (
                "interlocutory",
                "ia details",
                "i.a.",
                "ia/ma",
                "application details",
                "ia's",
                "ia's/other",
                "other applications",
            )
        ):
            for table in body.find_all("table"):
                ia_details.extend(_extract_ia_rows(table, force_ia=True))

    fmt_case_no = _reformat_case_no(case_no)

    # Deduplicate IA rows while preserving order.
    seen_ias: set[tuple[Any, ...]] = set()
    deduped_ias: list[dict[str, Any]] = []
    for row in ia_details:
        key = (
            (row.get("ia_number") or "").strip().lower(),
            (row.get("party") or "").strip().lower(),
            (row.get("filing_date") or "").strip().lower(),
            (row.get("next_date") or "").strip().lower(),
            (row.get("status") or "").strip().lower(),
        )
        if key in seen_ias:
            continue
        seen_ias.add(key)
        deduped_ias.append(row)

    # Derive first/last listing dates from hearing history
    _today_str = datetime.today().strftime("%Y-%m-%d")
    hearing_dates = sorted(
        h["hearing_date"]
        for h in hearings
        if h.get("hearing_date") and re.match(r"\d{4}-\d{2}-\d{2}", h["hearing_date"])
    )
    first_listing_date = hearing_dates[0] if hearing_dates else None
    # last = most recent hearing on or before today
    past_dates = [d for d in hearing_dates if d <= _today_str]
    last_listing_date = past_dates[-1] if past_dates else None

    # Purpose for next hearing (from hearings list, first future date)
    future_hearings = [
        h for h in hearings
        if h.get("hearing_date") and h["hearing_date"] > _today_str
    ]
    purpose_next = future_hearings[0].get("purpose") if future_hearings else None

    return {
        "cin_no": filing_no,
        "registration_no": None,
        "filing_no": filing_no,
        "case_no": fmt_case_no,
        "type_name": _extract_type_name(fmt_case_no),
        "case_type": _extract_type_name(fmt_case_no),
        "status": status,  # 'Disposed' or 'Pending'
        "registration_date": registration_date,
        "filing_date": filing_date,
        "first_listing_date": first_listing_date,
        "next_listing_date": next_listing_date,
        "last_listing_date": last_listing_date,
        "decision_date": None,
        "court_no": None,
        "disposal_nature": 0 if (status or "").strip() == "Disposed" else 1,
        "purpose_next": purpose_next,
        "bench_name": location,
        "court_name": "NCLAT",
        "pet_name": petitioners or ([pet_title] if pet_title else []),
        "res_name": respondents or ([res_title] if res_title else []),
        "advocates": "\n".join(
            x
            for x in [
                f"Petitioner: {', '.join(sorted({a for a in pet_advs if a}))}" if pet_advs else None,
                f"Respondent: {', '.join(sorted({a for a in res_advs if a}))}" if res_advs else None,
            ]
            if x
        ).strip() or None,
        "petitioner_advocates": sorted({a for a in pet_advs if a}),
        "respondent_advocates": sorted({a for a in res_advs if a}),
        "judges": None,
        "acts": [],
        "orders": orders,
        "history": [],
        "ia_details": deduped_ias,
        "connected_matters": [],
        "application_appeal_matters": [],
        "additional_info": {
            "status": status,
            "case_title": title_text,
            "hearings": hearings,
            "location": location,
        },
        "original_json": {"original_html": html},
    }



@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def nclat_search_by_case_no(
    location: str,
    case_type: str,
    case_no: str,
    case_year: str,
) -> list[dict]:
    """
    Basic details: search by case number.
    Returns rows including filing_no (used for complete details).
    """
    schema = _normalize_location(location)
    ctype = _normalize_case_type(case_type)
    if not ctype:
        raise ValueError("case_type is required (id like '33' or known name).")
    if not (case_no or "").strip():
        raise ValueError("case_no is required.")

    session = _new_session()
    for attempt in range(8):
        print(session)
        captcha = (_solve_captcha(session)).upper() # Captcha only accepts Capital Values
        
        html = _ajax_post(
            session,
            {
                "action": "case_status_search",
                "search_by": "3",
                "case_type": ctype,
                "case_number": str(case_no).strip(),
                "case_year": (case_year or "").strip() or "All",
                "answer": captcha,
                "schema_name": schema,
            },
        )

        if "Captch Value is incorrect" in html:
            continue
        return _parse_search_results(html, location=schema)

    return []


@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def nclat_search_by_free_text(
    location: str,
    search_by: str,
    free_text: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """
    Basic details: free text search.
    The frontend currently passes (search_by, free_text, from_date, to_date).
    We support:
    - search_by in {'party','4'} => By Party
    - search_by in {'advocate','5'} => By Advocate
    - search_by in {'filing','1'} => Filing No (uses free_text as diary_no/filing-like)
    - search_by in {'case_type','2'} => Case Type (uses free_text as case_type id/name; returns possibly many)
    """
    schema = _normalize_location(location)
    sb_raw = (search_by or "").strip().lower()
    if sb_raw in {"4", "party", "by party"}:
        sb = "4"
    elif sb_raw in {"5", "advocate", "by advocate"}:
        sb = "5"
    elif sb_raw in {"1", "filing", "filing no", "filing_no"}:
        sb = "1"
    elif sb_raw in {"2", "case type", "case_type"}:
        sb = "2"
    else:
        raise ValueError("search_by must be one of: 1,2,4,5 (filing/case_type/party/advocate).")

    session = _new_session()
    for attempt in range(8):
        captcha = _solve_captcha(session)
        payload: dict[str, Any] = {
            "action": "case_status_search",
            "search_by": sb,
            "case_year": "All",
            "answer": captcha,
            "schema_name": schema,
        }

        text = (free_text or "").strip()
        if sb == "4":
            payload["select_party"] = "1"
            payload["party_name"] = text
        elif sb == "5":
            payload["advocate_name"] = text
        elif sb == "1":
            payload["diary_no"] = text
        elif sb == "2":
            ctype = _normalize_case_type(text)
            if not ctype:
                raise ValueError("For search_by=2, free_text must be a case_type id/name.")
            payload["case_type"] = ctype
            payload["select_status"] = "all"

        if from_date:
            payload["from_date"] = from_date
        if to_date:
            payload["to_date"] = to_date

        html = _ajax_post(session, payload)
        if "Captch Value is incorrect" in html:
            continue
        return _parse_search_results(html, location=schema)

    return []


@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def nclat_get_details(filing_no: str, bench: str | None = None) -> dict[str, Any] | None:
    """
    Complete details: fetch all details for a filing number.
    """
    if not (filing_no or "").strip():
        return None
    schema = _normalize_location(bench)

    session = _new_session()
    html = _ajax_post(
        session,
        {
            "action": "case_status_case_details",
            "filing_no": filing_no.strip(),
            "schema_name": schema,
        },
    )
    
    if "Direct access not allowed" in html:
        return None
    
    return _parse_details(html, location=schema, filing_no=filing_no.strip())


def _fetch_order_document(order_url: str, referer: str | None):
    session = _new_session()
    _ensure_ready(session)
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    return session.get(order_url, timeout=30, headers=headers)


async def persist_orders_to_storage(
    orders: list[dict] | None,
    case_id: str | None = None,
) -> list[dict] | None:
    """
    Saving orders: download order PDFs (order_view.php) and upload to storage,
    updating each order's `document_url` to a stored URL.
    """
    return await _persist_orders_to_storage(
        orders,
        case_id=case_id,
        fetch_fn=_fetch_order_document,
        base_url=BASE_URL,
        referer=CASE_STATUS_URL,
    )


def nclat_parse_cause_list_pdf(pdf_content: bytes, court_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Parse NCLAT cause-list PDF using coordinates and section headers.
    Handles multi-line entries by grouping text by Y-coordinate proximity.
    """
    doc = fitz.open(stream=pdf_content, filetype="pdf")
    entries = []
    
    current_stage = None
    current_coram = None
    header_found = False
    stop_parsing = False
    current_vc_link = None
    
    for page in doc:
        if stop_parsing:
            break
            
        blocks = page.get_text("dict")["blocks"]
        lines = []
        for b in blocks:
            if b["type"] == 0:
                for l in b["lines"]:
                    x0, y0, x1, y1 = l["bbox"]
                    text = "".join(s["text"] for s in l["spans"]).strip()
                    if text:
                        lines.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text})
        
        lines.sort(key=lambda x: (x["y0"], x["x0"]))
        
        if not lines:
            continue
            
        # Extract Coram for the current page
        page_coram_parts = []
        for l in lines:
            if l["y0"] > 250:
                break
            txt = l["text"]
            if txt.startswith("In the Court of"):
                page_coram_parts.append(txt)
            elif page_coram_parts and txt in ["(Technical)", "(Judicial)", "(Member)", "Member (Technical)", "Member (Judicial)"]:
                page_coram_parts.append(txt)
                
        if page_coram_parts:
            current_coram = " ".join(page_coram_parts)

        # Merge coram with court_name if both exist
        full_court_name = court_name
        if current_coram:
            if court_name and current_coram not in court_name:
                full_court_name = f"{court_name} | {current_coram}"
            else:
                full_court_name = current_coram

        rows_data = []
        current_row_lines = [lines[0]]
        for i in range(1, len(lines)):
            if abs(lines[i]["y0"] - current_row_lines[-1]["y0"]) < 5:
                current_row_lines.append(lines[i])
            else:
                rows_data.append(current_row_lines)
                current_row_lines = [lines[i]]
        rows_data.append(current_row_lines)

        for row in rows_data:
            row_text = " ".join(l["text"] for l in row)
            vc_match = re.search(r'(https?://\S+)', row_text)
            if vc_match:
                current_vc_link = vc_match.group(1)
                continue
            
            if "INSTRUCTIONS FOR" in row_text.upper():
                stop_parsing = True
                break

            if not header_found:
                if "Case No" in row_text or "parties" in row_text.lower():
                    header_found = True
                continue

            first_line = row[0]
            if first_line["x0"] > 100 and first_line["x1"] < 500 and any(kw in row_text for kw in ["For ", "After ", "Admission", "Hearing", "Part Heard"]):
                current_stage = row_text
                continue
            
            # Refined S.No detection
            sno_val = None
            sno_line = None
            
            for l in row:
                m = re.match(r"^(\d{1,3})\.\s*$", l["text"])
                if m:
                    if l["x0"] < 80 or l["x0"] > 350:
                        sno_val = m.group(1)
                        sno_line = l
                        break
            
            if sno_val:
                entries.append({
                    "item_no": sno_val,
                    "case_no": "",
                    "parties": "",
                    "counsel_app": "",
                    "counsel_res": "",
                    "stage": current_stage,
                    "court": full_court_name,
                    "court_name": full_court_name,
                    "coram_name": current_coram,
                    "vc_link" : current_vc_link,
                })
            
            if entries:
                for l in row:
                    if l is sno_line:
                        continue
                    
                    txt = l["text"]
                    if txt.lower() in ["s. no.", "s.no.", "case no.", "case no", "name of the parties", "counsel for", "appellants", "respondents"]:
                        continue
                    
                    if l["x0"] < 165: # Case No column
                        entries[-1]["case_no"] += " " + txt
                    elif l["x0"] < 355: # Parties column
                        entries[-1]["parties"] += " " + txt
                    elif l["x0"] < 465: # Counsel App column
                        entries[-1]["counsel_app"] += " " + txt
                    else: # Counsel Res column
                        entries[-1]["counsel_res"] += " " + txt
                        
    for e in entries:
        for k in ["case_no", "parties", "counsel_app", "counsel_res"]:
            e[k] = re.sub(r"\s+", " ", e[k]).strip()
            
    return entries


@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def nclat_fetch_cause_list(listing_date: datetime, bench: str = "delhi") -> List[Dict[str, Any]]:
    """
    Fetch and parse NCLAT cause list for a given date and bench.
    """

    print(listing_date)
    date_str = listing_date.strftime("%Y-%m-%d")
    print(date_str,type(date_str))
    
    params = {
        "field_final_date_value": date_str,
        "field_final_date_value_1": date_str,
    }
    if bench.lower() == "chennai":
        params["field_court_name_target_id"] = "43"
    else:
        params["field_court_name_target_id"] = "All"

    headers = {
        "User-Agent": DEFAULT_UA
    }
    


    resp = requests.get(CAUSE_LIST_URL, params=params, headers=headers, timeout=30)

    resp.raise_for_status()

    
    soup = BeautifulSoup(resp.text, "html.parser")



    
    table = soup.find("table", {"class": "cols-5"})

    if not table:
        logger.info(f"No cause list found for {date_str} and bench {bench}")
        return []
    
    pdf_links = []
    for row in table.find_all("tr")[1:]: # Skip header
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        
        court_name = cells[1].get_text(strip=True)
        # If bench is delhi, skip Chennai Bench entries
        if bench.lower() == "delhi" and "chennai" in court_name.lower():
            continue
            
        link_tag = cells[4].find("a", href=True)
        if link_tag:
            pdf_url = urljoin(CAUSE_LIST_URL, link_tag["href"])
            pdf_links.append({"url": pdf_url, "court": court_name})
    
    all_entries = []
    for link_info in pdf_links:
        try:
            pdf_resp = requests.get(link_info["url"], headers=headers, timeout=60)
            pdf_resp.raise_for_status()
            entries = nclat_parse_cause_list_pdf(pdf_resp.content, court_name=link_info["court"])
            all_entries.extend(entries)
        except Exception as e:
            logger.error(f"Error parsing PDF {link_info['url']}: {e}")

    return all_entries


def nclat_find_case_in_causelist(listing_date: datetime, case_no: str, bench: str = "delhi") -> List[Dict[str, Any]]:
    """
    Search for a specific case number in the cause list.
    """
    entries = nclat_fetch_cause_list(listing_date, bench)
    if not entries:
        return []
        
    # Strict matching:
    # 1. Full normalized case string containment.
    target_full = re.sub(r"[^A-Z0-9]+", "", case_no.upper())

    # 2. Exact numeric case token (e.g., "69/2026" or "No. 69 of 2026").
    nums = re.findall(r"\d+", case_no)
    target_no_year_pattern = None
    if len(nums) >= 2:
        no = re.escape(nums[-2].lstrip("0") or nums[-2])
        year = re.escape(nums[-1])
        target_no_year_pattern = re.compile(
            rf"(?:NO\.?\s*)?(?<!\d){no}(?!\d)\s*(?:/|OF|-)\s*{year}(?!\d)",
            re.IGNORECASE,
        )

    matched = []
    for e in entries:
        curr_text = e["case_no"].upper()
        curr_norm = re.sub(r"[^A-Z0-9]+", "", curr_text)

        # Check full normalized substring in candidate text.
        if target_full and target_full in curr_norm:
            matched.append(e)
            continue

        # Check exact no/year token match.
        if target_no_year_pattern and target_no_year_pattern.search(curr_text):
            matched.append(e)
            continue

    return matched

def _test_case_type_extraction() -> None:
    """
    Validates that every canonical case-type name in CASE_TYPE_NAME_TO_ID survives the
    full extraction pipeline:
        raw case_no  →  _reformat_case_no  →  _extract_type_name  →  _normalize_case_type

    Also checks common portal variations (spaces around parentheses, uppercase).
    Prints PASS / FAIL per entry.
    """
    failures: list[str] = []

    # Build test inputs: for each map entry, construct a realistic case_no string
    # as the NCLAT portal would return it, in several variants.
    for canonical_name, expected_id in CASE_TYPE_NAME_TO_ID.items():
        # Title-case version of the name (closest to what portal returns)
        portal_name = canonical_name.title()

        variants = [
            f"{portal_name} - 69/2026",           # normal portal format
            f"{portal_name.upper()} - 69/2026",   # all-caps
            f"{portal_name}/69/2026",              # already reformatted
            # With spaces around parentheses (portal sometimes adds them)
            re.sub(r"\(", " ( ", re.sub(r"\)", " ) ", portal_name)).strip() + " - 69/2026",
        ]

        for variant in variants:
            fmt = _reformat_case_no(variant)
            extracted = _extract_type_name(fmt)
            resolved_id = _normalize_case_type(extracted) if extracted else None

            if resolved_id != expected_id:
                failures.append(
                    f"  FAIL  canonical='{canonical_name}'  expected_id={expected_id}\n"
                    f"        input='{variant}'\n"
                    f"        reformatted='{fmt}'  extracted='{extracted}'  resolved_id={resolved_id}"
                )
            else:
                print(f"  PASS  '{canonical_name}' → id={resolved_id}  (input='{variant}')")

    # Also verify pure-numeric case numbers produce no type name
    for bad in ["559/2021", "121/2026", "100/2024"]:
        fmt = _reformat_case_no(bad)
        extracted = _extract_type_name(fmt)
        if extracted is not None:
            failures.append(
                f"  FAIL  numeric case_no '{bad}' should produce type_name=None, got '{extracted}'"
            )
        else:
            print(f"  PASS  numeric case_no '{bad}' → type_name=None")

    if failures:
        print("\n--- FAILURES ---")
        for f in failures:
            print(f)
        raise SystemExit(f"{len(failures)} extraction test(s) failed.")
    else:
        print(f"\nAll {len(CASE_TYPE_NAME_TO_ID) * 4 + 3} extraction checks passed.")


if __name__ == '__main__':
    import json

    # print("=== Case-type extraction tests ===")
    # _test_case_type_extraction()
    # Live search example (uncomment to run against portal):
    # a = nclat_search_by_case_no('delhi', '35', '121', '2026')
    # print(json.dumps(a, indent=4))

    a = nclat_get_details('9910110084442023', 'delhi')
    with open("nclt_output.json", "w") as f:
        json.dump(a, f, indent=4)