import logging
import os
import re
from datetime import datetime, timedelta
from hashlib import sha256
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

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

BASE_URL = "https://delhihighcourt.nic.in"
SEARCH_URL = f"{BASE_URL}/app/get-case-type-status"
VALIDATE_CAPTCHA_URL = f"{BASE_URL}/app/validateCaptcha"
CAUSE_LIST_URL = f"{BASE_URL}/web/cause-lists/cause-list"

CAUSE_LIST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

DELHI_CASE_NO_PATTERN = re.compile(
    r"\b[A-Z][A-Z0-9()./&-]{0,40}\s*\d{1,7}/\d{4}\b"
)


def parse_listing_date(date_str: Optional[str]) -> datetime:
    if not date_str:
        return datetime.now() + timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError("Invalid listing_date format. Use YYYY-MM-DD, DD/MM/YYYY, or DD-MM-YYYY.")


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


def _normalize_case_token(case_no: str) -> str:
    token = re.sub(r"\s+", " ", (case_no or "").upper()).strip()
    token = token.replace(" /", "/").replace("/ ", "/")
    return token


def _case_tail(case_no: str) -> str:
    token = _normalize_case_token(case_no)
    match = re.search(r"(\d{1,7}/\d{4})", token)
    if match:
        return match.group(1)
    compact = re.sub(r"[^A-Z0-9/]+", "", token)
    parts = compact.split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return compact


def _clean_pdf_line(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return ""
    if cleaned.startswith("Page ") and " of " in cleaned:
        return ""
    if cleaned.startswith("Created on "):
        return ""
    if cleaned in {"IT CELL", "GOTO TOP", "FIRST PAGE"}:
        return ""
    return cleaned


def _extract_case_tokens(lines: List[str]) -> List[str]:
    seen = set()
    values: List[str] = []
    for line in lines:
        for token in DELHI_CASE_NO_PATTERN.findall((line or "").upper()):
            normalized = _normalize_case_token(token)
            if normalized and normalized not in seen:
                seen.add(normalized)
                values.append(normalized)
    return values


def _parse_single_cause_list_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    raw_lines = entry.get("raw_lines") or []
    case_numbers = _extract_case_tokens(raw_lines)
    case_no = case_numbers[0] if case_numbers else None
    text = "\n".join(raw_lines).strip()
    entry_hash_src = f"{entry.get('item_no')}|{entry.get('page_no')}|{text}"
    entry_hash = sha256(entry_hash_src.encode("utf-8")).hexdigest()
    # return {
    #     "item_no": entry.get("item_no"),
    #     "page_no": entry.get("page_no"),
    #     "case_no": case_no,
    #     "case_nos": case_numbers,
    #     "text": text,
    #     "entry_hash": entry_hash,
    # }
    return {
    "item_no": entry.get("item_no"),
    "page_no": entry.get("page_no"),
    "case_no": case_no,
    "case_nos":[
        case_numbers
    ],
    "parties":[

    ],
    "petitioner":"",
    "respondent":"",
    "party_names":"",
    "advocates":"",
    "court_name":entry.get('coram'),
    "vc_link":entry.get('vc_link'),
    "text":text,
    "entry_hash":entry_hash
}



def parse_cause_list_pdf(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Parse Delhi HC cause-list PDF and keep only case-number entries.
    """
    entries: List[Dict[str, Any]] = []
    with fitz.open(pdf_path) as doc:
        open_entry: Optional[Dict[str, Any]] = None


        vc_link = ''
        coram = ''
        for page_idx in range(doc.page_count):
            page = doc[page_idx]
            page_txt = page.get_text()


            domain = "dhcvirtualcourt.webex.com"

            pattern = rf"https?://[^\s]*{re.escape(domain)}[^\s]*"

            match = re.search(pattern, page_txt)

            if match:
                match = match.group(0)


            pattern = r"(HON\S*BLE\s+(?:MR|MS|DR)\.?\s+JUSTICE\s+[A-Z\s\.]+?)(?=\s+HON|\n|$)"

            matches = re.findall(pattern, page_txt)

            # clean up formatting
            judges = [" ".join(m.split()) for m in matches]



            for judge in judges:
                coram = coram + ', ' + judge



            if "HON'BLE" in page_txt:
                if match:
                    vc_link = match
        
            lines: List[Dict[str, Any]] = []

            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    x0, y0, _, _ = line.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    if y0 < 100 or y0 > 780:
                        continue
                    line_text = "".join(span.get("text", "") for span in line.get("spans", []))
                    cleaned = _clean_pdf_line(line_text)

                    if not cleaned:
                        continue
                    lines.append({"x": float(x0), "y": float(y0), "text": cleaned})

            lines.sort(key=lambda item: (item["y"], item["x"]))
            starts = [
                line
                for line in lines
                if line["x"] < 75 and re.fullmatch(r"\d{1,4}", line["text"])
            ]
            starts.sort(key=lambda item: item["y"])

            if not starts:
                if open_entry:
                    open_entry["raw_lines"].extend(line["text"] for line in lines)
                continue

            first_start_y = starts[0]["y"]
            if open_entry:
                for line in lines:
                    if line["y"] >= first_start_y:
                        continue
                    open_entry["raw_lines"].append(line["text"])
                entries.append(_parse_single_cause_list_entry(open_entry))
                open_entry = None

            for idx, start in enumerate(starts):
                y_start = start["y"]
                y_end = starts[idx + 1]["y"] if idx + 1 < len(starts) else float("inf")
                segment = {
                    "item_no": start["text"],
                    "page_no": page_idx + 1,
                    "raw_lines": [],
                    "vc_link" : vc_link,
                    "coram" :coram ,
                }
                for line in lines:
                    if y_start <= line["y"] < y_end:
                        segment["raw_lines"].append(line["text"])

                if idx + 1 < len(starts):
                    entries.append(_parse_single_cause_list_entry(segment))
                else:
                    open_entry = segment

            
   

        if open_entry:
            entries.append(_parse_single_cause_list_entry(open_entry))
    
    return [entry for entry in entries if entry.get("case_nos")]


def find_case_entries(pdf_path: str, case_no: str) -> List[Dict[str, Any]]:
    """
    Find Delhi HC cause-list entries that match a case number.
    Matching is based on case tail: <number>/<year>.
    """
    target_tail = _case_tail(case_no)
    parsed = parse_cause_list_pdf(pdf_path)

    
    if not target_tail:
        return parsed

    matched_entries: List[Dict[str, Any]] = []
    for entry in parsed:
        case_nos = entry.get("case_no") or []
        # tails = {_case_tail(value) for value in case_nos if value}


        if target_tail in _case_tail(case_nos):
            # print(target_tail, _case_tail(case_nos), 'hello')

            matched_entries.append(entry)





    return matched_entries


def _extract_pdf_links_from_table(page_html: str, page_url: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(page_html, "html.parser")
    links: List[Dict[str, Any]] = []
    seen = set()

    for row in soup.find_all("tr"):
        title = ""
        listing_date = None
        cells = row.find_all("td")
        if len(cells) >= 3:
            title = cells[1].get_text(" ", strip=True)
            date_text = cells[2].get_text(" ", strip=True)
            date_match = re.search(r"\d{2}[-/]\d{2}[-/]\d{4}", date_text)
            if date_match:
                listing_date = date_match.group(0).replace("/", "-")

        for tag in row.find_all("a", href=True):
            href = (tag.get("href") or "").strip()
            if not href:
                continue
            lower_href = href.lower()
            if lower_href.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg")):
                continue
            if ".pdf" not in lower_href:
                continue
            pdf_url = urljoin(page_url, href)
            if pdf_url in seen:
                continue
            seen.add(pdf_url)
            links.append(
                {
                    "title": title or tag.get_text(" ", strip=True) or "Delhi HC Cause List",
                    "listing_date": listing_date,
                    "pdf_url": pdf_url,
                    "source_page": page_url,
                }
            )
    return links


def fetch_cause_list_pdfs(
    listing_date: datetime,
    max_pages: int = 10,
    title_contains: Optional[str] = "Cause List of Sitting of Benches",
) -> List[Dict[str, Any]]:
    """
    Discover Delhi HC cause-list PDFs for a specific date.
    """
    date_token = listing_date.strftime("%d-%m-%Y")
    all_page_urls: List[str] = []

    for page_idx in range(max_pages):
        suffix = f"?page={str(page_idx)}" if page_idx else ""
        all_page_urls.append(f"{CAUSE_LIST_URL}{suffix}")

    found: List[Dict[str, Any]] = []
    seen_urls = set()
    for page_url in all_page_urls:
        try:
            resp = requests.get(page_url, headers=CAUSE_LIST_HEADERS, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Failed to fetch cause-list index page %s: %s", page_url, exc)
            continue

        for item in _extract_pdf_links_from_table(resp.text, page_url):
            if item["pdf_url"] in seen_urls:
                continue
            if item.get("listing_date") and item["listing_date"] != date_token:
                continue
            title = (item.get("title") or "").lower()

            if title_contains:
                if title_contains and title_contains.lower() not in title:
                    continue
            found.append(item)
            seen_urls.add(item["pdf_url"])

    return found


def fetch_cause_list_pdf_bytes(
    listing_date: datetime,
    max_pages: int = 10,
    title_contains: Optional[str] = "Cause List of Sitting of Benches",
) -> bytes:
    """
    Fetch the first matching Delhi HC cause-list PDF bytes for the given date.
    """
    pdfs = fetch_cause_list_pdfs(
        listing_date=listing_date,
        max_pages=max_pages,
        title_contains=title_contains,
    )
    if not pdfs and title_contains:
        pdfs = fetch_cause_list_pdfs(
            listing_date=listing_date,
            max_pages=max_pages,
            title_contains=None,
        )
    if not pdfs:
        raise ValueError(f"No Delhi HC cause-list PDF found for {listing_date.strftime('%d-%m-%Y')}")

    response = requests.get(
        pdfs[0]["pdf_url"],
        headers=CAUSE_LIST_HEADERS,
        timeout=60,
    )
    response.raise_for_status()
    return response.content


def fetch_cause_list_entries(
    listing_date: Optional[str] = None,
    case_no: Optional[str] = None,
    max_pages: int = 10,
) -> Dict[str, Any]:
    """
    Fetch Delhi HC cause-list PDFs for date and parse case-number entries.
    If case_no is passed, matched_entries contains only matching rows.
    """
    target_date = parse_listing_date(listing_date)
    date_token = target_date.strftime("%d-%m-%Y")
    pdfs = fetch_cause_list_pdfs(
        listing_date=target_date,
        max_pages=max_pages,
    )

    pdf_results: List[Dict[str, Any]] = []
    all_entries: List[Dict[str, Any]] = []
    matched_entries: List[Dict[str, Any]] = []

    for pdf in pdfs:
        tmp_path: Optional[str] = None
        try:
            resp = requests.get(pdf["pdf_url"], headers=CAUSE_LIST_HEADERS, timeout=60)
            resp.raise_for_status()
            with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
                tmp_pdf.write(resp.content)
                tmp_path = tmp_pdf.name

            parsed_entries = parse_cause_list_pdf(tmp_path)
            case_matched_entries = (
                find_case_entries(tmp_path, case_no) if case_no else parsed_entries
            )
            all_entries.extend(parsed_entries)
            matched_entries.extend(case_matched_entries)
            pdf_results.append(
                {
                    "url": pdf["pdf_url"],
                    "title": pdf.get("title"),
                    "status": "success",
                    "entries": parsed_entries,
                    "matched_entries": case_matched_entries,
                }
            )
        except Exception as exc:
            pdf_results.append(
                {
                    "url": pdf["pdf_url"],
                    "title": pdf.get("title"),
                    "status": "error",
                    "error": str(exc),
                    "entries": [],
                    "matched_entries": [],
                }
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    return {
        "status": "success" if pdf_results else "error",
        "listing_date": date_token,
        "pdfs": pdf_results,
        "entries": all_entries,
        "matched_entries": matched_entries,
        "searched_case_no": case_no,
    }

class DelhiHCService:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            # 'X-Requested-With': 'XMLHttpRequest'  <-- Removed from global headers
        }
        self.csrf_token = None

    def _get_initial_state(self):
        """
        Fetch the main page to get cookies, CSRF token, and the initial CAPTCHA code.
        """
        try:
            # Standard page load, accept HTML
            headers = {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8'
            }
            resp = self.session.get(SEARCH_URL, headers=headers, timeout=30)
            resp.raise_for_status()
            
            # Extract CSRF Token
            # Pattern: data: { "_token": "TOKEN", ... }
            token_match = re.search(r'data:\s*\{\s*"_token":\s*"([^"]+)"', resp.text)
            if token_match:
                self.csrf_token = token_match.group(1)
            else:
                logger.warning("Could not find CSRF token in page source.")

            # Extract CAPTCHA code
            # Pattern: <span id="captcha-code" class="captcha-code">CODE</span>
            soup = BeautifulSoup(resp.text, 'html.parser')
            captcha_span = soup.find('span', {'id': 'captcha-code'})
            captcha_code = captcha_span.text.strip() if captcha_span else None
            
            # Fallback to hidden input if span is empty (unlikely given source)
            if not captcha_code:
                random_id_input = soup.find('input', {'id': 'randomid'})
                if random_id_input:
                    captcha_code = random_id_input.get('value')
            
            return captcha_code

        except Exception as e:
            logger.error(f"Error fetching initial state: {e}")
            return None

    def validate_captcha(self, captcha_code: str) -> bool:
        """
        Validate the CAPTCHA with the server.
        """
        if not self.csrf_token or not captcha_code:
            return False
            
        try:
            data = {
                "_token": self.csrf_token,
                "captchaInput": captcha_code
            }
            headers = {'X-Requested-With': 'XMLHttpRequest'}
            resp = self.session.post(VALIDATE_CAPTCHA_URL, data=data, headers=headers, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            return result.get("success") is True
        except Exception as e:
            logger.error(f"Error validating CAPTCHA: {e}")
            return False

    def fetch_orders(self, orders_url: str) -> List[Dict[str, Any]]:
        """
        Fetch orders from the given orders URL (JSON endpoint).
        """
        try:
            headers = {'X-Requested-With': 'XMLHttpRequest'}
            params = {
                "draw": "1",
                "start": "0",
                "length": "200", # Fetch ample history
                "search[value]": "",
                "search[regex]": "false"
            }
            resp = self.session.get(orders_url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            orders = []
            for item in data.get('data', []):
                # Parse link
                link_html = item.get('case_no_order_link')
                doc_url = None
                if link_html:
                     soup = BeautifulSoup(link_html, 'html.parser')
                     a_tag = soup.find('a')
                     if a_tag and a_tag.get('href'):
                         doc_url = a_tag['href'].strip()
                
                # Date format is dd/mm/yyyy
                date_str = item.get('orddate')
                if date_str:
                    try:
                         # normalize to YYYY-MM-DD
                         dt = datetime.strptime(date_str, "%d/%m/%Y")
                         date_str = dt.strftime("%Y-%m-%d")
                    except ValueError:
                        pass
                
                if doc_url:
                    orders.append({
                        "date": date_str,
                        "description": f"Order dated {date_str}",
                        "document_url": doc_url
                    })
            
            # Sort by date descending
            orders.sort(key=lambda x: x['date'] if x['date'] else "", reverse=True)
            return orders

        except Exception as e:
            logger.error(f"Error fetching orders from {orders_url}: {e}")
            return []

    def fetch_ia_details(self, ia_url: str) -> List[Dict[str, Any]]:
        """
        Fetch IA details from the given IA URL (JSON endpoint).
        """
        try:
            headers = {'X-Requested-With': 'XMLHttpRequest'}
            params = {
                "draw": "1",
                "start": "0",
                "length": "200", # Fetch ample history
                "search[value]": "",
                "search[regex]": "false"
            }
            resp = self.session.get(ia_url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            ias = []
            for item in data.get('data', []):
                # Extract and normalize IA details with expanded field matching
                ia_no = (item.get('ia_no') or item.get('ia_number') or item.get('iano') or 
                         item.get('ia_no_display') or item.get('ia_no_order_link'))
                if ia_no and '<a' in str(ia_no):
                    ia_no = BeautifulSoup(str(ia_no), 'html.parser').get_text(strip=True)
                
                party = (item.get('party') or item.get('party_name') or item.get('petitioner') or 
                         item.get('pet') or item.get('pet_res'))
                if party and '<a' in str(party):
                    party = BeautifulSoup(str(party), 'html.parser').get_text(strip=True)

                filing_date = _normalize_date(item.get('filing_date') or item.get('iadate') or item.get('filingdate'))
                next_date = _normalize_date(item.get('next_date') or item.get('next_hearing_date') or 
                                           item.get('next_dt') or item.get('orddate'))
                status = (item.get('status') or item.get('ia_status') or item.get('order_judgement_status'))
                
                if ia_no or party:
                    ias.append({
                        "ia_no": (ia_no or "").strip(),
                        "ia_number": (ia_no or "").strip(),
                        "party": (party or "").strip(),
                        "filing_date": filing_date,
                        "next_date": next_date,
                        "status": (status or "").strip()
                    })
            
            return ias

        except Exception as e:
            logger.error(f"Error fetching IA details from {ia_url}: {e}")
            return []

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def fetch_case_details(self, case_type: str, case_no: str, case_year: str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch case status/details.
        Note: The Delhi HC search returns a list (table rows).
        """
        # 1. Get Session & Captcha
        captcha_code = self._get_initial_state()
        if not captcha_code:
            raise ValueError("Failed to retrieve CAPTCHA code")
            
        # 2. Validate Captcha
        if not self.validate_captcha(captcha_code):
            raise ValueError("CAPTCHA validation failed")

        # 3. Perform Search (DataTables Request)
        params = {
            "draw": "1",
            "start": "0",
            "length": "50",  # Get up to 50 results
            "case_type": case_type,
            "case_number": case_no,
            "case_year": case_year,
        }
        
        try:
            headers = {'X-Requested-With': 'XMLHttpRequest'}
            resp = self.session.get(SEARCH_URL, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            # The response format for DataTables is { draw: X, recordsTotal: Y, recordsFiltered: Z, data: [...] }
            rows = data.get('data', [])
            return self._parse_results(rows)
            
        except Exception as e:
            logger.error(f"Error searching cases: {e}")
            return None

    def _parse_results(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Parse the raw DataTables rows into canonical case schema.
        """
        parsed_cases = []
        for row in rows:
            pet_adv = (row.get('pet_adv') or "").strip()
            res_adv = (row.get('res_adv') or "").strip()
            diary_no = row.get('diary_no')
            diary_year = row.get('diary_yr')
            court_no = (row.get('courtno') or "").strip()
            category_code = row.get('catcode')
            respondent_str = (row.get('res') or "").strip()

            orders: List[Dict[str, Any]] = []
            ia_details: List[Dict[str, Any]] = []

            # Status normalization
            raw_status = (row.get('status') or "").strip()
            if raw_status == 'P':
                status_str = 'PENDING'
            elif raw_status == 'D':
                status_str = 'DISPOSED'
            else:
                status_str = raw_status

            disposal_nature = 0 if status_str == 'DISPOSED' else 1

            # Next listing date from row data
            next_dt_raw = row.get('h_d_dt') or row.get('old_h_dt')
            next_listing_date = _normalize_date(next_dt_raw) if next_dt_raw else None

            # last listing date / listing details
            listing_details_text = None
            last_listing_date = None

            case_no = None
            case_details_raw = None
            case_type = None

            # Parse 'ctype' column (Diary No. / Case No.[STATUS])
            if 'ctype' in row:
                soup = BeautifulSoup(row['ctype'], 'html.parser')
                text = soup.get_text(" ", strip=True)
                case_details_raw = text

                match = re.search(r'(.*?)\[(.*?)\]', text)
                if match:
                    case_no = match.group(1).strip()
                    if not status_str:
                        status_str = match.group(2).strip()
                        disposal_nature = 0 if status_str == 'DISPOSED' else 1
                else:
                    case_no = text

                # Derive case_type from case_no (part before " - ")
                if case_no:
                    ct_match = re.match(r'^([A-Z][A-Z0-9()./ &-]+?)\s*[-–]\s*\d', case_no)
                    case_type = ct_match.group(1).strip() if ct_match else None

                for a_tag in soup.find_all('a', href=True):
                    link_text = a_tag.get_text(" ", strip=True).lower()
                    href = a_tag['href'].strip()
                    if 'order' in link_text:
                        orders = self.fetch_orders(href)
                    elif 'ia' in link_text or 'cm' in link_text or 'interlocutory' in link_text:
                        ia_details.extend(self.fetch_ia_details(href))

            # Parse 'pet' column (Petitioner Vs. Respondent)
            petitioner_str = ""
            parties_text = None
            if 'pet' in row:
                soup = BeautifulSoup(row['pet'], 'html.parser')
                parties_text = soup.get_text(" ", strip=True)
                parts = re.split(r'\s+VS\.?\s+', parties_text, flags=re.IGNORECASE)
                if len(parts) >= 2:
                    petitioner_str = parts[0].strip()
                    if not respondent_str:
                        respondent_str = parts[1].strip()
                else:
                    petitioner_str = parties_text

            # Parse 'orderdate' column for listing details and fallback dates
            if 'orderdate' in row:
                soup = BeautifulSoup(row['orderdate'], 'html.parser')
                listing_details_text = soup.get_text(" ", strip=True)

                if not next_listing_date:
                    m = re.search(r'NEXT DATE\s*:\s*(\d{2}/\d{2}/\d{4})', listing_details_text, re.IGNORECASE)
                    if m:
                        next_listing_date = _normalize_date(m.group(1))
                    else:
                        m2 = re.search(r'(\d{2}/\d{2}/\d{4})', listing_details_text)
                        if m2:
                            next_listing_date = _normalize_date(m2.group(1))

                # Extract last date
                last_m = re.search(r'Last Date\s*:\s*(\d{2}/\d{2}/\d{4})', listing_details_text, re.IGNORECASE)
                if last_m:
                    last_listing_date = _normalize_date(last_m.group(1))

                if not court_no or court_no == 'NA':
                    cm = re.search(r'COURT NO\s*:\s*(\d+)', listing_details_text, re.IGNORECASE)
                    if cm:
                        court_no = cm.group(1)

            # Build canonical advocates string
            adv_lines: List[str] = []
            if pet_adv:
                adv_lines.append(f"Petitioner:\n{pet_adv}")
            if res_adv:
                adv_lines.append(f"Respondent:\n{res_adv}")
            advocates = "\n\n".join(adv_lines).strip() or None

            case_info: Dict[str, Any] = {
                "cin_no": None,
                "filing_no": str(diary_no) if diary_no else None,
                "case_no": case_no,
                "case_type": case_type,
                "registration_date": None,
                "filing_date": None,
                "first_listing_date": None,
                "next_listing_date": next_listing_date,
                "last_listing_date": last_listing_date,
                "decision_date": None,
                "court_no": court_no or None,
                "disposal_nature": disposal_nature,
                "purpose_next": None,
                "pet_name": [petitioner_str] if petitioner_str else [],
                "res_name": [respondent_str] if respondent_str else [],
                "advocates": advocates,
                "judges": None,
                "bench_name": None,
                "court_name": None,
                "history": [],
                "acts": [],
                "orders": orders,
                "ia_details": ia_details,
                "additional_info": {
                    "petitioner_advocate": pet_adv or None,
                    "respondent_advocate": res_adv or None,
                    "diary_no": diary_no,
                    "diary_year": diary_year,
                    "category_code": category_code,
                    "listing_details": listing_details_text,
                    "case_details_raw": case_details_raw,
                    "parties": parties_text,
                    "status": status_str,
                },
                "original_json": {k: v for k, v in row.items()},
            }

            parsed_cases.append(case_info)

        return parsed_cases

# Global instance
_service = DelhiHCService()

def get_delhi_case_details(case_type: str, case_no: str, case_year: str) -> Optional[Dict[str, Any]]:
    results = _service.fetch_case_details(case_type, case_no, case_year)
    if not results:
        return None
    return results[0]

async def persist_orders_to_storage(
    orders: list[dict] | None,
    case_id: str | None = None,
) -> list[dict] | None:
    """
    Upload scraped Delhi HC order documents to storage and update their URLs.
    """
    # Delhi HC order URLs might be simple GETs but let's check.
    # The URL is like: https://delhihighcourt.nic.in/app/showlogo/TOKEN/YEAR
    # It likely requires no special headers if opened in new tab, but maybe user-agent.
    
    def _fetch_order_document(url: str, referer: str | None = None) -> requests.Response:
        headers = {
             'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0',
        }
        if referer:
            headers["Referer"] = referer
        return requests.get(url, timeout=60, headers=headers)

    return await _persist_orders_to_storage(
        orders,
        case_id=case_id,
        fetch_fn=_fetch_order_document,
        referer=f"{BASE_URL}/",
    )

if __name__ == "__main__":
    import asyncio
    import json
    logging.basicConfig(level=logging.INFO)
    
    results = get_delhi_case_details("W.P.(C)", "533", "2025")
    print(json.dumps(results, indent=2))

    d=  datetime.strptime('02/04/2026', "%d/%m/%Y")
    print(fetch_cause_list_entries('02/04/2026','535')
    )
    # Optional: Test persist orders if needed (requires Supabase env vars)
    # if results and results[0]['orders']:
    #     asyncio.run(persist_orders_to_storage(results[0]['orders'], case_id="TEST_CASE_ID"))



