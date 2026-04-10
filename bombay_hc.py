
import hashlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import ddddocr
import fitz
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

BASE_URL = "https://bombayhighcourt.gov.in/bhc"
SEARCH_URL = f"{BASE_URL}/case-status-new"
CASE_TYPE_URL = f"{BASE_URL}/get-case-types-new"
SEARCH_API_URL = f"{BASE_URL}/get-case-status-by-caseno-new"

# Cause List Constants
CAUSE_LIST_FINAL_URL = f"{BASE_URL}/causelistFinal"
CAUSE_LIST_DATA_URL = f"{BASE_URL}/causelist/get-data"

CASE_NO_PATTERN = re.compile(r"\b(?:[A-Z]{1,6}/)?[A-Z]{1,10}/\d{1,7}/\d{4}\b")


def _normalize_case_token(case_no: str) -> str:
    return re.sub(r"\s+", "", (case_no or "").upper())


def _case_tail(case_no: str) -> str:
    token = _normalize_case_token(case_no)
    # Split by / or - or space
    parts = re.split(r"[/-\s]", token)
    if len(parts) >= 3:
        return "/".join(parts[-3:])
    return token


class BombayHCService:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0',
        }
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.case_types_path = Path(__file__).with_name("bombay_case_types.json")
        self._load_case_types()

    def _load_case_types(self):
        self.case_types_map = {} # side -> name -> code
        if self.case_types_path.exists():
            try:
                with open(self.case_types_path, 'r') as f:
                    self.case_types_map = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load case types: {e}")

    def _refresh_session(self) -> Dict[str, str]:
        """Visit search page to get session and tokens."""
        self.session.headers.update({'X-Requested-With': 'XMLHttpRequest'})
        try:
            resp = self.session.get(SEARCH_URL, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')
            
            # CSRF Token
            token_meta = soup.find('meta', {'name': 'csrf-token'})
            token = token_meta['content'] if token_meta else ''
            
            form = soup.find('form', id='getCaseStatusByCaseNo')
            if not form:
                # Try causelist page if search page doesn't have it
                resp = self.session.get(f"{BASE_URL}/causelistFinal", timeout=30)
                soup = BeautifulSoup(resp.content, 'html.parser')
                token_meta = soup.find('meta', {'name': 'csrf-token'})
                token = token_meta['content'] if token_meta else ''
                # For causelistFinal, form_secret is in the forms
                form = soup.find('form', class_='causelist_form')
                if not form:
                     return {'_token': token, 'form_secret': ''}
                secret = form.find('input', {'name': 'form_secret'}).get('value')
                return {'_token': token, 'form_secret': secret}

            if not token:
                token = form.find('input', {'name': '_token'}).get('value')
            secret = form.find('input', {'name': 'form_secret'}).get('value')
            
            return {'_token': token, 'form_secret': secret}
        except Exception as e:
            logger.error(f"Failed to refresh session: {e}")
            raise

    def _get_causelist_tokens(self) -> Dict[str, str]:
        """Visit causelistFinal page to get tokens."""
        try:
            resp = self.session.get(CAUSE_LIST_FINAL_URL, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')

            token_meta = soup.find('meta', {'name': 'csrf-token'})
            token = token_meta['content'] if token_meta else ''

            form = soup.find('form', class_='pdfcauselist_form')
            if not form:
                # Fallback to causelist_form if pdfcauselist_form not found
                form = soup.find('form', class_='causelist_form')

            if not form:
                raise ValueError("Could not find causelist form")

            form_secret = form.find('input', {'name': 'form_secret'}).get('value')
            chkpassphrase = (
                form.find('input', {'name': 'chkpassphrase'}).get('value')
                if form.find('input', {'name': 'chkpassphrase'})
                else ""
            )

            return {
                "_token": token,
                "form_secret": form_secret,
                "chkpassphrase": chkpassphrase,
            }
        except Exception as e:
            logger.error(f"Failed to get causelist tokens: {e}")
            raise

    def fetch_cause_list_pdf_bytes(
        self, listing_date: datetime, bench: str = "B"
    ) -> bytes:
        """
        Fetch cause list PDF for a given date and bench using the new endpoint.
        Bench codes: B=Bombay, N=Nagpur, A=Aurangabad, G=Goa, K=Kolhapur
        """
        tokens = self._get_causelist_tokens()

        headers = {
            "X-CSRF-TOKEN": tokens["_token"],
            "X-Requested-With": "XMLHttpRequest",
            "Referer": CAUSE_LIST_FINAL_URL,
        }

        payload = {
            "_token": tokens["_token"],
            "form_secret": tokens["form_secret"],
            "chkpassphrase": tokens["chkpassphrase"],
            "m_juris": bench,
            "m_causedt": listing_date.strftime("%d-%m-%Y"),
        }

        resp = self.session.post(
            CAUSE_LIST_DATA_URL, data=payload, headers=headers, timeout=60
        )
        resp.raise_for_status()

        json_resp = resp.json()
        if not json_resp.get("status"):
            raise ValueError(
                f"Failed to fetch cause list data: {json_resp.get('message')}"
            )

        html_page = json_resp.get("page")
        if not html_page:
            raise ValueError("No page content returned in cause list response")

        soup = BeautifulSoup(html_page, "html.parser")
        # Find PDF links
        pdf_links = [
            a["href"]
            for a in soup.find_all("a", href=True)
            if ".pdf" in a["href"].lower()
        ]

        if not pdf_links:
            # Check for generic links that might lead to PDFs
            pdf_links = [
                a["href"]
                for a in soup.find_all("a", href=True)
                if "download" in a["href"].lower()
            ]

        if not pdf_links:
            raise ValueError("No PDF links found in cause list result page")

        # Download the first PDF
        pdf_url = pdf_links[0]
        if not pdf_url.startswith("http"):
            pdf_url = urljoin(BASE_URL, pdf_url)

        resp_pdf = self.session.get(pdf_url, timeout=60)
        resp_pdf.raise_for_status()
        return resp_pdf.content

    def parse_cause_list_pdf(self, pdf_path: str) -> List[Dict[str, Any]]:
        """
        Parse Bombay HC cause-list PDF and extract structured entries.
        """
        entries: List[Dict[str, Any]] = []

        with fitz.open(pdf_path) as doc:
            for page_idx in range(doc.page_count):
                page = doc[page_idx]
                text = page.get_text("text")
                
                # Bombay HC PDFs are often structured in blocks or tables.
                # A simple approach is to find all case numbers and their surrounding text.
                lines = text.split('\n')
                current_entry = None
                
                for i, line in enumerate(lines):
                    cleaned_line = line.strip()
                    if not cleaned_line:
                        continue
                    
                    # Detect case numbers like WP/123/2023 or ASWP/123/2023
                    case_matches = CASE_NO_PATTERN.findall(cleaned_line)
                    if case_matches:
                        if current_entry:
                            entries.append(self._finalize_entry(current_entry))
                        
                        current_entry = {
                            "item_no": None, # Will try to extract
                            "page_no": page_idx + 1,
                            "case_nos": [_normalize_case_token(m) for m in case_matches],
                            "raw_lines": [cleaned_line],
                            "text": cleaned_line
                        }
                        
                        # Look for item number in previous lines
                        if i > 0:
                            prev_line = lines[i-1].strip()
                            if re.match(r"^\d{1,4}$", prev_line):
                                current_entry["item_no"] = prev_line
                    elif current_entry:
                        current_entry["raw_lines"].append(cleaned_line)
                        current_entry["text"] += "\n" + cleaned_line
                
                if current_entry:
                    entries.append(self._finalize_entry(current_entry))
                    current_entry = None

        return [e for e in entries if e.get("case_nos")]

    def _finalize_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Finalize an entry with hash and basic party names."""
        text = "\n".join(entry["raw_lines"]).strip()
        entry_hash = hashlib.sha256(f"{entry.get('item_no')}|{entry.get('page_no')}|{text}".encode("utf-8")).hexdigest()
        
        # Simple party name extraction (look for V/S)
        party_names = None
        petitioner = None
        respondent = None
        for line in entry["raw_lines"]:
            if "V/S" in line.upper() or " VS " in line.upper():
                party_names = line
                parts = re.split(r"V/S| VS ", line, flags=re.IGNORECASE)
                if len(parts) >= 2:
                    petitioner = parts[0].strip()
                    respondent = parts[1].strip()
                break
        
        return {
            "item_no": entry.get("item_no"),
            "page_no": entry.get("page_no"),
            "case_no": entry["case_nos"][0] if entry["case_nos"] else None,
            "case_nos": entry["case_nos"],
            "petitioner": petitioner,
            "respondent": respondent,
            "party_names": party_names,
            "text": text,
            "entry_hash": entry_hash,
        }

    def find_case_entries(self, pdf_path: str, registration_no: str) -> List[Dict[str, Any]]:
        """
        Find cause-list entries that match a registration/case number.
        """
        target_tail = _case_tail(registration_no)
        parsed = self.parse_cause_list_pdf(pdf_path)
        if not target_tail:
            return parsed

        matched_entries: List[Dict[str, Any]] = []
        for entry in parsed:
            case_nos = entry.get("case_nos") or []
            tails = {_case_tail(case_no) for case_no in case_nos if case_no}
            if target_tail in tails:
                matched_entries.append(entry)
        return matched_entries

    def _clean_text(self, text: str) -> Optional[str]:
        if not text:
            return None
        # Remove newlines and collapse spaces
        t = re.sub(r'\s+', ' ', text).strip()
        if t in ['—', '-', '', 'NA']:
            return None
        return t

    def _parse_date(self, date_str: str) -> Optional[str]:
        if not date_str or '—' in date_str or '-' == date_str.strip():
            return None
        try:
            # Format usually dd-mm-yyyy or similar
            return datetime.strptime(date_str.strip(), "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError:
            return None

    def _extract_label_value(self, soup, label_text):
        """Helper to find 'Label' ...... 'Value' structure in the HTML"""
        # The HTML uses <b>Label</b> ... value structure often in divs
        # <div class="col-xxl-4"><b>Label</b></div>
        # <div class="col-xxl-8">Value</div>
        
        # Find b tag with label
        label_b = soup.find('b', string=lambda s: s and label_text.lower() in s.lower())
        if label_b:
            # Go up to col-xxl-4 div
            label_col = label_b.find_parent('div')
            if label_col:
                # Find next sibling div (value col)
                value_col = label_col.find_next_sibling('div')
                if value_col:
                    return value_col.get_text(strip=True)
        return None

    def _parse_html_response(self, html_content: str) -> Dict[str, Any]:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        result = {
            "status": None,
            "cnr_no": None,
            "filing_no": None,
            "registration_no": None,
            "registration_date": None,
            "filing_date": None,
            "case_type": None,
            "case_no": None,
            "case_year": None,
            "pet_name": [],
            "res_name": [],
            "advocates": None,
            "judges": None,
            "court_name": "Bombay High Court",
            "bench_name": None,
            "district": None,
            "first_hearing_date": None,
            "next_listing_date": None,
            "decision_date": None,
            "orders": [],
            "history": [],
            "connected_matters": [],
            "application_appeal_matters": [],
            "ia_details": [],
            "original_json": {
                "documents": [],
                "objections": [],
                "ia_details": [],
            }
        }

        # 1. Header Parsing
        header_div = soup.find('div', class_=lambda c: c and 'border-bottom' in c and 'pb-2' in c)
        if header_div:
            text = header_div.get_text(" ", strip=True)
            # Case No. FA/1760/2025
            case_match = re.search(r"Case No\.\s*([\w/]+)", text, re.IGNORECASE)
            if case_match:
                result['registration_no'] = case_match.group(1)
                parts = result['registration_no'].split('/')
                if len(parts) >= 3:
                    result['case_type'] = parts[0]
                    result['case_no'] = parts[1]
                    result['case_year'] = parts[2]

            # CNR No. HCBM010149552025
            cnr_match = re.search(r"CNR No[\.:]?\s*([A-Z0-9]+)", text, re.IGNORECASE)
            if cnr_match:
                result['cnr_no'] = cnr_match.group(1)
            
            # Filing Date
            date_match = re.search(r"filed on\s*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
            if date_match:
                result['filing_date'] = datetime.strptime(date_match.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")

        # 2. Structured fields
        result['filing_no'] = self._clean_text(self._extract_label_value(soup, "Filing Number"))
        result['registration_date'] = self._parse_date(self._extract_label_value(soup, "Registration Date"))
        result['status'] = self._clean_text(self._extract_label_value(soup, "Status"))
        result['next_listing_date'] = self._parse_date(self._extract_label_value(soup, "Next Listing Date"))
        
        # 3. Petitioner / Respondent extraction (multiple p tags)
        def extract_parties(label):
            label_b = soup.find('b', string=lambda s: s and label.lower() in s.lower())
            if label_b:
                label_col = label_b.find_parent('div')
                if label_col:
                    value_col = label_col.find_next_sibling('div')
                    if value_col:
                        ps = value_col.find_all('p')
                        if ps:
                            return [p.get_text(strip=True) for p in ps]
                        return [value_col.get_text(strip=True)]
            return []

        result['pet_name'] = extract_parties("Petitioner")
        result['res_name'] = extract_parties("Respondent")
            
        # Advocates
        pet_advs = extract_parties("Petitioner's Advocate")
        res_advs = extract_parties("Respondent's Advocate")
        adv_lines = []
        if pet_advs:
            adv_lines.append(f"Petitioner: {', '.join(pet_advs)}")
        if res_advs:
            adv_lines.append(f"Respondent: {', '.join(res_advs)}")
        result["advocates"] = "\n".join(adv_lines) if adv_lines else None

        # 4. Tabs
        # History / Proceedings
        history_tab = soup.find('div', id='CaseNoHistory')
        if not history_tab:
             history_tab = soup.find('div', id='CaseNoListing') # They seem to use Listing for history sometimes

        if history_tab:
            rows = history_tab.find_all('tr')
            for row in rows[1:]:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    # Date, Coram, Remark
                    date_text = cols[0].get_text(strip=True)
                    date_val = self._parse_date(date_text)
                    if date_val:
                        result["history"].append({
                            "business_date": date_val,
                            "hearing_date": date_val,
                            "judge": cols[1].get_text(strip=True),
                            "purpose": cols[2].get_text(strip=True)
                        })

        # Orders
        orders_tab = soup.find('div', id='CaseNoOrders')
        if orders_tab:
            rows = orders_tab.find_all('tr')
            for row in rows[1:]:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    coram = cols[1].get_text(strip=True)
                    date_text = cols[2].get_text(strip=True).split('\n')[0].strip() # Date is before link
                    date_val = self._parse_date(date_text)
                    
                    doc_url = None
                    link = row.find('a', href=True)
                    if link:
                        doc_url = link['href']
                        if not doc_url.startswith('http'):
                            doc_url = urljoin(BASE_URL, doc_url)

                    result['orders'].append({
                        "date": date_val,
                        "description": f"Order by {coram}",
                        "judge": coram,
                        "document_url": doc_url
                    })

        # IA Details / Application Cases
        ia_tab = soup.find('div', id='CaseNoApplCases')
        if ia_tab:
            rows = ia_tab.find_all('tr')
            for row in rows[1:]:
                cols = row.find_all('td')
                if len(cols) >= 4:
                    ia_entry = {
                        "ia_no": cols[3].get_text(strip=True),
                        "ia_number": cols[3].get_text(strip=True),
                        "cnr_no": cols[1].get_text(strip=True),
                        "filing_no": cols[2].get_text(strip=True),
                        "description": f"IA Filing: {cols[2].get_text(strip=True)}"
                    }
                    result['ia_details'].append(ia_entry)
        
        result['original_json']['ia_details'] = result['ia_details']

        return result

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def fetch_case_details(
        self,
        case_type_name: str,
        case_no: str,
        case_year: str,
        side: str = "AS",
        stamp: str = "Register"
    ) -> Optional[Dict[str, Any]]:
        
        # 1. Get Tokens
        tokens = self._refresh_session()
        
        case_code = None
        if case_type_name.isdigit():
            case_code = case_type_name
        else:
            # Look up in map
            side_map = self.case_types_map.get(side, self.case_types_map.get("AS", {}))
            case_code = side_map.get(case_type_name.upper())

        if not case_code:
            logger.error(f"Case type '{case_type_name}' not found for side {side}.")
            return None

        # 3. POST Search
        payload = {
            '_token': tokens['_token'],
            'form_secret': tokens['form_secret'],
            'side': '1' if side == "AS" else '2', # 1=AS, 2=OS
            'Stamp': 'R' if stamp == "Register" else 'S', # R=Register, S=Stamp
            'case_type': case_code,
            'case_no': str(case_no),
            'year': str(case_year),
        }
        resp = self.session.post(SEARCH_API_URL, data=payload, timeout=30)
        resp.raise_for_status()
        
        json_resp = resp.json()
        
        if json_resp.get('status') is True:
            html = json_resp.get('page')
            if html:
                return self._parse_html_response(html)
        else:
            logger.warning(f"Search failed for {case_type_name}/{case_no}/{case_year}: {json_resp.get('message')}")
            return None
        
        return None

# Global instance
_service = BombayHCService()

def get_bombay_case_details(
    case_type: str,
    case_no: str,
    case_year: str,
    side: str = "AS",
    stamp: str = "Register"
):
    return _service.fetch_case_details(case_type, case_no, case_year, side=side, stamp=stamp)

def get_bombay_cause_list_pdf(listing_date: datetime, bench: str = "B") -> bytes:
    return _service.fetch_cause_list_pdf_bytes(listing_date, bench=bench)

def parse_bombay_cause_list_pdf(pdf_path: str) -> List[Dict[str, Any]]:
    return _service.parse_cause_list_pdf(pdf_path)

def find_bombay_case_entries(pdf_path: str, registration_no: str) -> List[Dict[str, Any]]:
    return _service.find_case_entries(pdf_path, registration_no)


def _fetch_order_document(url: str, referer: Optional[str] = None) -> requests.Response:
    """Helper to fetch order documents."""
    headers = {}
    if referer:
        headers["Referer"] = referer
    return _service.session.get(url, timeout=30, headers=headers)

async def persist_orders_to_storage(
    orders: Optional[List[dict]],
    case_id: Optional[str] = None,
) -> Optional[List[dict]]:
    """
    Upload scraped Bombay HC order documents to storage and update their URLs.
    """
    return await _persist_orders_to_storage(
        orders,
        case_id=case_id,
        fetch_fn=_fetch_order_document,
        referer=SEARCH_URL,
    )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Test Case Details
    # print("Testing AS...")
    # print(json.dumps(get_bombay_case_details("1", "1", "2025", side="1"), indent=2, default=str))
    
    # Test Cause List
    print("Testing Cause List Fetching...")
    try:
        pdf_bytes = get_bombay_cause_list_pdf(datetime(2026, 2, 9), bench="B")
        with open("bombay_cl_test.pdf", "wb") as f:
            f.write(pdf_bytes)
        print("Successfully fetched cause list PDF.")
        
        entries = parse_bombay_cause_list_pdf("bombay_cl_test.pdf")
        print(f"Found {len(entries)} entries in PDF.")
        if entries:
            print("First entry:", json.dumps(entries[0], indent=2))
    except Exception as e:
        print(f"Cause list test failed: {e}")
