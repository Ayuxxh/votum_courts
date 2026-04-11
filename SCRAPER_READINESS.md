# Court Scraper Feature Readiness Matrix

> **Last Updated**: 2026-04-10
> **Analysis Coverage**: All court scrapers in `/backend/ecourts/` directory

## Recent Updates

- **2026-04-10**: Major updates to **Bombay HC**, **DC Services**, and **Automation Engine**
  - **Bombay HC**: Updated to new `causelistFinal` and `causelist/get-data` endpoints. Enhanced HTML parsing for case details, including robust IA extraction from `CaseNoApplCases` and CNR extraction from header text.
  - **DC Services**: Significant enhancement to `EcourtsWebScraper`. Implemented full cause list fetch/parse cycle with CAPTCHA solving. Robust case details parsing now handles `ia_table` and `IAheading` for IA extraction.
  - **Gujarat HC**: Improved `parse_cause_list_pdf` with widened column logic. Added `court_name` (judges) and `vc_link` extraction from cause lists. Details parsing now uses reliable section lookup instead of hardcoded indexes.
  - **NCLT**: Enhanced `parse_cause_list_pdf` with better item number and Coram detection. Added `vc_link` extraction from cause lists. `nclt_get_details` now extracts IA details from `mainFilnowithIaNoList`.
  - **Automation Engine**: `case_hearing_sync.py` now includes specialized Saturday/Sunday windows for Monday listing fetches and more robust next-listing-date change detection.
  - **e-Jagriti**: Confirmed production readiness for NCDRC/SCDRC/DCDRC via direct JSON API integration. Extracts full hearing history and order paths.

- **2026-03-22**: Expanded **DRT.py** readiness to cover **DRAT** and aligned frontend metadata strategy
  - Browser-inspected `https://drt.gov.in/#/casedetail` to capture the site’s real DRAT endpoint contract
  - Added DRAT support to `backend/ecourts/DRT.py` using:
    - `getDratCaseDetailCaseNoWise`
    - `getDratCaseDetailDiaryNoWise`
    - `drat_party_name_wise`
  - Confirmed DRAT case-number search uses `casetype`, not `casetypeId`
  - Live-verified DRAT case-number scraping against production with successful sample fetches
  - Frontend add-case now uses static DRT/DRAT benches from `src/app/home/cases/addcase/data/tribunals.json`
  - Added static DRAT case types in `src/app/home/cases/addcase/data/drat_case_types.json`

- **2026-03-20**: Added **DRT** scraper readiness and live verification
  - Added `backend/ecourts/DRT.py`
  - Implemented DRT search by case number, diary number, and party name
  - Implemented DRT detail fetch by `filing_no`
  - Added DRT order persistence support in `/ecourts/store_orders/`
  - Live-verified against `https://drt.gov.in/#/casedetail`
  - Current DRT gap: cause list fetch/parse is not implemented yet

- **2026-03-17**: Added **Automation & Reporting** features analysis
  - **Case Hearing Sync**: Implemented automated hearing-day sync with notification delivery.
  - **Daily Reports**: Added multi-variant PDF generation with Email/WhatsApp delivery.
  - **NCLAT**: Implemented Cause List fetch/parse and IA extraction.
  - **NCLT**: Enhanced IA extraction from `mainFilnowithIaNoList`.
  - **SCI**: Added IA extraction from listing dates/remarks.
  - **Bombay HC**: Added IA extraction from `CaseNoApplCases` tab and CNR extraction from case details.
  - **Overall IA Readiness**: Increased from 44% to 89%.
  - **Overall Cause List Readiness**: Increased from 67% to 78%.
  - **Party Search**: Added dedicated tracking for party-name based searching (67% readiness).

## Overview

This document provides a comprehensive feature readiness assessment for all court scrapers in the Votum platform, covering the core features required for legal case management automation and reporting.

---

## Feature Summary Table

| Court/Scraper                    | Case Search                                                                                                                                      | Party Search                                                        | Details Search                                                                 | IA Extraction                                  | Cause List Fetch                      | Cause List Parse                          | CNR/Unique ID Fetch                    | Notes                                                               |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------- | ------------------------------------- | ----------------------------------------- | -------------------------------------- | ------------------------------------------------------------------- |
| **DRT / DRAT**                   | ✅ `drt_search_by_case_number()`<br>✅ `drt_search_by_diary_number()`<br>✅ `drat_search_by_case_number()`<br>✅ `drat_search_by_diary_number()` | ✅ `drt_search_by_party_name()`<br>✅ `drat_search_by_party_name()` | ✅ `drt_get_details(drt, filing_no)`<br>✅ `drat_get_details(drat, filing_no)` | ✅ Extracts from `iaDetails` / proceeding rows | ❌ Not implemented                    | ❌ Not implemented                        | ✅ Uses `filing_no` when available     | DRT live-verified March 20, 2026; DRAT live-verified March 22, 2026 |
| **e-Jagriti**                    | ✅ `jagriti_search_case_details()`                                                                                                               | ✅ Free-text / advanced search                                      | ✅ `get_case_status()` + `get_case_history()`                                  | ❌ Not implemented                             | ❌ Not implemented                    | ❌ Not implemented                        | ✅ Filing reference / case number      | Live-verified; public daily order PDF fetch works                   |
| **NCLAT**                        | ✅ `nclat_search_by_case_no()`                                                                                                                   | ✅ `nclat_search_by_free_text()`                                    | ✅ `nclat_get_details(filing_no)`                                              | ✅ Extracts from HTML tables                   | ✅ `nclat_fetch_cause_list()`         | ✅ `nclat_parse_cause_list_pdf()`         | ✅ Uses `filing_no` as unique ID       | Now supports daily cause lists                                      |
| **NCLT**                         | ✅ `nclt_search_by_filing_number()`<br>✅ `nclt_search_by_case_number()`                                                                         | ✅ `nclt_search_by_party_name()`                                    | ✅ `nclt_get_details(bench, filing_no)`                                        | ✅ Maps from `mainFilnowithIaNoList`           | ✅ `fetch_cause_list_pdfs()`          | ✅ `parse_cause_list_pdf()`               | ✅ Uses `filing_no`                    | Bench-specific (14+ benches)                                        |
| **SCI** (Supreme Court)          | ✅ `sci_search_by_diary_number()`<br>✅ `sci_search_by_case_number()`                                                                            | ✅ `sci_search_by_party_name()`                                     | ✅ `sci_get_details(diary_no, diary_year)`                                     | ✅ Extracts from listing dates                 | ✅ `sci_get_cause_list()`             | ✅ `sci_parse_cause_list_pdf()`           | ✅ Diary No + Year                     | Math captcha via OCR                                                |
| **Bombay HC**                    | ✅ `fetch_case_details(case_type, no, year)`                                                                                                     | ❌ Not implemented                                                  | ✅ HTML parsing via `_parse_html_response()`                                   | ✅ Extracts from `CaseNoApplCases` tab         | ✅ `fetch_cause_list_pdf_bytes()`     | ✅ `parse_cause_list_pdf()`               | ✅ Extracts `cnr_no` from text         | Updated April 10, 2026                              |
| **Delhi HC**                     | ✅ `fetch_case_details(case_type, no, year)`                                                                                                     | ❌ Not implemented                                                  | ✅ DataTables parsing + orders                                                 | ✅ `fetch_ia_details()`                        | ✅ `fetch_cause_list_pdfs()`          | ✅ `parse_cause_list_pdf()`               | ❌ No CNR fetch                        | Visual captcha validation                                           |
| **Gujarat HC**                   | ✅ `fetch_case_details()`                                                                                                                        | ✅ `search_by_party_name()`                                         | ✅ Comprehensive JSON parsing                                                  | ✅ IA details from `applicationmatters`        | ✅ `fetch_cause_list_pdf_bytes()`     | ✅ `parse_cause_list_pdf()`               | ✅ **Has CNR fetch**                   | Improved PDF parsing & Judge/VC extraction                          |
| **HC Services** (Generic HC)     | ✅ `hc_search_by_case_number()`                                                                                                                  | ✅ `hc_search_by_party_name()`                                      | ✅ `hc_get_case_history()`                                                     | ✅ IA table parsing (`ia_table`)               | ❌ Not implemented                    | ❌ Not implemented                        | ✅ `hc_search_by_cnr()`                | Uses hcservices.ecourts.gov.in                                      |
| **DC Services** (District Court) | ✅ `search_by_case_no()`<br>✅ `search_by_advocate_name()`                                                                                       | ✅ `search_by_party_name()`                                         | ✅ `get_case_details()`                                                        | ✅ IA table parsing                            | ✅ `fetch_cause_list()`               | ✅ `parse_dc_cause_list_pdf()`            | ✅ Uses CIN/CNR                        | OCR captcha solving; Full Cause List support                        |
| **eCourts** (Mobile API)         | ✅ `search_by_case_number()`                                                                                                                     | ❌ Not implemented                                                  | ✅ `get_by_cnr()`                                                              | ❌ Not in API response                         | ❌ Not implemented                    | ❌ Not implemented                        | ✅ `get_by_cnr()`                      | Encrypted API (AES-256)                                             |

---

## Automation & Orchestration Readiness

Votum features high-level background jobs that orchestrate individual scrapers to maintain database freshness and deliver timely reports.

### 1. Case Sync Engine (`case_hearing_sync.py`)

This core engine manages the lifecycle of tracked cases, ensuring next hearing dates and orders are always up-to-date.

| Feature                 | Status   | Implementation                       | Key Capabilities                                                                    |
| ----------------------- | -------- | ------------------------------------ | ----------------------------------------------------------------------------------- |
| **Hearing Day Sync**    | ✅ Ready | `run_hearing_day_case_updates()`     | Fetches latest details for all cases listed on the target date.                     |
| **Order Persistence**   | ✅ Ready | `_persist_orders_for_case()`         | Uploads discovered order documents to storage and updates internal URLs.            |
| **Next-Day Prep**       | ✅ Ready | `run_next_day_cause_list_sync()`     | Pre-emptively fetches and parses tomorrow's cause lists for matching cases.         |
| **Notification Engine** | ✅ Ready | `_notify_case_recipients()`          | Delivers In-App and Email alerts when hearing dates change or new orders are found. |
| **Stale Case Recovery** | ✅ Ready | `is_stale` flag handling             | Automatically re-attempts sync for cases that failed previous fetch attempts.       |
| **Mirroring**           | ✅ Ready | `_mirror_cause_list_entry_to_case()` | Syncs cause list data back to the primary case table for easier frontend access.    |

### 2. Daily Case Reporting (`daily_case_reports.py`)

Automated reporting pipeline that generates and delivers professional PDF summaries to workspaces and individual users.

| Feature                 | Status   | Implementation                   | Key Capabilities                                                                |
| ----------------------- | -------- | -------------------------------- | ------------------------------------------------------------------------------- |
| **Multi-Variant PDFs**  | ✅ Ready | PDF 1 & PDF 2                    | Generates "Matters Listed Tomorrow" (PDF 1) and "Matters with Orders" (PDF 2).  |
| **Workspace Scoping**   | ✅ Ready | `_build_matters_by_workspace()`  | Groups and generates reports isolated by tenant workspace.                      |
| **User Scoping**        | ✅ Ready | `_build_matters_by_user_scope()` | Generates personalized reports for users based on their assigned/created cases. |
| **Email Delivery**      | ✅ Ready | `_send_report_email()`           | Delivers generated reports via SMTP with PDF attachments.                       |
| **WhatsApp Delivery**   | ✅ Ready | `_send_report_whatsapp()`        | Delivers reports via WhatsApp using cloud-hosted PDF links.                     |
| **Storage Integration** | ✅ Ready | `DAILY_REPORTS_BUCKET`           | Persists all generated reports to Supabase Storage with signed URL access.      |

---

## Feature-by-Feature Analysis

### 1. Case Search Function (11/11 ✅)

All scrapers implement case search with multiple modes including case number, party name, and advocate name.

### 2. Party Search Function (8/11 ✅)

Most scrapers support searching for cases by party name (Petitioner/Respondent).

| Scraper     | Search Method         | Function                                                     |
| ----------- | --------------------- | ------------------------------------------------------------ |
| e-Jagriti   | Free text / advanced  | `jagriti_search_case_details()`                              |
| DRT / DRAT  | Party Name            | `drt_search_by_party_name()` / `drat_search_by_party_name()` |
| NCLAT       | Free text search      | `nclat_search_by_free_text(search_by='party')`               |
| NCLT        | Party Name            | `nclt_search_by_party_name()`                                |
| SCI         | Party Name            | `sci_search_by_party_name()`                                 |
| Gujarat HC  | Party Name            | `search_by_party_name()`                                     |
| HC Services | Petitioner/Respondent | `hc_search_by_party_name()`                                  |
| DC Services | Party Name            | `search_by_party_name()`                                     |
| Bombay HC   | ❌                    | N/A                                                          |
| Delhi HC    | ❌                    | N/A                                                          |
| eCourts     | ❌                    | N/A                                                          |

### 3. Details Search Function (11/11 ✅)

All scrapers can fetch comprehensive case details including parties, advocates, orders, and hearing history.

### 4. IA Data Extraction Logic (10/11 ✅)

**Ready for Production:**

- ✅ **e-Jagriti**: Normalized from `caseHearingDetails` (hearing history records often contain IA context)
- ✅ **DRT / DRAT**: Extracts from `iaDetails` and proceeding/order rows in the rich detail response
- ✅ **NCLAT**: Extracts from HTML tables using `_extract_ia_rows`
- ✅ **NCLT**: Maps from `mainFilnowithIaNoList` in JSON response
- ✅ **SCI**: Extracts from listing dates section
- ✅ **Bombay HC**: Extracts from `CaseNoApplCases` div in HTML
- ✅ **Gujarat HC**: Extracts from `applicationmatters` section
- ✅ **Delhi HC**: Fetches via `fetch_ia_details` method
- ✅ **HC Services**: Parses `IAheading` table
- ✅ **DC Services**: Parses `ia_table` or `IAheading` table

**Needs Implementation:**

- ❌ eCourts (Mobile API does not expose IA details)

### 5. Cause List Fetching Logic (8/11 ✅)

| Scraper     | Implementation                 | Method                    | Notes                    |
| ----------- | ------------------------------ | ------------------------- | ------------------------ |
| e-Jagriti   | ❌ Not implemented             | N/A                       | No cause-list source yet |
| DRT / DRAT  | ❌ Not implemented             | N/A                       | No fetcher yet           |
| NCLAT       | `nclat_fetch_cause_list()`     | GET from daily-cause-list | Scrapes for PDF links    |
| NCLT        | `fetch_cause_list_pdfs()`      | POST with math captcha    | Returns PDF URLs         |
| SCI         | `sci_get_cause_list()`         | POST with math captcha    | Returns HTML + PDF links |
| Bombay HC   | `fetch_cause_list_pdf_bytes()` | POST with tokens          | Updated April 10, 2026   |
| Delhi HC    | `fetch_cause_list_pdfs()`      | GET from index page       | Scrapes for PDF links    |
| Gujarat HC  | `fetch_cause_list_pdf_bytes()` | POST with token           | Direct PDF download      |
| DC Services | `fetch_cause_list()`           | POST with captcha         | **NEW** Full support     |

### 6. Cause List Parsing Logic (8/11 ✅)

All cause list parsers use PyMuPDF (fitz) to extract text and regex to identify case numbers.

| Scraper     | Function                           | Pattern                     |
| ----------- | ---------------------------------- | --------------------------- |
| e-Jagriti   | ❌ Not implemented                 | N/A                         |
| DRT / DRAT  | ❌ Not implemented                 | N/A                         |
| NCLAT       | `nclat_parse_cause_list_pdf()`     | Item/Case No/Party/Advocate |
| NCLT        | `parse_cause_list_pdf()`           | Columnar SR/Case Details    |
| SCI         | `sci_parse_cause_list_pdf()`       | Item number + columns       |
| Bombay HC   | `parse_cause_list_pdf()`           | Item-based detection        |
| Delhi HC    | `parse_cause_list_pdf()`           | Vertical spacing based      |
| Gujarat HC  | `parse_cause_list_pdf()`           | Widened column format       |
| DC Services | `parse_dc_cause_list_pdf()`        | **NEW** Full support        |

### 7. CNR/Unique Number Based Fetching (10/11 ✅)

| Scraper     | Unique ID Format      | Fetch Status                                      |
| ----------- | --------------------- | ------------------------------------------------- |
| e-Jagriti   | Filing Ref / Case No  | ✅ Full status/history fetch + public order fetch |
| DRT / DRAT  | Filing Number         | ✅ Fetch by filing_no when present                |
| Gujarat HC  | CNR Number            | ✅ Full fetch support                             |
| HC Services | CIN/CNR               | ✅ Full fetch support                             |
| DC Services | CIN/CNR               | ✅ Full fetch support                             |
| eCourts     | CIN/CNR               | ✅ Full fetch support                             |
| NCLAT       | Filing Number         | ✅ Fetch by filing_no                             |
| NCLT        | Filing Number         | ✅ Fetch by filing_no                             |
| SCI         | Diary Number + Year   | ✅ Fetch by diary info                            |
| Bombay HC   | CNR Number            | ✅ Extracts from text                             |
| Delhi HC    | Case Type + No + Year | ❌ No CNR support                                 |

---

## Detailed Scraper Profiles

### DRT / DRAT (`DRT.py`)

- ✅ **DRT Case Search**: `drt_search_by_case_number()` and `drt_search_by_diary_number()` use the live multipart DRT API.
- ✅ **DRAT Case Search**: `drat_search_by_case_number()` and `drat_search_by_diary_number()` use browser-verified DRAT endpoints.
- ✅ **Party Search**: `drt_search_by_party_name()` and `drat_search_by_party_name()` supported.
- ✅ **Details Search**: Normalized response into shared case schema.
- ✅ **IA Extraction**: Extracts `iaDetails` and preserves proceeding/order metadata.
- ❌ **Cause List**: Not implemented yet.

---

### e-Jagriti

- ✅ **Search**: `search_by_case_no()` supports filing reference and case number candidates.
- ✅ **Status & History**: Extracts full hearing history including `proceedingText`.
- ✅ **Public Orders**: `orderDocumentPath` extraction from hearing details.
- ✅ **Commissions**: Full catalog of NCDRC/SCDRC/DCDRC via `get_commission_catalog()`.
- ❌ **Cause List**: Not implemented yet.

---

### NCLT (`NCLT.py`)

- ✅ **Cause List**: Improved parser handles both start and end-of-line item numbers and extracts VC links.
- ✅ **IA Extraction**: Maps IA numbers and details from `mainFilnowithIaNoList`.
- ✅ **Details**: Robust extraction of party advocates and order paths.

---

### Bombay HC (`bombay_hc.py`)

- ✅ **IA Extraction**: Extracts from `CaseNoApplCases` tab.
- ✅ **CNR Extraction**: Extracts `CNR No` from header text using regex.
- ✅ **Cause List**: Updated to new endpoints; extracts PDF links from JSON page response.

---

### District Court (`dc_services.py`)

- ✅ **Cause List**: Full automated fetch and parse cycle implemented in `EcourtsWebScraper`.
- ✅ **IA Extraction**: Robustly handles `ia_table` and `IAheading` in details HTML.
- ✅ **Orders**: Handles temporary PDF generation via `home/display_pdf` POST flow.

---

## Metrics Summary

| Feature             | Ready | Partial | Missing | Score   |
| ------------------- | ----- | ------- | ------- | ------- |
| Case Search         | 11    | 0       | 0       | 100%    |
| Party Search        | 8     | 0       | 3       | 73%     |
| Details Search      | 11    | 0       | 0       | 100%    |
| IA Extraction       | 10    | 0       | 1       | 91%     |
| Cause List Fetch    | 8     | 0       | 3       | 73%     |
| Cause List Parse    | 8     | 0       | 3       | 73%     |
| CNR/Unique ID Fetch | 10    | 0       | 1       | 91%     |
| **Automation Flow** | 12    | 0       | 0       | 100%    |
| **Overall**         | -     | -       | -       | **88%** |

---

_Generated: 2026-04-10_
_Analysis Tool: Gemini CLI_
