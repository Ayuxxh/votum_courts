# Court Scraper Feature Readiness Matrix

> **Last Updated**: 2026-02-15
> **Analysis Coverage**: All court scrapers in `/backend/ecourts/` directory

## Recent Updates

- **2026-02-15**: Added IA extraction for Delhi HC scraper (`delhi_hc.py`)
  - New `fetch_ia_details()` method to fetch IA information
  - New `_normalize_date()` helper for date parsing
  - Integrated IA details into `_parse_results()` output
  - IA details now included in case info with fields: `ia_no`, `party`, `filing_date`, `next_date`, `status`

## Overview

This document provides a comprehensive feature readiness assessment for all court scrapers in the Votum platform, covering 6 key features required for legal case management automation.

---

## Feature Summary Table

| Court/Scraper | Case Search | Details Search | IA Extraction | Cause List Fetch | Cause List Parse | CNR/Unique ID Fetch | Notes |
|--------------|-------------|---------------|--------------|------------------|------------------|-------------------|-------|
| **NCLAT** | ✅ `nclat_search_by_case_no()`<br>✅ `nclat_search_by_free_text()` (party/advocate) | ✅ `nclat_get_details(filing_no)` | ❌ No IA extraction | ❌ Not implemented | ❌ Not implemented | ✅ Uses `filing_no` as unique ID | Filing no required for details |
| **NCLT** | ✅ `nclt_search_by_filing_number()`<br>✅ `nclt_search_by_case_number()`<br>✅ `nclt_search_by_party_name()`<br>✅ `nclt_search_by_advocate_name()` | ✅ `nclt_get_details(bench, filing_no)` | ❌ Connected matters only | ✅ `fetch_cause_list_pdfs(bench_name, date)` | ✅ `parse_cause_list_pdf()`<br>✅ `find_case_entries()` | ✅ Uses `filing_no` | Bench-specific (14+ benches mapped) |
| **SCI** (Supreme Court) | ✅ `sci_search_by_diary_number()`<br>✅ `sci_search_by_case_number()`<br>✅ `sci_search_by_party_name()`<br>✅ `sci_search_by_aor_code()`<br>✅ `sci_search_by_court()` | ✅ `sci_get_details(diary_no, diary_year)` | ❌ No IA extraction | ✅ `sci_get_cause_list()` (web) | ✅ `sci_parse_cause_list_pdf()`<br>✅ `sci_find_case_entries_in_pdf()` | ✅ Diary No + Year | Math captcha solving via OCR |
| **Bombay HC** | ✅ `fetch_case_details(case_type, no, year)` | ✅ HTML parsing via `_parse_html_response()` | ❌ No IA extraction | ✅ `fetch_cause_list_pdf_bytes(listing_date, bench)` | ✅ `parse_cause_list_pdf()`<br>✅ `find_case_entries()` | ❌ No CNR fetch (case_type/no/year only) | Bench codes: B/N/A/G/K |
| **Delhi HC** | ✅ `fetch_case_details(case_type, no, year)` | ✅ DataTables parsing + orders | ✅ **NEW** `fetch_ia_details()` | ✅ `fetch_cause_list_pdfs(listing_date)`<br>✅ `fetch_cause_list_pdf_bytes()` | ✅ `parse_cause_list_pdf()`<br>✅ `find_case_entries()` | ❌ No CNR fetch | Visual captcha validation |
| **Gujarat HC** | ✅ `fetch_case_details(case_type, no, year)`<br>✅ `fetch_case_by_filing_no()`<br>✅ `fetch_case_by_cnr_no()` | ✅ Comprehensive JSON parsing (`_parse_details()`) | ✅ IA details from `applicationmatters` (ia_no, party, dates, status) | ✅ `fetch_cause_list_pdf_bytes(listing_date)` | ✅ `parse_cause_list_pdf()`<br>✅ `find_case_entries()` | ✅ **Has CNR fetch** | Most feature-complete HC scraper |
| **HC Services** (Generic HC) | ✅ `hc_search_by_case_number()`<br>✅ `hc_search_by_party_name()` | ✅ `hc_get_case_history()`<br>✅ `hc_get_case_details()` | ✅ IA table parsing (`ia_table`, `IAheading`) | ❌ Not implemented | ❌ Not implemented | ✅ `hc_search_by_cnr()` | Uses hcservices.ecourts.gov.in |
| **DC Services** (District Court) | ✅ `search_by_case_no()`<br>✅ `search_by_advocate_name()` | ✅ `get_case_details(case_params)` | ✅ IA table parsing | ✅ `fetch_cause_list(state, dist, complex, date)` | ✅ `parse_dc_cause_list_pdf()`<br>✅ `find_dc_case_entries()` | ✅ Uses CIN/CNR | OCR captcha solving |
| **eCourts** (Mobile API) | ✅ `search_by_case_number()`<br>✅ `search_by_advocate_name()` | ✅ `get_by_cnr()` | ❌ Not in API response | ❌ Not implemented | ❌ Not implemented | ✅ `get_by_cnr()` | Encrypted API (AES-256) |

---

## Feature-by-Feature Analysis

### 1. Case Search Function (9/9 ✅)

All scrapers implement case search with multiple modes:

| Scraper | Search Methods | Key Functions |
|---------|---------------|---------------|
| NCLAT | Case number, party, advocate, filing no, case type | `nclat_search_by_case_no()`, `nclat_search_by_free_text()` |
| NCLT | Filing number, case number, party name, advocate name | `nclt_search_by_*()` |
| SCI | Diary number, case number, party name, AOR code, court | `sci_search_by_*()` |
| Bombay HC | Case type/number/year | `fetch_case_details()` |
| Delhi HC | Case type/number/year | `fetch_case_details()` |
| Gujarat HC | Case type/number/year, filing number, CNR | `fetch_case_details()`, `fetch_case_by_*()` |
| HC Services | Case number, party name | `hc_search_by_case_number()`, `hc_search_by_party_name()` |
| DC Services | Case number, advocate name | `search_by_case_no()`, `search_by_advocate_name()` |
| eCourts | Case number, advocate name | `search_by_case_number()`, `search_by_advocate_name()` |

### 2. Details Search Function (9/9 ✅)

All scrapers can fetch comprehensive case details:

| Scraper | Detail Source | Parsed Fields |
|---------|--------------|---------------|
| NCLAT | HTML response | Parties, advocates, orders, hearings, status |
| NCLT | JSON API | Parties, advocates, proceedings, orders, connected matters |
| SCI | HTML tabs | Parties, advocates, category, listing dates, orders |
| Bombay HC | HTML response | Filing info, parties, advocates, orders |
| Delhi HC | DataTables JSON + orders API | Case info, parties, orders with documents |
| Gujarat HC | JSON (multi-section) | **Full parsing** with IA details, connected matters, proceedings |
| HC Services | HTML tables | Case details, parties, history, orders, IA details |
| DC Services | HTML tables | Case details, parties, history, acts, orders, IA details |
| eCourts | Mobile API (encrypted) | Case info, parties, advocates, history, acts, orders |

### 3. IA Data Extraction Logic (4/9 ⚠️)

**Ready for Production:**
- ✅ **Gujarat HC** (`gujarat_hc.py`): Lines 663-721
  ```python
  # Extracts from 'applicationmatters' section
  - ia_no / ia_number
  - description / party
  - filing_date / next_date / disposal_date
  - status
  - cin_no
  ```

- ✅ **Delhi HC** (`delhi_hc.py`): Lines 117-195
  ```python
  # Fetches from /app/ia-details endpoint or embedded in case page
  - ia_no / ia_number
  - party
  - filing_date
  - next_date
  - status
  ```

- ✅ **HC Services** (`hc_services.py`): Lines 974-990
  ```python
  # Parses 'IAheading' table
  - ia_number
  - party
  - filing_date
  - next_date
  - status
  ```

- ✅ **DC Services** (`dc_services.py`): Lines 800-813
  ```python
  # Parses 'ia_table' or 'IAheading' table
  - ia_number
  - party
  - filing_date
  - next_date
  - status
  ```

**Needs Implementation:**
- ❌ NCLAT, NCLT, SCI, Bombay HC, eCourts

**Recommendation:** Extract and standardize the IA parsing logic from `hc_services.py` to implement in other scrapers.

### 4. Cause List Fetching Logic (6/9 ⚠️)

| Scraper | Implementation | Method | Notes |
|---------|---------------|--------|-------|
| NCLT | `fetch_cause_list_pdfs(bench_name, date)` | POST with math captcha | Returns PDF URLs |
| SCI | `sci_get_cause_list(listing_date, ...)` | POST with math captcha | Returns HTML table + PDF links |
| Bombay HC | `fetch_cause_list_pdf_bytes(listing_date, bench)` | POST with captcha | Direct PDF download |
| Delhi HC | `fetch_cause_list_pdfs(listing_date)` | GET from index page | Scrape table for PDF links |
| Gujarat HC | `fetch_cause_list_pdf_bytes(listing_date)` | POST with token | Direct PDF download |
| DC Services | `fetch_cause_list(state, dist, complex, date)` | POST with captcha | Extract PDF links from response |
| NCLAT | ❌ | N/A | Would need implementation |
| HC Services | ❌ | N/A | Not applicable |
| eCourts | ❌ | N/A | Not available in API |

### 5. Cause List Parsing Logic (6/9 ✅)

All cause list parsers use PyMuPDF (fitz) with similar patterns:

| Scraper | Function | PDF Pattern | Output Fields |
|---------|----------|------------|--------------|
| NCLT | `parse_cause_list_pdf()` | Table columns (Sr/CASE DETAILS) | item_no, page_no, case_nos, text |
| SCI | `sci_parse_cause_list_pdf()` | Item number + columns | item_no, case_no, parties, advocates |
| Bombay HC | `parse_cause_list_pdf()` | Item-based detection | item_no, case_nos, parties, text |
| Delhi HC | `parse_cause_list_pdf()` | Item number + vertical spacing | item_no, case_nos, text |
| Gujarat HC | `parse_cause_list_pdf()` | Multi-column (case/party/advocate) | item_no, case_nos, parties, advocates |
| DC Services | `parse_dc_cause_list_pdf()` | Numbered entry detection | item_no, case_nos, text |

**Common Pattern:**
1. Extract text from PDF using `fitz`
2. Sort lines by Y then X coordinate
3. Detect item numbers (x < threshold)
4. Group lines between items
5. Extract case numbers using regex patterns
6. Parse parties by "VS" delimiter

### 6. CNR/Unique Number Based Fetching (6/9 ⚠️)

| Scraper | Unique ID Format | Function | Status |
|---------|------------------|----------|--------|
| Gujarat HC | CNR Number | `fetch_case_by_cnr_no(cnr_no)` | ✅ Full implementation |
| HC Services | CIN/CNR | `hc_search_by_cnr(cnr_number)` | ✅ Full implementation |
| DC Services | CIN/CNR | `get_by_cnr()` | ✅ Via mobile API |
| eCourts | CIN/CNR | `get_by_cnr()` | ✅ Via mobile API |
| NCLAT | Filing Number (10 digits) | `nclat_get_details(filing_no)` | ✅ Equivalent to CNR |
| NCLT | Filing Number | `nclt_get_details(bench, filing_no)` | ✅ Equivalent to CNR |
| SCI | Diary Number + Year | `sci_get_details(diary_no, diary_year)` | ✅ Composite unique ID |
| Bombay HC | Case Type + No + Year | `fetch_case_details(case_type, case_no, year)` | ❌ No CNR support |
| Delhi HC | Case Type + No + Year | `fetch_case_details(case_type, case_no, year)` | ❌ No CNR support |

---

## Detailed Scraper Profiles

### Gujarat HC (`gujarat_hc.py`) - ⭐ Most Feature-Complete

**Strengths:**
- ✅ Multiple search modes (case number, filing number, CNR)
- ✅ Comprehensive JSON parsing with proper type handling
- ✅ **IA data extraction** from `applicationmatters`
- ✅ Connected matters tracking
- ✅ Application/Appeal matters
- ✅ Cause list fetch and parse
- ✅ Orders with document URLs
- ✅ Bench mapping (15 benches)
- ✅ Case type caching with JSON persistence

**Key Functions:**
```python
fetch_case_details(case_type, case_no, case_year)
fetch_case_by_filing_no(case_type, filing_no, filing_year)
fetch_case_by_cnr_no(cnr_no)  # Only HC with CNR support!
fetch_cause_list_pdf_bytes(listing_date)
parse_cause_list_pdf(pdf_path)
```

**Sample IA Extraction:**
```python
# Lines 663-721
for item in self._get_section_records(data, "applicationmatters"):
    ia_number = item.get("aino") or item.get("ia_no")
    description = item.get("descriptionlm")
    status = item.get("statusnamelm")
    filing_date = self._parse_date(item.get("filingdatelm"))
    next_date = self._parse_date(item.get("nextdatelm"))
```

---

### NCLT (`NCLT.py`) - Best Cause List Implementation

**Strengths:**
- ✅ 14+ bench mapping
- ✅ Multiple search modes (filing, case number, party, advocate)
- ✅ Cause list PDF fetch with math captcha solving
- ✅ Sophisticated PDF parsing with column detection
- ✅ Case number normalization for matching

**Key Functions:**
```python
nclt_search_by_filing_number(bench, filing_number)
nclt_search_by_case_number(bench, case_type, case_number, case_year)
nclt_search_by_party_name(bench, party_type, party_name, ...)
nclt_search_by_advocate_name(bench, advocate_name, year)
nclt_get_details(bench, filing_no)
fetch_cause_list_pdfs(bench_name, date)  # Math captcha
parse_cause_list_pdf(pdf_path)
find_case_entries(pdf_path, case_no)
```

---

### SCI (`SCI.py`) - Supreme Court Special Features

**Strengths:**
- ✅ Most search modes (diary no, case no, party, AOR code, court)
- ✅ Math captcha expression solving (arithmetic)
- ✅ Multiple tabs (case details, listing dates, judgment/orders)
- ✅ Cause list with multiple filters (court, judge, AOR, party)
- ✅ PDF parsing for Supreme Court format

**Key Functions:**
```python
sci_search_by_diary_number(diary_number, diary_year)
sci_search_by_case_number(case_type, case_number, case_year)
sci_search_by_party_name(party_type, party_name, year, party_status)
sci_search_by_aor_code(party_type, aor_code, year, case_status)
sci_search_by_court(court, state, bench, case_type, case_number, case_year, order_date)
sci_get_details(diary_no, diary_year)
sci_get_cause_list(listing_date, search_by, causelist_type, msb)
sci_parse_cause_list_pdf(pdf_path)
sci_get_all_cases_for_day(listing_date)
```

**Math Captcha Solving:**
```python
# Lines 161-218
def _evaluate_captcha(question: str) -> int:
    # Safely evaluate arithmetic expressions like "14 + 6 ="
    expr = _normalize_captcha_expression(question)
    # Uses AST parsing for security
```

---

### HC Services (`hc_services.py`) - Generic High Court Wrapper

**Strengths:**
- ✅ Unified interface for all High Courts
- ✅ CNR-based searching
- ✅ Party name search
- ✅ **IA data extraction** from HTML tables
- ✅ Session refresh with captcha retry
- ✅ Comprehensive HTML parsing with fallbacks

**Key Functions:**
```python
hc_search_by_cnr(cnr_number)
hc_search_by_case_number(state_code, court_code, case_type, case_no, year)
hc_search_by_party_name(state_code, court_code, pet_name, res_name)
hc_get_case_history(state_code, court_code, court_complex_code, case_no, cino)
hc_get_case_details(state_code, court_code, case_id)
hc_get_states()
hc_get_benches(state_code)
hc_get_case_types(state_code, court_code)
```

**IA Extraction Pattern:**
```python
# Lines 974-990
ia_table = soup.find('table', class_='IAheading')
for row in rows[1:]:
    ia_details.append({
        'ia_number': cells[0].get_text(strip=True),
        'party': cells[1].get_text(strip=True),
        'filing_date': parse_iso_date(cells[2].get_text(strip=True)),
        'next_date': parse_iso_date(cells[3].get_text(strip=True)),
        'status': cells[4].get_text(strip=True)
    })
```

---

### DC Services (`dc_services.py`) - District Court Coverage

**Strengths:**
- ✅ Covers all district courts via eCourts
- ✅ State → District → Complex → Establishment hierarchy
- ✅ Captcha solving with retry
- ✅ **IA data extraction**
- ✅ Cause list support
- ✅ Order PDF URL generation via `display_pdf`

**Key Functions:**
```python
search_by_case_no(state_code, dist_code, complex_code, case_type, case_no, year)
search_by_advocate_name(...)
get_case_details(case_params)
fetch_cause_list(state_code, dist_code, complex_code, listing_date)
parse_dc_cause_list_pdf(pdf_path)
find_dc_case_entries(pdf_path, registration_no)
```

---

## Captcha Handling Comparison

| Scraper | Captcha Type | Solving Method | Retry Logic |
|---------|--------------|----------------|-------------|
| NCLAT | Image OCR | ddddocr | 8 attempts |
| NCLT | Math expression | Regex + eval | Single solve |
| SCI | Math arithmetic | AST-based eval | 3 attempts |
| Bombay HC | Image OCR | ddddocr | Per request |
| Delhi HC | Visual code | Manual validation | Per request |
| Gujarat HC | Image OCR | ddddocr | 3 attempts |
| HC Services | Image OCR | ddddocr | 5 attempts |
| DC Services | Image OCR | ddddocr | 3 attempts |
| eCourts | N/A (encrypted API) | N/A | N/A |

---

## Recommendations

### Priority 1: Implement IA Extraction

**Template to reuse:** `hc_services.py` (lines 974-990) or `dc_services.py` (lines 800-813)

**Scrapers needing IA extraction:**
1. Bombay HC
2. Delhi HC
3. NCLAT
4. NCLT (has connected matters but no IA-specific fields)
5. SCI

**Implementation pattern:**
```python
# Find IA table
ia_table = soup.find('table', class_='ia_table') or soup.find('table', class_='IAheading')
if ia_table:
    for row in ia_table.find_all('tr')[1:]:
        cols = row.find_all('td')
        if len(cols) >= 5:
            ia_details.append({
                'ia_number': cols[0].get_text(strip=True),
                'party': cols[1].get_text(strip=True),
                'filing_date': _normalize_date(cols[2].get_text(strip=True)),
                'next_date': cols[3].get_text(strip=True),
                'status': cols[4].get_text(strip=True)
            })
```

### Priority 2: Add CNR Support for Bombay & Delhi HC

**Reference implementation:** `gujarat_hc.py` `fetch_case_by_cnr_no()` (lines 814-821)

**Current limitation:** Bombay and Delhi HC only support case_type/case_no/year format. Consider adding CNR search if available on their portals.

### Priority 3: Standardize Cause List Parsing

**Current state:** Each scraper has its own parsing logic with similar patterns.

**Recommended action:** Create a shared utility module:
```python
# ecourts/cause_list_parser.py
class CauseListParser:
    @staticmethod
    def parse_pdf_columns(pdf_path, column_boundaries, case_pattern)
    @staticmethod
    def parse_item_based(pdf_path, x_threshold, case_pattern)
```

### Priority 4: Add Cause List for NCLAT

**Why missing:** NCLAT portal may not have cause list PDFs.

**Investigation needed:** Check if NCLAT publishes daily cause lists and if so, implement similar to NCLT.

---

## File Reference

- `NCLAT.py` - National Company Law Appellate Tribunal
- `NCLT.py` - National Company Law Tribunal
- `SCI.py` - Supreme Court of India
- `bombay_hc.py` - Bombay High Court
- `delhi_hc.py` - Delhi High Court
- `gujarat_hc.py` - Gujarat High Court
- `hc_services.py` - Generic High Court Services (all HCs)
- `dc_services.py` - District Court Services (all DCs)
- `ecourts.py` - eCourts Mobile API wrapper
- `order_storage.py` - Order document persistence to storage

---

## Metrics Summary

| Feature | Ready | Partial | Missing | Score |
|---------|-------|---------|---------|-------|
| Case Search | 9 | 0 | 0 | 100% |
| Details Search | 9 | 0 | 0 | 100% |
| IA Extraction | 4 | 0 | 5 | 44% |
| Cause List Fetch | 6 | 0 | 3 | 67% |
| Cause List Parse | 6 | 0 | 3 | 67% |
| CNR/Unique ID Fetch | 6 | 0 | 3 | 67% |
| **Overall** | - | - | - | **74%** |

---

*Generated: 2026-02-15*
*Analysis Tool: Claude Code*
