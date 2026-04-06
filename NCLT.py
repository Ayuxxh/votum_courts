import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from typing import Optional
from urllib import parse

import fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from order_storage import \
    persist_orders_to_storage as _persist_orders_to_storage

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

BASE_URL = 'https://efiling.nclt.gov.in/'
SEARCH_URL = 'https://efiling.nclt.gov.in/caseHistoryoptional.drt'
DETAILS_URL = 'https://efiling.nclt.gov.in/caseHistoryalldetails.drt'
ORDERS_URL = 'https://efiling.nclt.gov.in/ordersview.drt'

NCLT_GOV_URL = 'https://nclt.gov.in'
CAUSE_LIST_URL = f'{NCLT_GOV_URL}/all-couse-list'

session = requests.Session()
session.verify = False
session.headers = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Content-Type': 'application/json',
    'Origin': 'https://efiling.nclt.gov.in',
    'Referer': 'https://efiling.nclt.gov.in/casehistorybeforeloginmenutrue.drt',
    'X-Requested-With': 'XMLHttpRequest',
}

BENCH_MAP = {
    'principal': '10',
    'new delhi': '10',
    'delhi': '10',
    'mumbai': '9',
    'cuttack': '13',
    'ahmedabad': '1',
    'amaravati': '12',
    'chandigarh': '4',
    'kolkata': '8',
    'jaipur': '11',
    'bengaluru': '3',
    'bangalore': '3',
    'chennai': '5',
    'guwahati': '6',
    'hyderabad': '7',
    'kochi': '14',
    'indore': '15',
    'allahabad': '2',
    'prayagraj': '2',
}

CAUSE_LIST_BENCH_MAP = {
    "ahmedabad bench court-i": "88",
    "ahmedabad bench court-ii": "89",
    "allahabad bench court-i": "90",
    "amaravati bench court-i": "91",
    "bengaluru bench court-i": "92",
    "chandigarh bench court-i": "93",
    "chandigarh bench court-ii": "137",
    "chennai bench court-i": "94",
    "chennai bench court-ii": "95",
    "cuttack bench court-i": "96",
    "guwahati bench court-i": "97",
    "hyderabad bench court-i": "98",
    "hyderabad bench court-ii": "99",
    "indore bench court-i": "100",
    "jaipur bench court-i": "101",
    "kochi bench court-i": "102",
    "kolkata bench court ii": "103",
    "kolkata bench court-3": "139",
    "kolkata bench court-i": "104",
    "mumbai bench court-i": "105",
    "mumbai bench court-ii": "106",
    "mumbai bench court-iii": "107",
    "mumbai bench court-iv": "108",
    "mumbai bench court-v": "109",
    "mumbai bench court-vi": "128",
    "new delhi bench court-ii": "110",
    "new delhi bench court-iii": "111",
    "new delhi bench court-iv": "112",
    "new delhi bench court-v": "113",
    "new delhi bench court-vi": "114",
    "principal bench court-i": "115",
    "registrar nclt court-i": "116",
}

CASE_NO_PATTERN = re.compile(
    r"\b(?:CP|IA|MA|CA|TCP|TP|C\.P\.|Appeal)\s*"
    r"(?:\(\s*IB\s*\))?[\s\./-]*\d+"
    r"(?:[\s\./-]*(?:\([A-Z]+\)|[A-Z]{2,8}))?"
    r"[\s\./\-]*(?:of\s+)?\d{4}\b",
    re.IGNORECASE,
)

def _normalize_case_token(case_no: str) -> str:
    return re.sub(r"\s+", "", (case_no or "").upper())

def _case_tail(case_no: str) -> str:
    token = _normalize_case_token(case_no)
    # Extract digits/digits (e.g. 443/2025) or just digits
    match = re.search(r"(\d+)[\D]+(\d{4})", token)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    # Fallback to removing all non-alphanumeric and some common prefixes
    token = re.sub(r"^(?:CP|IA|MA|CA|TCP|TP|C\.P\.|APPEAL)(?:\(IB\))?", "", token)
    return re.sub(r"[^A-Z0-9]", "", token)


def _normalize_order_date(date_str: Optional[str]) -> Optional[str]:
    value = (date_str or "").strip()
    if not value or value.upper() == "NA":
        return None

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

def get_bench_id(bench_name):
    if not bench_name:
        return '0'
    normalized = bench_name.lower().strip()
    # Check for direct match or partial match
    for key, val in BENCH_MAP.items():
        if key in normalized:
            return val
    return '0'

def _canonicalize_bench_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().replace("-", " ").strip())


def get_cause_list_bench_ids(bench_name) -> list[str]:
    if not bench_name:
        return ["All"]
    normalized = _canonicalize_bench_name(str(bench_name))

    # 1) Exact (canonical) match: single, unambiguous ID.
    for key, val in CAUSE_LIST_BENCH_MAP.items():
        if _canonicalize_bench_name(key) == normalized:
            return [val]

    # 2) Bench-only / short input (e.g. "Ahmedabad") -> all matching courts for that bench.
    contains_matches: list[tuple[str, str]] = []
    for key, val in CAUSE_LIST_BENCH_MAP.items():
        if normalized in _canonicalize_bench_name(key):
            contains_matches.append((key, val))
    if contains_matches:
        # Preserve order, dedupe
        seen = set()
        ordered: list[str] = []
        for _, val in contains_matches:
            if val not in seen:
                seen.add(val)
                ordered.append(val)
        return ordered

    # 3) Input includes a full court name plus extra text.
    # Choose the longest matching key to avoid "court-i" matching "court-ii".
    key_matches: list[tuple[int, str]] = []
    for key, val in CAUSE_LIST_BENCH_MAP.items():
        key_canon = _canonicalize_bench_name(key)
        if key_canon in normalized:
            key_matches.append((len(key_canon), val))
    if key_matches:
        max_len = max(length for length, _ in key_matches)
        return [val for length, val in key_matches if length == max_len]

    return ["All"]

def _standardize_result(item):
    """
    Standardize the result from the search list to match the expected output format.
    """
    print(item)
    return {
        'cino': item.get('filing_no'), # Using Case No as CINO for now, as usually CINO is unique but here Case No is prominent
        'date_of_decision': item.get('date_of_filing'),
        'pet_name': item.get('case_title1'),
        'res_name': item.get('case_title2'),
        'type_name': item.get('status'),
        'filing_no': item.get('filing_no'), # Important for fetching details
        'case_no': item.get('case_no'),
        'bench': item.get('bench_location_name')
    }

def solve_math_captcha(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    captcha_sid = soup.find('input', {'name': 'captcha_sid'})['value']
    captcha_token = soup.find('input', {'name': 'captcha_token'})['value']
    
    captcha_text = soup.find('span', {'class': 'field-prefix'}).text
    # Example: "14 + 6 ="
    match = re.search(r'(\d+)\s*([\+\-\*])\s*(\d+)', captcha_text)
    if not match:
        raise ValueError(f"Could not parse math captcha: {captcha_text}")
    
    v1, op, v2 = match.groups()
    if op == '+':
        res = int(v1) + int(v2)
    elif op == '-':
        res = int(v1) - int(v2)
    elif op == '*':
        res = int(v1) * int(v2)
    else:
        raise ValueError(f"Unsupported operator: {op}")
        
    return captcha_sid, captcha_token, str(res)

def fetch_cause_list_pdfs(bench_name: str, date: datetime) -> list[str]:
    """
    Fetch cause list PDFs for a given bench and date.
    Returns a list of URLs to the PDFs.
    """
    bench_ids = get_cause_list_bench_ids(bench_name)
    date_str = date.strftime("%m/%d/%Y")
    pdf_urls: list[str] = []
    seen_urls = set()

    for bench_id in bench_ids:
        # 1. Get initial page to get CAPTCHA
        resp = requests.get(CAUSE_LIST_URL, verify=False)
        resp.raise_for_status()

        try:
            sid, token, solution = solve_math_captcha(resp.text)
        except Exception as e:
            logger.error(f"Failed to solve CAPTCHA for bench_id=%s: %s", bench_id, e)
            continue

        # 2. Submit search
        params = {
            'field_nclt_benches_list_target_id': bench_id,
            'field_cause_date_value': date_str,
            'field_cause_date_value_1': date_str,
            'captcha_sid': sid,
            'captcha_token': token,
            'captcha_response': solution
        }

        resp = requests.get(CAUSE_LIST_URL, params=params, verify=False)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, 'html.parser')
        table = soup.find('table', {'class': 'views-table'})
        if not table:
            continue

        for row in table.find_all('tr'):
            link = row.find('a', href=re.compile(r'\.pdf$'))
            if link and link['href'] not in seen_urls:
                seen_urls.add(link['href'])
                pdf_urls.append(link['href'])

    return pdf_urls

def _clean_pdf_line(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return ""
    if cleaned.startswith("Page ") and " of " in cleaned:
        return ""
    return cleaned

def _parse_single_cause_list_entry(entry: dict, court_name: str = "", vc_link: str = "") -> dict:
    raw_lines = entry.get("raw_lines") or []
    text = "\n".join(raw_lines).strip()
    
    case_numbers: list[str] = []
    seen = set()
    for line in raw_lines:
        for token in CASE_NO_PATTERN.findall(line):
            normalized = _normalize_case_token(token)
            if normalized and normalized not in seen:
                seen.add(normalized)
                case_numbers.append(normalized)
    
    # Prefer non-IA/MA for the main case_no
    main_case_no = None
    if case_numbers:
        # Try to find a 'main' case type like CP or C.P. or Appeal
        mains = [cn for cn in case_numbers if re.search(r"^(CP|C\.P\.|APPEAL)", cn, re.I)]
        if mains:
            main_case_no = mains[0]
        else:
            # Fallback to first non-IA
            non_ias = [cn for cn in case_numbers if not cn.upper().startswith("IA")]
            main_case_no = non_ias[0] if non_ias else case_numbers[0]

    entry_hash_src = f"{entry.get('item_no')}|{entry.get('page_no')}|{text}"
    entry_hash = hashlib.sha256(entry_hash_src.encode("utf-8")).hexdigest()

    return {
        "item_no": entry.get("item_no"),
        "page_no": entry.get("page_no"),
        "case_no": main_case_no,
        "case_nos": case_numbers,
        "text": text,
        "entry_hash": entry_hash,
        "court_name": court_name,
        "vc_link": vc_link,
    }


def extract_coram(lines):
    for line in lines:
        line_upper = line.upper()
        if "CORAM" in line_upper and ":" in line_upper:
            return line.split(":", 1)[1].strip()
    return None

def parse_cause_list_pdf(pdf_path: str) -> list[dict]:
    """
    Parse NCLT cause-list PDF and extract structured entries.
    """
    entries: list[dict] = []
    current_coram = ''
    current_vc_link = ""
    
    with fitz.open(pdf_path) as doc:
        open_entry = None
        
        for page_idx in range(doc.page_count):
            page = doc[page_idx]
            words = page.get_text("words")
            # Sort by y then x
            words.sort(key=lambda w: (w[1], w[0]))
            
            lines = []
            current_y = -1
            current_line = []
            
            for w in words:
                x0, y0, x1, y1, text = w[:5]
                if abs(y0 - current_y) > 5:
                    if current_line:
                        # Sort by x
                        current_line.sort(key=lambda it: it['x'])
                        lines.append(current_line)
                    current_line = []
                    current_y = y0
                current_line.append({'x': x0, 'y': y0, 'text': text})
            
            if current_line:
                current_line.sort(key=lambda it: it['x'])
                lines.append(current_line)

            # Look for table header on this page or previous
            has_header = False
            header_y = -1
            coram_lines_above = []
            coram_found = False
            for line in lines:
                line_text = _clean_pdf_line(" ".join(it['text'] for it in line))
                if not line_text:
                    continue
                
                # Detect VC Link
                url_match = re.search(r"https?://\S+", line_text)
                if url_match:
                    current_vc_link = url_match.group(0)

                line_text_upper = line_text.upper()

                is_coram_line = (
                not coram_found and
                "CORAM" in line_text_upper and
                ":" in line_text_upper
            )

                if is_coram_line:
                    parts = line_text.split(":", 1)
                    current_coram = parts[1].strip() if len(parts) > 1 else line_text.strip()
                    coram_found = True
                    continue




                
                
                # Header detection: looking for common column names
                if any(h in line_text_upper for h in ["CP. NO.", "CP NO.", "CASE NO.", "SECTION/RULE", "CP/CA/IA/MA", "SR. NO", "S.NO"]):
                    has_header = True
                    header_y = line[0]['y']
                    # Don't break yet, might be multiple header lines
                    continue
                
                # Collect potential coram info above the first header
                if not has_header:
                    if any(k in line_text_upper for k in ["CORAM", "COURT", "HON'BLE", "MEMBER"]):
                        if not any(noise in line_text_upper for noise in ["DATE:", "TIME:", "VIDEO CONFERENCING"]):
                            coram_lines_above.append(line_text)

            # if coram_lines_above:
            #     # If we found new coram info, use it. Some PDFs have it on every page.
            #     new_coram = " | ".join(coram_lines_above)
            #     if new_coram != current_coram:
            #         current_coram = new_coram
            
            if not has_header and not open_entry:
                continue
                
            # Filter lines below header
            content_lines = []
            if has_header:
                content_lines = [l for l in lines if l[0]['y'] > header_y]
            else:
                content_lines = lines

            # Detect row starts
            for line in content_lines:
                item_no_candidate = None
                
                line_text = _clean_pdf_line(" ".join(it['text'] for it in line))
                if not line_text:
                    continue

                # Detect VC Link in content lines as well (rare, but just in case)
                url_match = re.search(r"https?://\S+", line_text)
                if url_match:
                    current_vc_link = url_match.group(0)

                line_text_upper = line_text.upper()
                
                # Check for table header (second table on same page or repeated header)
                if any(h in line_text_upper for h in ["CP. NO.", "CP NO.", "CASE NO.", "SECTION/RULE", "CP/CA/IA/MA"]):
                    if open_entry:
                        entries.append(_parse_single_cause_list_entry(open_entry, current_coram, current_vc_link))
                        open_entry = None
                    continue



                

                # # Check for Coram line (might appear between tables)
                # is_coram_line = any(k in line_text_upper for k in ["CORAM", "COURT", "HON'BLE", "MEMBER"]) and \
                #                not any(noise in line_text_upper for noise in ["DATE:", "TIME:", "VIDEO CONFERENCING"])
                
                # if is_coram_line:
                #     if open_entry:
                #         entries.append(_parse_single_cause_list_entry(open_entry, current_coram, current_vc_link))
                #         open_entry = None
                    
                #     # Update current coram
                #     if any(k in line_text_upper for k in ["CORAM", "COURT"]):
                #          current_coram = line_text
                #     else:
                #          current_coram = (current_coram + " | " + line_text) if current_coram else line_text
                #     continue

                # Detection of item number (Sr. No.)
                # In Mumbai PDFs, it's often at the end of the line or in a specific x-range
                # In others, it's at the start.
                
                first_token = line[0]['text'].strip().rstrip('.')
                last_token = line[-1]['text'].strip().rstrip('.')
                
                # Case 1: Item number at the start (x < 110) - expanded range
                if line[0]['x'] < 110 and re.fullmatch(r'\d{1,4}', first_token):
                    item_no_candidate = first_token
                # Case 2: Item number at the end (x > 500)
                elif line[-1]['x'] > 500 and re.fullmatch(r'\d{1,4}', last_token):
                    item_no_candidate = last_token
                
                # Check for case number pattern in the line
                has_case_no = CASE_NO_PATTERN.search(line_text)
                
                if item_no_candidate:
                    # Logic: item number signifies a row start.
                    # 'Grouping is top bottom': if we already have an open entry that was 
                    # started by a case number but has no item number, this item number 
                    # belongs to THAT entry (case number was above item number).
                    if open_entry and open_entry.get("item_no") == "":
                        open_entry["item_no"] = item_no_candidate
                        open_entry["raw_lines"].append(line_text)
                    else:
                        # Otherwise, this item number starts a brand new entry.
                        if open_entry:
                            entries.append(_parse_single_cause_list_entry(open_entry, current_coram, current_vc_link))
                        
                        open_entry = {
                            "item_no": item_no_candidate,
                            "page_no": page_idx + 1,
                            "raw_lines": [line_text]
                        }
                elif has_case_no:
                    # If we find a case number but no item number on this line:
                    if not open_entry:
                        # Start an entry with empty item_no, hoping to find it in the following lines.
                        open_entry = {
                            "item_no": "",
                            "page_no": page_idx + 1,
                            "raw_lines": [line_text]
                        }
                    else:
                        # Continuation or look-ahead case number
                        open_entry["raw_lines"].append(line_text)
                else:
                    if open_entry:
                        open_entry["raw_lines"].append(line_text)

        if open_entry:
            entries.append(_parse_single_cause_list_entry(open_entry, current_coram, current_vc_link))
            
    return [e for e in entries if e.get("case_nos")]

def find_case_entries(pdf_path: str, case_no: str) -> list[dict]:
    """
    Find cause-list entries matching a case number.
    """
    target_tail = _case_tail(case_no)
    parsed = parse_cause_list_pdf(pdf_path)

    print(target_tail)
    if not target_tail:
        return parsed
        
    matched = []
    for entry in parsed:
        tails = [_case_tail(cn) for cn in entry.get("case_nos", [])]

        for case_n in entry.get('case_nos'):
            case = _case_tail(case_n)
            print(case, '|', target_tail)
            print('found')
        if target_tail in tails:
            matched.append(entry)
    return matched

@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
def nclt_search_by_filing_number(bench, filing_number):
    # Note: filing_year is not explicitly used in the filing number search payload of the new site,
    # but the old signature included it. We'll ignore it or check if it's part of filing_number.
    try:
        payload = {
            "wayofselection": "filingnumber",
            "i_bench_id": get_bench_id(bench),
            "filing_no": filing_number
        }
        resp = session.post(SEARCH_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        if 'mainpanellist' in data and data['mainpanellist']:
            return [_standardize_result(item) for item in data['mainpanellist']]
        return []

    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        raise

@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
def nclt_search_by_case_number(bench, case_type, case_number, case_year):
    try:
        payload = {
            "wayofselection": "casenumber",
            "i_bench_id_case_no": get_bench_id(bench),
            "i_case_type_caseno": case_type, # Expecting ID
            "case_no": case_number,
            "i_case_year_caseno": case_year
        }
        resp = session.post(SEARCH_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        if 'mainpanellist' in data and data['mainpanellist']:
            return [_standardize_result(item) for item in data['mainpanellist']]
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        raise

@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
def nclt_search_by_party_name(bench, party_type, party_name, case_year, case_status):
    try:
        payload = {
            "wayofselection": "partyname",
            "i_bench_id_party": get_bench_id(bench),
            "party_type_party": party_type, # 'P' or 'R' or '0'
            "party_name_party": party_name,
            "i_case_year_party": case_year,
            "status_party": case_status, # 'P' or 'D' or '0'
            "i_party_search": "E" # Default to Exact, maybe 'W' for wrap?
        }
        resp = session.post(SEARCH_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        if 'mainpanellist' in data and data['mainpanellist']:
            return [_standardize_result(item) for item in data['mainpanellist']]
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        raise

@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
def nclt_search_by_advocate_name(bench, advocate_name, year):
    try:
        payload = {
            "wayofselection": "advocatename",
            "i_bench_id_lawyer": get_bench_id(bench),
            "party_lawer_name": advocate_name,
            "i_case_year_lawyer": year,
            "bar_council_advocate": "", # Optional
            "i_adv_search": "E"
        }
        resp = session.post(SEARCH_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        if 'mainpanellist' in data and data['mainpanellist']:
            return [_standardize_result(item) for item in data['mainpanellist']]
        return []

    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        raise

@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
def nclt_get_details(bench, filing_no, flag_ia=False):
    # Bench argument is preserved for compatibility but not strictly needed for the detail fetch 
    # as filing_no is unique global identifier in NCLT usually, or at least the API just needs filing_no.
    try:
        params = {
            'filing_no': filing_no,
            'flagIA': 'true' if flag_ia else 'false'
        }
        # The endpoint expects GET
        resp = session.get(DETAILS_URL, params=params)
        resp.raise_for_status()
        data = resp.json() # It returns JSON

        # Parse detailed data
        # Structure is complex, we need to map it to the expected output format
        
        # 1. Registration Info
        reg_list = data.get('isregistered') or []
        reg_info = reg_list[0] if reg_list else {}
        case_no = reg_info.get('case_no')
        reg_date = reg_info.get('regis_date')
        
        # 2. Party Details
        parties = data.get('partydetailslist') or []
        pet_names = []
        res_names = []
        pet_advs = []
        res_advs = []
        
        for p in parties:
            ptype = p.get('party_type', '').strip().upper()
            name = p.get('party_name', '').strip()
            adv = p.get('party_lawer_name', '').strip()
            
            # Check for Petitioner/Applicant (P, A, P1, A1, Petitioner, Applicant)
            if ptype.startswith('P') or ptype.startswith('A') or 'PETITIONER' in ptype or 'APPLICANT' in ptype:
                if name: pet_names.append(name)
                if adv and adv.upper() != 'NA': 
                    # Split multiple advocates if comma separated
                    for a in adv.split(','):
                        a = a.strip()
                        if a: pet_advs.append(a)
            
            # Check for Respondent (R, R1, Respondent, D, D1, Defendant)
            elif ptype.startswith('R') or ptype.startswith('D') or 'RESPONDENT' in ptype or 'DEFENDANT' in ptype:
                if name: res_names.append(name)
                if adv and adv.upper() != 'NA':
                    for a in adv.split(','):
                        a = a.strip()
                        if a: res_advs.append(a)
        
        # 3. Status
        final_status_list = data.get('allfinalstatuslist') or []
        final_status = final_status_list[0] if final_status_list else {}
        case_status = final_status.get('current_status')
        listing_date = final_status.get('listing_date')
        next_listing_date = final_status.get('case_next_list_date') or final_status.get('nextlisting_step')
        last_listing_date = final_status.get('listing_date_step') or listing_date
        
        # 4. Orders
        orders = []
        proceedings = data.get('allproceedingdtls') or []
        for proc in proceedings:
            order_path = proc.get('encPath') # This is the 'path' param for ordersview.drt
            # Construct URL: https://efiling.nclt.gov.in/ordersview.drt?path={encPath}
            order_url = f"{ORDERS_URL}?path={parse.quote(order_path)}" if order_path and order_path != 'NA' else None
            parsed_order_date = _normalize_order_date(proc.get('order_upload_date'))
            parsed_listing_date = _normalize_order_date(proc.get('listing_date'))
            
            orders.append({
                "date": parsed_order_date or parsed_listing_date or proc.get('order_upload_date') or proc.get('listing_date'),
                "description": f"Listing: {proc.get('listing_date')} | Purpose: {proc.get('purpose')} | Action: {proc.get('today_action')}",
                "document_url": order_url,
                "source_document_url": order_url,
                "listing_date": proc.get('listing_date'),
                "upload_date": proc.get('order_upload_date')
            })
            
        # 5. Connected Matters (using IA/MA list or similar)
        connected = []
        ia_details = []
        ias = data.get('mainFilnowithIaNoList') or []
        for ia in ias:
            ia_entry = {
                "filing_no": ia.get('filing_no'),
                "case_no": ia.get('case_no'),
                "title": f"{ia.get('case_title1')} VS {ia.get('case_title2')}",
                "status": ia.get('status')
            }
            connected.append(ia_entry)
            
            ia_details.append({
                "ia_no": ia.get('case_no'),
                "ia_number": ia.get('case_no'),
                "description": f"{ia.get('case_title1')} VS {ia.get('case_title2')}",
                "party": ia.get('case_title1'),
                "filing_date": _normalize_order_date(ia.get('date_of_filing')),
                "next_date": _normalize_order_date(ia.get('next_list_date') or ia.get('listing_date')),
                "status": ia.get('status'),
                "disposal_date": _normalize_order_date(ia.get('disposal_date')),
                "cin_no": ia.get('filing_no'),
                "purpose": ia.get('purpose') or ia.get('next_listing_purpose') or ia.get('last_purpose_step'),
            })
            
        # Construct result
        res = {
            "cin_no": filing_no, # Using filing_no as cin_no for now
            "registration_no": case_no, # or case_no?
            "filing_no": filing_no,
            "case_no": case_no,
            "registration_date": reg_date,
            "filing_date": final_status.get('date_of_filing'),
            "first_listing_date": listing_date,
            "next_listing_date": _normalize_order_date(next_listing_date),
            "last_listing_date": _normalize_order_date(last_listing_date),
            "decision_date": _normalize_order_date(final_status.get('disposal_date')),
            "court_no": final_status.get('court_no'),
            "disposal_nature": final_status.get('action_type'),
            "purpose_next": final_status.get('next_listing_purpose') or final_status.get('last_purpose_step'),
            "case_type": final_status.get('case_type'),
            "pet_name": pet_names,
            "res_name": res_names,
            "advocates": "\n".join(
                [
                    x
                    for x in [
                        (
                            f"Petitioner: {', '.join(sorted({a for a in pet_advs if a}))}"
                            if pet_advs
                            else None
                        ),
                        (
                            f"Respondent: {', '.join(sorted({a for a in res_advs if a}))}"
                            if res_advs
                            else None
                        ),
                    ]
                    if x
                ]
            ).strip()
            or None,
            "judges": None,
            "bench_name": bench,
            "court_name": final_status.get('bench_nature_descr'),
            "history": [], # Detailed history is in orders/proceedings
            "acts": None,
            "orders": orders,
            "ia_details": ia_details,
            "additional_info": {
                "case_status": case_status,
                "party_name": f"{', '.join(pet_names)} VS {', '.join(res_names)}",
                "listing_history": proceedings,
                "ia_ma": ias,
                "connected_matters": connected,
            },
            "original_json": data,
        }
            
        return res

    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        raise

def _fetch_order_document(order_url: str, referer: str | None):
    headers = session.headers.copy()
    if referer:
        headers["Referer"] = referer
    return session.get(order_url, timeout=30, headers=headers)

async def persist_orders_to_storage(
    orders: list[dict] | None,
    case_id: str | None = None,
) -> list[dict] | None:
    return await _persist_orders_to_storage(
        orders,
        case_id=case_id,
        fetch_fn=_fetch_order_document,
        base_url=BASE_URL,
        referer=f"{BASE_URL}caseHistoryalldetails.drt",
    )

if __name__ == '__main__':
    # Test code
    # NOTE: You need valid IDs for bench and case types for this to work.
    # Case Type 16 is "Company Petition IB(IBC)"
    
    a = ('ahmedabad', '4', '1', '2025')


    a = fetch_cause_list_pdfs('kolkata', datetime.strptime('06/04/2026', "%d/%m/%Y"))
    a = json.dumps(a, indent=4)
    with open('nclt_fetch_cause_list.json', 'w') as f:
        f.write(a)

    
    a = json.dumps(nclt_get_details('kolkata', '1908134021072024'), indent=4)# Use a valid filing number found from search
    with open('nclt_get_details.json', 'w') as f:
        f.write(a)

    a = fetch_cause_list_pdfs('111', datetime.strptime('02/04/2026', "%d/%m/%Y"))

    a = find_case_entries(r'D:\Projects\2026\April 26\votum_courts\kol.pdf', '301/2024') ## case/year format only!!
    a = json.dumps(a, indent=4)
    with open('find_case_entries.json', 'w') as f:
        f.write(a)
    # print(a)
    # pass

    a = parse_cause_list_pdf(r'D:\Projects\2026\April 26\votum_courts\kol.pdf')
    a = json.dumps(a, indent=4)
    with open('parse_cause_list_pdf.json', 'w') as f:
        f.write(a)
    # print(a)
    # pass
