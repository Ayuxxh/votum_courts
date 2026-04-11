import hashlib
import json
import logging
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import ddddocr
import fitz
import httpx
import requests
from bs4 import BeautifulSoup
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

try:
    from .order_storage import \
        persist_orders_to_storage as _persist_orders_to_storage
except ImportError:
    from order_storage import \
        persist_orders_to_storage as _persist_orders_to_storage

logger = logging.getLogger(__name__)

BASE_URL = "https://gujarathc-casestatus.nic.in/gujarathc"
CASE_TYPE_URL = f"{BASE_URL}/GetCaseTypeDataOnLoad"
CAPTCHA_URL = f"{BASE_URL}/CaptchaServlet?ct=S&tm={{}}"
DATA_URL = f"{BASE_URL}/GetData"

# Cause List Constants
CAUSE_LIST_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-GB,en;q=0.5",
    "cache-control": "no-cache",
    "content-type": "application/x-www-form-urlencoded",
    "pragma": "no-cache",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Brave";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "sec-gpc": "1",
    "upgrade-insecure-requests": "1",
    "Referer": "https://gujarathc-casestatus.nic.in/gujarathc/",
}

CAUSE_LIST_HOME_URL = "https://gujarathc-casestatus.nic.in/gujarathc/"
CAUSE_LIST_PRINT_URL = "https://gujarathc-casestatus.nic.in/gujarathc/printBoardNew"

CASE_NO_PATTERN = re.compile(r"\b(?!\d{1,2}/\d{1,2}/\d{4})(?:[A-Z0-9.]{1,10}/){1,3}\d{1,7}/\d{4}\b")


def _normalize_case_token(case_no: str) -> str:
    return re.sub(r"\s+", "", (case_no or "").upper())


def _case_tail(case_no: str) -> str:
    token = _normalize_case_token(case_no)
    parts = token.split("/")
    if len(parts) >= 3:
        return "/".join(parts[-3:])
    return token


def _is_vs_line(text: str) -> bool:
    normalized = re.sub(r"\s+", "", (text or "").upper())
    return normalized in {"V/S", "VS", "V.S", "V/S."}


def _clean_pdf_line(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return ""
    if cleaned.startswith("Page ") and " of " in cleaned:
        return ""
    if cleaned in {"IT CELL", "GOTO TOP", "FIRST PAGE"}:
        return ""
    if cleaned.startswith("Created on "):
        return ""
    return cleaned


def _is_party_noise_line(text: str) -> bool:
    upper = (text or "").upper()
    if not upper:
        return True
    if upper in {
        "SNO",
        "CASE DETAILS",
        "NAME OF PARTIES",
        "NAME OF ADVOCATES",
        "REMARKS",
        "FRESH MATTERS",
    }:
        return True
    noise_markers = [
        "GOVERNMENT PLEADER",
        "ADVOCATE",
        "LAW ASSOCIATES",
        "SINGHI & CO",
        "LIST DATE:",
        "CORAM:",
        "COURT:",
        "PAGE ",
    ]
    if any(marker in upper for marker in noise_markers):
        return True
    if re.match(r"^(MR|MRS|MS|SMT|SHRI)\b", upper):
        return True
    return False


def _pick_vc_link_from_page(page: fitz.Page) -> Optional[str]:
    """
    Extract best-effort VC URL from page hyperlinks.
    Prefer known VC providers when multiple links exist.
    """
    candidates: List[str] = []
    preferred: List[str] = []
    for link in page.get_links() or []:
        uri = str(link.get("uri") or "").strip()
        if not uri or not uri.lower().startswith(("http://", "https://")):
            continue
        lowered = uri.lower()
        if any(token in lowered for token in ("zoom.us", "webex", "teams.microsoft", "meet.google")):
            preferred.append(uri)
        else:
            candidates.append(uri)
    if preferred:
        return preferred[0]
    if candidates:
        return candidates[0]
    return None


def _parse_single_cause_list_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    case_lines = entry.get("case_lines") or []
    party_lines = entry.get("party_lines") or []
    advocate_lines = entry.get("advocate_lines") or []
    raw_lines = entry.get("raw_lines") or []

    case_numbers: List[str] = []
    seen = set()
    
    # Try case_lines first
    for line in case_lines:
        for token in CASE_NO_PATTERN.findall(line):
            normalized = _normalize_case_token(token)
            if normalized and normalized not in seen:
                seen.add(normalized)
                case_numbers.append(normalized)
    
    # Fallback to raw_lines if nothing found (handles shifted columns)
    if not case_numbers:
        for line in raw_lines:
             for token in CASE_NO_PATTERN.findall(line):
                normalized = _normalize_case_token(token)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    case_numbers.append(normalized)

    petitioner: Optional[str] = None
    respondent: Optional[str] = None
    petitioner_parts: List[str] = []
    respondent_parts: List[str] = []
    vs_indexes = [idx for idx, line in enumerate(party_lines) if _is_vs_line(line)]
    if vs_indexes:
        first_vs = vs_indexes[0]
        next_vs = vs_indexes[1] if len(vs_indexes) > 1 else len(party_lines)
        petitioner_lines = [
            line
            for line in party_lines[:first_vs]
            if line and not _is_vs_line(line) and not _is_party_noise_line(line)
        ]
        respondent_lines: List[str] = []
        petitioner_norm_set = {
            re.sub(r"[^A-Z0-9]+", "", line.upper()) for line in petitioner_lines if line
        }
        for line in party_lines[first_vs + 1:next_vs]:
            if not line or _is_vs_line(line) or _is_party_noise_line(line):
                continue
            norm_line = re.sub(r"[^A-Z0-9]+", "", line.upper())
            if norm_line and norm_line in petitioner_norm_set:
                break
            respondent_lines.append(line)
        petitioner_parts = [x.strip() for x in petitioner_lines if x and x.strip()]
        respondent_parts = [x.strip() for x in respondent_lines if x and x.strip()]
        petitioner = " ".join(petitioner_parts).strip() or None
        respondent = " ".join(respondent_parts).strip() or None
    else:
        # Fallback: keep party lines as a single list if we can't find a VS delimiter.
        petitioner_parts = [x.strip() for x in party_lines if x and x.strip()]

    case_no = case_numbers[0] if case_numbers else None
    party_names = None
    if petitioner and respondent:
        party_names = f"{petitioner} V/S {respondent}"
    elif petitioner:
        party_names = petitioner
    elif respondent:
        party_names = respondent

    text = "\n".join(raw_lines).strip()
    advocates = "\n".join([x for x in advocate_lines if x]).strip() or None
    entry_hash_src = f"{entry.get('item_no')}|{entry.get('page_no')}|{text}"
    entry_hash = hashlib.sha256(entry_hash_src.encode("utf-8")).hexdigest()

    # New regime:
    # - parties: extracted into an array (petitioner parts + respondent parts when available)
    # - advocates: store the raw advocates text (no name extraction)
    parties = [x for x in (petitioner_parts + respondent_parts) if x]
    
    court_name = entry.get("court_name")
    vc_link = entry.get("vc_link")

    return {
        "item_no": entry.get("item_no"),
        "page_no": entry.get("page_no"),
        "case_no": case_no,
        "case_nos": case_numbers,
        "parties": parties,
        "petitioner": petitioner,
        "respondent": respondent,
        "party_names": party_names,
        "advocates": advocates,
        "court_name": court_name,
        "vc_link": vc_link,
        "text": text,
        "entry_hash": entry_hash,
    }


def parse_cause_list_pdf(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Parse Gujarat HC cause-list PDF using PyMuPDF (fast) with widened column logic.
    """
    entries: List[Dict[str, Any]] = []

    with fitz.open(pdf_path) as doc:
        open_entry: Optional[Dict[str, Any]] = None
        current_court_name: Optional[str] = None
        current_vc_link: Optional[str] = None

        for page_idx in range(doc.page_count):
            page = doc[page_idx]
            page_vc_link = _pick_vc_link_from_page(page)
            if page_vc_link:
                current_vc_link = page_vc_link
            
            # --- Extract Coram / Court Name ---
            raw_page_lines = []
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    x0, y0, _, _ = line.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    line_text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                    if line_text:
                        raw_page_lines.append({"x": float(x0), "y": float(y0), "text": line_text})
            
            raw_page_lines.sort(key=lambda item: (item["y"], item["x"]))

            first_start_y_coram = float("inf")
            for l in raw_page_lines:
                if l["x"] < 75 and l["y"] > 80 and re.fullmatch(r"\d{1,4}\.?", l["text"]):
                    first_start_y_coram = min(first_start_y_coram, l["y"])
            
            page_honourables = []
            page_coram_short = None
            page_court_no = None

            for l in raw_page_lines:
                if l["y"] >= first_start_y_coram:
                    break
                txt = l["text"]
                txt_upper = txt.upper()
                if "HONOURABLE" in txt_upper:
                    page_honourables.append(txt)
                elif txt_upper.startswith("CORAM:"):
                    page_coram_short = txt
                elif "COURT NO :" in txt_upper or "COURT NO:" in txt_upper or txt_upper.startswith("COURT:"):
                    page_court_no = txt
            
            page_court_name_parts = []
            if page_honourables:
                page_court_name_parts.append(" ".join(page_honourables))
            elif page_coram_short:
                page_court_name_parts.append(page_coram_short)
            
            if page_court_no:
                page_court_name_parts.append(page_court_no)
            
            # Only update current_court_name if we found new judge info.
            # If we only found court_no, we assume it's a continuation of the same judge(s).
            if page_honourables or page_coram_short:
                 current_court_name = " | ".join(page_court_name_parts)
            elif page_court_no and not current_court_name:
                 current_court_name = " | ".join(page_court_name_parts)
            # ----------------------------------

            lines: List[Dict[str, Any]] = []

            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    x0, y0, _, _ = line.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    # Ignore headers and footers (y-range 80 to 815)
                    if y0 < 80 or y0 > 815:
                        continue
                    line_text = "".join(span.get("text", "") for span in line.get("spans", []))
                    cleaned = _clean_pdf_line(line_text)
                    if not cleaned:
                        continue
                    lines.append({"x": float(x0), "y": float(y0), "text": cleaned})

            lines.sort(key=lambda item: (item["y"], item["x"]))
            page_tokens = {line["text"].upper() for line in lines}
            has_table_header = (
                "SNO" in page_tokens
                and "CASE DETAILS" in page_tokens
                and "NAME OF PARTIES" in page_tokens
            )
            
            # Find the header y-coordinate if present
            header_y = 0
            if has_table_header:
                for line in lines:
                    if line["text"].upper() in ["SNO", "CASE DETAILS", "NAME OF PARTIES"]:
                         header_y = max(header_y, line["y"])

            # If no header and no entry open, skip page (e.g. cover page, TOC)
            if not has_table_header and not open_entry:
                continue

            starts = [
                line for line in lines 
                if line["x"] < 75 
                and re.fullmatch(r"\d{1,4}\.?", line["text"])
                and (not has_table_header or line["y"] > header_y)
            ]
            starts.sort(key=lambda item: item["y"])

            # Process lines before the first start of this page (if any) as continuation
            first_start_y = starts[0]["y"] if starts else float("inf")
            if open_entry:
                for line in lines:
                    if line["y"] >= first_start_y:
                        break
                    x = line["x"]
                    txt = line["text"]
                    open_entry["raw_lines"].append(txt)
                    # Use widened x-boundaries for case details
                    if 35 <= x < 210:
                        open_entry["case_lines"].append(txt)
                    elif 210 <= x < 355:
                        open_entry["party_lines"].append(txt)
                    elif 355 <= x < 480:
                        open_entry["advocate_lines"].append(txt)
                
                # If we hit a start or the end of page text, close current open entry
                if starts or not lines:
                    entries.append(_parse_single_cause_list_entry(open_entry))
                    open_entry = None

            # Process each new entry starting on this page
            for idx, start in enumerate(starts):
                y_start = start["y"]
                y_end = starts[idx + 1]["y"] if idx + 1 < len(starts) else float("inf")
                segment = {
                    "item_no": start["text"],
                    "page_no": page_idx + 1,
                    "raw_lines": [],
                    "case_lines": [],
                    "party_lines": [],
                    "advocate_lines": [],
                    "court_name": current_court_name,
                    "vc_link": current_vc_link,
                }
                for line in lines:
                    if not (y_start <= line["y"] < y_end):
                        continue
                    x = line["x"]
                    txt = line["text"]
                    segment["raw_lines"].append(txt)
                    # Widened x-boundaries
                    if 35 <= x < 210:
                        segment["case_lines"].append(txt)
                    elif 210 <= x < 355:
                        segment["party_lines"].append(txt)
                    elif 355 <= x < 480:
                        segment["advocate_lines"].append(txt)

                if idx + 1 < len(starts):
                    entries.append(_parse_single_cause_list_entry(segment))
                else:
                    open_entry = segment

        if open_entry:
            entries.append(_parse_single_cause_list_entry(open_entry))

    return [entry for entry in entries if entry.get("case_nos")]


def find_case_entries(pdf_path: str, registration_no: str) -> List[Dict[str, Any]]:
    """
    Find cause-list entries that match a registration/case number.
    Supports matching both prefixed and non-prefixed forms:
    e.g. R/SCA/4937/2022 <-> SCA/4937/2022.
    """
    target_tail = _case_tail(registration_no)
    parsed = parse_cause_list_pdf(pdf_path)
    if not target_tail:
        return parsed

    matched_entries: List[Dict[str, Any]] = []
    for entry in parsed:
        case_nos = entry.get("case_nos") or []
        tails = {_case_tail(case_no) for case_no in case_nos if case_no}
        
        if target_tail in tails:
            matched_entries.append(entry)
    return matched_entries

def fetch_cause_list_pdf_bytes(listing_date: datetime) -> bytes:
    listing_date_str = listing_date.strftime("%d/%m/%Y")
    payload = {
        "coram": "",
        "coramOrder": "",
        "sidecode": "",
        "listflag": "5",
        "courtcode": "0",
        "courtroom": "undefined-undefined-undefined",
        "listingdate": listing_date_str,
        "advocatecodeval": "",
        "advocatenameval": "",
        "ccinval": "",
        "download_token": "",
    }

    with httpx.Client(
        headers=CAUSE_LIST_HEADERS, follow_redirects=True, timeout=120.0, verify=False
    ) as client:
        home_response = client.get(CAUSE_LIST_HOME_URL)
        home_response.raise_for_status()
        token_match = re.search(
            r'name="download_token"\s+value="([^"]*)"', home_response.text
        )
        if token_match:
            payload["download_token"] = token_match.group(1)

        response = client.post(CAUSE_LIST_PRINT_URL, data=payload)
        response.raise_for_status()
        return response.content


class GujaratHCService:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.5",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
            "Connection": "keep-alive",
            "Referer": f"{BASE_URL}/"
        }
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.case_types_map = {}
        self.case_types_path = Path(__file__).with_name("gujarat_case_types.json")

    def _refresh_session(self):
        """Visit main page to establish session/cookies."""
        if self.session.cookies:
            return

        try:
            resp = self.session.get(f"{BASE_URL}/", timeout=30)
            resp.raise_for_status()
            logger.info("Successfully refreshed session/cookies.")
        except Exception as e:
            logger.warning(f"Failed to refresh session: {e}")

    def solve_captcha(self) -> Optional[str]:
        """Download and solve CAPTCHA."""
        try:
            ts = int(time.time() * 1000)
            url = CAPTCHA_URL.format(ts)
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                res = self.ocr.classification(resp.content)
                return res
        except Exception as e:
            logger.error(f"Error solving CAPTCHA: {e}")
        return None

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def get_case_types(self) -> Dict[str, str]:
        """Fetch and cache case types (Name -> Code)."""
        if self.case_types_map:
            return self.case_types_map

        if self.case_types_path.exists():
            try:
                with self.case_types_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    self.case_types_map = data
                    return self.case_types_map
                logger.warning("Case types file is empty or invalid; refetching.")
            except Exception as e:
                logger.warning(f"Failed to read case types file: {e}; refetching.")

        self._refresh_session()
        try:
            resp = self.session.post(CASE_TYPE_URL, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            # Parse case types
            # Structure: finaldata[0].casetypearray[].Civil[].{casetype, casecode}
            groups = data.get('finaldata', [])[0].get('casetypearray', [])
            mapping = {}
            for group in groups:
                for category in ['Civil', 'Criminal', 'OJ']:
                    if category in group:
                        for item in group[category]:
                            name = item.get('casetype')
                            code = item.get('casecode')
                            if name and code:
                                mapping[name] = code
            
            self.case_types_map = mapping
            try:
                with self.case_types_path.open("w", encoding="utf-8") as f:
                    json.dump(self.case_types_map, f, ensure_ascii=True, indent=2, sort_keys=True)
            except Exception as e:
                logger.warning(f"Failed to persist case types file: {e}")
            return mapping
        except Exception as e:
            logger.error(f"Failed to fetch case types: {e}")
            raise

    def _parse_date(self, date_str: str) -> Optional[str]:
        if not date_str or date_str.strip() in ['-', '', 'NA']:
            return None
        value = date_str.strip()

        formats = [
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%d/%m/%y",
            "%d-%m-%y",
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%d.%m.%Y",
            "%d %b %Y",
            "%d %B %Y",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

        match = re.search(r"(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})", value)
        if not match:
            return None

        day, month, year = match.groups()
        year = f"20{year}" if len(year) == 2 else year
        try:
            return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
        except ValueError:
            return None

    def _get_section_records(
        self, data: List[Dict[str, Any]], section_key: str
    ) -> List[Dict[str, Any]]:
        """
        Gujarat HC response array ordering can vary between requests/cases.
        Find records by section key instead of hardcoded indexes.
        """
        for section in data:
            if not isinstance(section, dict):
                continue
            records = section.get(section_key)
            if isinstance(records, list):
                return records
        return []

    def fetch_order_document(self, order_params: Dict[str, str]) -> requests.Response:
        """
        Fetch the actual PDF content for an order.
        order_params should contain ccin_no, order_no, order_date, flag, casedetail, nc.
        """
        url = f"{BASE_URL}/OrderHistoryViewDownload"
        data = {
            'ccin_no': order_params.get('ccin_no'),
            'order_no': order_params.get('order_no'),
            'order_date': order_params.get('order_date'),
            'flag': order_params.get('flag', 'v'),
            'casedetail': order_params.get('casedetail'),
            'nc': order_params.get('nc', '-'),
            'download_token_value_id': str(int(time.time() * 1000))
        }
        return self.session.post(url, data=data, timeout=30)

    def _parse_details(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Parse the JSON response into a normalized format."""
        
        result = {
            "status": None,
            "cin_no": None,
            "registration_no": None,
            "registration_date": None,
            "filing_no": None,
            "filing_date": None,
            "case_type": None,
            "case_no": None,
            "case_year": None,
            "pet_name": [],
            "res_name": [],
            # New regime: advocates stored as raw text (single field), not extracted into arrays.
            "advocates": None,
            "judges": None,
            "court_name": "Gujarat High Court",
            "bench_name": None,
            "district": None,
            "first_hearing_date": None,
            "next_listing_date": None,
            "decision_date": None,
            "disposal_nature": None,
            "orders": [],
            "history": [],
            "connected_matters": [],
            "application_appeal_matters": [],
            "ia_details": [],
            "original_json": {
                "documents": [],
                "objections": [],
                "ia_details": [],
            },
            "raw_data": data  # Keep raw data for debugging/completeness
        }

        # Main Details (ordering can vary: use section lookup)
        main_records = self._get_section_records(data, "maindetails")
        if main_records:
            main = main_records[0]
            result['cin_no'] = main.get('ccin')
            result['status'] = main.get('casestatus')
            result['registration_date'] = self._parse_date(main.get('registration_date'))
            result['filing_no'] = main.get('stampnumber')
            result['filing_date'] = self._parse_date(main.get('presentdate')) # "Presented On"
            result['case_type'] = main.get('casetype')
            result['case_no'] = main.get('casenumber')
            result['case_year'] = main.get('caseyear')
            result['bench_name'] = main.get('benchname')
            result['district'] = main.get('districtname')
            result['judges'] = main.get('judges')
            result['next_listing_date'] = self._parse_date(main.get('listingdate')) # Often listingdate is next date
            result['decision_date'] = self._parse_date(main.get('disposaldate'))
            result['disposal_nature'] = (
                0 if (main.get('casestatus') or '').strip().upper() == 'DISPOSED'
                else 1
            )

            # Format registration number: TYPE/NO/YEAR
            if result['case_type'] and result['case_no'] and result['case_year']:
                result['registration_no'] = f"{result['case_type']}/{result['case_no']}/{result['case_year']}"

        # Litigant (Petitioner)
        for item in self._get_section_records(data, "litigant"):
                name = item.get('litigantname')
                if name:
                    result['pet_name'].append(name)

        # Respondent
        for item in self._get_section_records(data, "respondant"):
                name = item.get('respondantname')
                if name:
                    result['res_name'].append(name)

        # Advocate (store raw text; no name extraction/splitting)
        advocate_records = self._get_section_records(data, "advocate")
        if advocate_records:
            lines: List[str] = []
            for item in advocate_records:
                adv_name = (item.get("advocatename") or "").strip()
                l_type = (item.get("litiganttypecode") or "").strip()  # 1=Pet, 2=Res
                if not adv_name:
                    continue
                if l_type == "1":
                    lines.append(f"Petitioner: {adv_name}")
                elif l_type == "2":
                    lines.append(f"Respondent: {adv_name}")
                else:
                    lines.append(adv_name)
            result["advocates"] = "\n".join(lines).strip() or None

        # Court proceedings (hearing history).
        linked_matters = self._get_section_records(data, "linkedmatterscp")
        for item in linked_matters:
            result["history"].append(
                {
                    "business_date": self._parse_date(item.get("PROCEEDINGDATElmcp")),
                    "hearing_date": self._parse_date(item.get("PROCEEDINGDATElmcp")),
                    "judge": item.get("JUDGESlmcp"),
                    "purpose": item.get("STAGENAMElmcp"),
                    "result": item.get("ACTIONNAMElmcp"),
                }
            )

        # Connected matters.
        for item in self._get_section_records(data, "linkedmatters"):
            result["connected_matters"].append(
                {
                    "case_no": item.get("casedescriptionlm"),
                    "cin_no": item.get("cinolm"),
                    "status": item.get("statusnamelm"),
                    "disposal_date": self._parse_date(item.get("disposaldatelm")),
                    "judge": item.get("JUDGESlm"),
                    "action": item.get("actionname"),
                }
            )

        # Application / Appeal matters.
        for item in self._get_section_records(data, "lpamatters"):
            result["application_appeal_matters"].append(
                {
                    "case_no": item.get("casedescriptionlm"),
                    "cin_no": item.get("cinolm"),
                    "status": item.get("statusnamelm"),
                    "judge": item.get("JUDGESlm"),
                    "disposal_date": self._parse_date(item.get("disposaldatelm")),
                    "action": item.get("actionname"),
                }
            )

        # IA Details (summary from main case API response).
        for item in self._get_section_records(data, "applicationmatters"):
            ia_number = (
                item.get("aino")
                or item.get("ia_no")
                or item.get("ia_number")
                or item.get("IANO")
                or item.get("iaNo")
            )
            description = (
                item.get("descriptionlm")
                or item.get("description")
                or item.get("ia_description")
                or item.get("DESC")
            )
            status = (
                item.get("statusnamelm")
                or item.get("status")
                or item.get("STATUS")
            )
            filing_date = self._parse_date(
                item.get("filingdatelm")
                or item.get("filing_date")
                or item.get("iafilingdate")
                or item.get("iadate")
            )
            next_date = self._parse_date(
                item.get("nextdatelm")
                or item.get("next_date")
                or item.get("nexthearingdate")
            )
            disposal_date = self._parse_date(
                item.get("disposaldatelm")
                or item.get("disposal_date")
            )
            party = (
                item.get("partyname")
                or item.get("litigantname")
                or item.get("party")
            )
            cin_no = item.get("ccin") or item.get("cin_no")

            if not any(
                [ia_number, description, status, filing_date, next_date, disposal_date, party, cin_no]
            ):
                continue

            result["ia_details"].append(
                {
                    "ia_no": ia_number,
                    "ia_number": ia_number,
                    "description": description,
                    "party": party,
                    "filing_date": filing_date,
                    "next_date": next_date,
                    "status": status,
                    "disposal_date": disposal_date,
                    "cin_no": cin_no,
                }
            )

        result["original_json"]["ia_details"] = result["ia_details"]

        # Tagged orders often represent linked tagging context.
        for item in self._get_section_records(data, "taggedorder"):
            result["connected_matters"].append(
                {
                    "source": "taggedorder",
                    "main_case_no": item.get("MAINCASE"),
                    "tagged_case_no": item.get("TAGCASE"),
                    "main_cin_no": item.get("mccin"),
                    "main_order_no": item.get("mno"),
                    "main_order_date": self._parse_date(item.get("mdate")),
                    "tagged_order_no": item.get("tno"),
                    "tagged_order_date": self._parse_date(item.get("tdate")),
                }
            )

        # 11. Order History
        if len(data) > 11 and 'orderhistory' in data[11]:
            for item in data[11]['orderhistory']:
                # Construct logical document_url with parameters
                params = {
                    "ccin_no": item.get('ccinoh'),
                    "order_no": item.get('ordernooh'),
                    "order_date": item.get('orderdate'),
                    "flag": 'v',
                    "casedetail": item.get('descriptionoh'),
                    "nc": item.get('nc', '-')
                }
                # Create a pseudo-URL that contains all necessary info
                param_str = "&".join([f"{k}={v}" for k, v in params.items()])
                doc_url = f"{BASE_URL}/OrderHistoryViewDownload?{param_str}"

                order = {
                    "date": self._parse_date(item.get('orderdate')),
                    "description": f"{item.get('descriptionoh')} | {item.get('judgesoh')} | {item.get('orderdate')}",
                    "judge": item.get('judgesoh'),
                    "order_no": item.get('ordernooh'),
                    "ccin": item.get('ccinoh'),
                    "document_url": doc_url
                }
                result['orders'].append(order)

        return result

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def _parse_search_results(self, html_content: str) -> List[Dict[str, Any]]:
        """Parse the HTML table from SearchLitigant or SearchAdvocate."""
        if not html_content or "NO DATA" in html_content.upper():
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        table = soup.find('table', {'id': 'master'})
        if not table:
            return []

        results = []
        rows = table.find_all('tr')[1:]  # Skip header
        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 2:
                continue

            onclick = row.get('onclick', '')
            # Extract CCIN from onclick='javascript:GoButtonConfirmation("GJHC240125422024");'
            ccin_match = re.search(r'GoButtonConfirmation\("([^"]+)"\)', onclick)
            ccin = ccin_match.group(1) if ccin_match else None

            case_info = {
                "ccin": ccin,
                "case_no_display": cells[0].text.strip(),
                "status": cells[1].text.strip(),
                "party_name": cells[2].text.strip(),
                "hearing_date": self._parse_date(cells[3].text.strip()),
                "litigant_type": cells[4].text.strip() if len(cells) > 4 else None,
                "district": cells[5].text.strip() if len(cells) > 5 else None,
            }
            results.append(case_info)
        return results

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def search_by_party_name(
        self,
        party_name: str,
        litigant_type: str = "0",  # 0=All, 1=Petitioner, 2=Respondent
        from_year: str = "",
        to_year: str = "",
        case_type: str = "select",
        district: str = "select",
        status: str = "select",
        beginning_with: str = "any",  # any, begin, end, exact
        counter: int = 1
    ) -> List[Dict[str, Any]]:
        """Search cases by party name."""
        self._refresh_session()
        time.sleep(1) # Be gentle
        
        if not from_year:
            from_year = str(datetime.now().year - 5)
        if not to_year:
            to_year = str(datetime.now().year)

        url = f"{BASE_URL}/SearchLitigant"
        data = {
            "litigantcode": litigant_type,
            "beginningwith": beginning_with,
            "searchString": party_name,
            "fromyear": from_year,
            "toyear": to_year,
            "counter": str(counter),
            "casetypelt": case_type,
            "district": district,
            "statustype": status
        }
        
        resp = self.session.post(url, data=data, timeout=60)
        resp.raise_for_status()
        
        # Format is HTML###TotalRecords###TotalPages
        parts = resp.text.split("###")
        html_content = parts[0]
        return self._parse_search_results(html_content)

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def get_advocate_codes(self, advocate_name: str, beginning_with: str = "begin") -> List[Dict[str, Any]]:
        """Get list of advocates matching the name."""
        self._refresh_session()
        time.sleep(1) # Be gentle
        url = f"{BASE_URL}/GetAdvocateList"
        data = {
            "beginningwith": beginning_with,
            "searchString": advocate_name.upper(),
            "status": "A",
            "counter": "1"
        }
        resp = self.session.post(url, data=data, timeout=60)
        resp.raise_for_status()
        
        parts = resp.text.split("###")
        html_content = parts[0]
        if not html_content or "<tbody>" not in html_content:
            logger.warning(f"No advocate data found in response: {resp.text[:200]}")
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        rows = table.find_all('tr') if (table := soup.find('table', {'id': 'master'})) else []
        if not rows:
             logger.warning(f"No rows found in advocate table: {html_content[:200]}")
        advocates = []
        for row in rows:
            onclick = row.get('onclick', '')
            if not onclick:
                continue
            # confirmAdvocateWiseCaseList("1138","MR AK TRIVEDI")
            match = re.search(r'confirmAdvocateWiseCaseList\("(\d+)",\s*"([^"]+)"\)', onclick)
            if match:
                advocates.append({
                    "code": match.group(1),
                    "name": match.group(2)
                })
            else:
                logger.debug(f"Failed to match onclick: {onclick}")
        
        if advocates:
             logger.info(f"Found {len(advocates)} advocates matching '{advocate_name}'")
        else:
             logger.warning(f"No advocates matched from {len(rows)} rows for '{advocate_name}'")
        return advocates

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def search_by_advocate(
        self,
        advocate_name: str,
        litigant_type: str = "select",
        from_year: str = "",
        to_year: str = "",
        case_type: str = "select",
        district: str = "select",
        status: str = "A",
        beginning_with: str = "select",
        search_string: str = "",
        counter: int = 1
    ) -> List[Dict[str, Any]]:
        """Search cases by advocate name."""
        advocates = self.get_advocate_codes(advocate_name)
        if not advocates:
            # Try with 'any' if 'begin' fails
            advocates = self.get_advocate_codes(advocate_name, beginning_with="any")
            if not advocates:
                return []

        # For simplicity, we search for the first matching advocate
        # In a real UI, we might want to let the user choose
        adv_code = advocates[0]["code"]
        
        if not from_year:
            from_year = ""
        if not to_year:
            to_year = ""

        time.sleep(1) # Be gentle
        url = f"{BASE_URL}/SearchAdvocate"
        data = {
            "litigantcode": litigant_type,
            "beginningwith": beginning_with,
            "searchString": search_string,
            "fromyear": from_year,
            "toyear": to_year,
            "casetypelt": case_type,
            "district": district,
            "status": status,
            "pfromdate": "",
            "ptodate": "",
            "advcode": adv_code,
            "counter": str(counter)
        }
        
        resp = self.session.post(url, data=data, timeout=60)
        resp.raise_for_status()
        
        parts = resp.text.split("###")
        return self._parse_search_results(parts[0])

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def fetch_case_details(self, case_type_name: str, case_no: str, case_year: str) -> Optional[Dict[str, Any]]:
        """
        Fetch case details by Case Type (Name), Number, and Year.
        e.g. fetch_case_details("SCA", "7966", "2025")
        """
        case_code = self._get_case_code(case_type_name)
        if not case_code:
            logger.error(f"Case type '{case_type_name}' not found.")
            return None

        # Format: R#<case_code>#<case_no>#<case_year>
        case_code = str(case_code).zfill(3)
        genccin = f"R#{case_code}#{case_no}#{case_year}"
        
        return self._fetch_by_genccin(genccin)

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def fetch_case_by_filing_no(self, case_type_name: str, filing_no: str, filing_year: str) -> Optional[Dict[str, Any]]:
        """
        Fetch case details by Filing Number.
        """
        case_code = self._get_case_code(case_type_name)
        if not case_code:
            logger.error(f"Case type '{case_type_name}' not found.")
            return None

        # Format: F#<case_code>#<filing_no>#<filing_year>
        case_code = str(case_code).zfill(3)
        genccin = f"F#{case_code}#{filing_no}#{filing_year}"
        
        return self._fetch_by_genccin(genccin)

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def fetch_case_by_cnr_no(self, cnr_no: str) -> Optional[Dict[str, Any]]:
        """
        Fetch case details by CNR Number.
        """
        # Format: C#<cnr_no>
        genccin = f"C#{cnr_no}"
        
        return self._fetch_by_genccin(genccin)

    def _get_case_code(self, case_type_name: str) -> Optional[str]:
        types = self.get_case_types()
        case_code = types.get(case_type_name.upper()) or types.get(case_type_name)
        if not case_code and case_type_name.isdigit():
            case_code = case_type_name
        return case_code

    def _fetch_by_genccin(self, genccin: str) -> Optional[Dict[str, Any]]:
        """Fetch data from the server using the constructed GENCCIN."""
        self._refresh_session()
        
        # Solve Captcha
        captcha = self.solve_captcha()
        if not captcha:
            raise ValueError("Failed to solve CAPTCHA")

        # Fetch Data
        data = {
            'ccin': genccin,
            'servicecode': '1',
            'challengeString': captcha
        }
        
        resp = self.session.post(DATA_URL, data=data, timeout=30)
        resp.raise_for_status()
        
        json_resp = resp.json()
        
        # Check for errors in response
        if 'finaldata' in json_resp:
            error_msg = json_resp['finaldata'][0].get('ERROR')
            if error_msg:
                if "captcha" in error_msg.lower():
                    logger.warning(f"Server returned error: {error_msg}. Retrying...")
                    raise ValueError(f"Server error: {error_msg}")
                logger.error(f"Server returned error: {error_msg}")
                return None
             
        if 'data' in json_resp:
            parsed = self._parse_details(json_resp['data'])
            # Sometimes the server returns success but missing key fields if it didn't find the case properly
            if parsed and not (parsed.get("registration_no") or parsed.get("filing_no")):
                 logger.warning("Missing registration/filing info for CCIN %s; retrying.", genccin)
                 raise ValueError("Incomplete case details in response")
            return parsed
        
        return None

# Global instance
_service = GujaratHCService()

def get_gujarat_case_details(case_type: str, case_no: str, case_year: str):
    return _service.fetch_case_details(case_type, case_no, case_year)

def get_gujarat_case_details_by_filing_no(case_type: str, filing_no: str, filing_year: str):
    return _service.fetch_case_by_filing_no(case_type, filing_no, filing_year)

def get_gujarat_case_details_by_cnr_no(cnr_no: str):
    return _service.fetch_case_by_cnr_no(cnr_no)

def gujarat_search_by_party_name(party_name: str, **kwargs):
    return _service.search_by_party_name(party_name, **kwargs)

def gujarat_search_by_advocate_name(advocate_name: str, **kwargs):
    return _service.search_by_advocate(advocate_name, **kwargs)

def _fetch_order_document(url: str, referer: str | None = None) -> requests.Response:
    """
    Helper to fetch order documents, handling both regular URLs and the pseudo-URLs
    generated for OrderHistoryViewDownload.
    """
    if "OrderHistoryViewDownload?" in url:
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return _service.fetch_order_document(params)
    
    headers = {}
    if referer:
        headers["Referer"] = referer
    return _service.session.get(url, timeout=30, headers=headers)

async def persist_orders_to_storage(
    orders: list[dict] | None,
    case_id: str | None = None,
) -> list[dict] | None:
    """
    Upload scraped Gujarat HC order documents to storage and update their URLs.
    """
    return await _persist_orders_to_storage(
        orders,
        case_id=case_id,
        fetch_fn=_fetch_order_document,
        referer=f"{BASE_URL}/",
    )



if __name__ == "__main__":
    # Test
    # logging.basicConfig(level=logging.INFO)
    # print(json.dumps(get_gujarat_case_details("FA", "636", "2008"), indent=4))
    # # # Print cause list entries for a case
    # res = find_case_entries("c_g.pdf", "FA/636/2008")

    # print(res)
    # print(res[0].get("text") if res else "No entries found")

    # a = fetch_cause_list_pdf_bytes(datetime.combine(date(2026, 4, 6), datetime.min.time()))

    # print(a)

    a = get_gujarat_case_details('CA', '1526', '2026')

    a = json.dumps(a, indent=4)
    with open('get_gujarat_case_details.json', 'w') as f:
        f.write(a)

    a = find_case_entries(r'D:\Projects\2026\April 26\votum_courts\guj.pdf', 'CA/1526/2026')

    a = json.dumps(a, indent=4)
    with open('guj_find_case_entries.json', 'w') as f:
        f.write(a)
    print(a)