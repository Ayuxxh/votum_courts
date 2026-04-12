1. District court: https://services.ecourts.gov.in/ecourtindia_v6/
2. High Court:
   a. Gujarat High Court: https://gujarathighcourt.nic.in
   b. Bombay High Court: https://bombayhighcourt.nic.in/index.php
   c. Delhi High Court: https://delhihighcourt.nic.in/web/
   d. Remaining: https://hcservices.ecourts.gov.in/hcservices/main.php
3. SCI: https://www.sci.gov.in/case-status-case-no/
4. NCLT: https://nclt.gov.in/case-number-wise
5. NCLAT: https://nclat.nic.in/display-board/cases
6. ITAT: https://itat.gov.in/judicial/casestatus
7. CESTAT: https://cestat.gov.in/casestatus
8. DRT: https://drt.gov.in/#/casedetail

# Uses

1. fetch basic case data
2. fetch case details + save orders to db on request
3. update next hearing date using cron job, cases are updated on the day of the hearing around evening + send alert
4. fetch daily cause_list + send alert to the users who have their case listed.

# Case numbers:

- Use any arbitary number as case number for searching
- Use any year
- If neither works, use any surcommon name like `Singh`, `Gupta`, `Tiwari` to find cases and use their case number.
  [TO BE UPDATED]

trigger build.

Per File

- backend/ecourts/results/details_runs_2026-04-12/delhi_hc_details.json:1
  - Root shape is wrong: list of one object instead of a case object.
  - petitioner and respondent are strings, not text[].
  - next_listing_date is 24/03/2025, not ISO.
  - Missing major canonical fields: cin_no, case_type, registration_date, filing_date, history, original_json, advocates, judges, acts,
    additional_info.
  - petitioner_advocate, respondent_advocate, diary_no, category_code, listing_details, case_details_raw, parties need mapping into
    canonical fields or additional_info.
- backend/ecourts/results/details_runs_2026-04-12/drat_details.json:1
  - This is only an error payload (status=error, error_type, params, note), not a case detail object.
  - Should be excluded from votum_cases ingest or wrapped in a separate run-status artifact.
- backend/ecourts/results/details_runs_2026-04-12/sci_details.json:1
  - Top-level dates are malformed: 02-01-2026, 17-02-2025 03:25 PM, 07-04-2025), 15-04-2026.
  - history is null; should be [] if empty.
  - listing_dates[*].cl_date and ia_details[*].next_date are also non-ISO.
  - listing_dates is extra metadata and should move into additional_info/court_display.
- backend/ecourts/results/details_runs_2026-04-12/nclt_details.json:1
  - registration_date, filing_date, first_listing_date are DD-MM-YYYY, not ISO.
  - Missing status, connected_matters, application_appeal_matters.
  - Nested orders[*].listing_date and upload_date are also non-ISO / mixed datetime strings.
- backend/ecourts/results/details_runs_2026-04-12/nclat_details.json:1
  - Missing case_type even though type_name exists and is the likely source.
  - Missing original_json; original_html should probably be stored under it or under additional_info.
  - Advocate fields are split into petitioner_advocates / respondent_advocates; they need to be merged into canonical advocates.
- backend/ecourts/results/details_runs_2026-04-12/hc_services_details.json:1
  - additional_info is a string, but schema expects jsonb.
  - Missing ia_details, connected_matters, application_appeal_matters.
  - registration_date is missing at top level, but the raw payload appears to contain it in raw_data.
  - raw_data is useful, but should live under original_json/additional_info, not as a custom top-level field.
- backend/ecourts/results/details_runs_2026-04-12/drt_details.json:1
  - Missing history even though listing history appears to exist inside additional_info.
  - Nested orders[*].listing_date uses D/M/YYYY, not ISO.
  - bench_name, court_name, diary_no need canonical placement.
- backend/ecourts/results/details_runs_2026-04-12/dc_services_details.json:1
  - Duplicated CIN fields: both cino and cin_no; keep one canonical cin_no.
  - Missing connected_matters, application_appeal_matters.
  - ia_details[0].next_date includes extra text (02-05-2026 (HEARING ON INJUNCTION APPLICATION)), so it is not clean date data.
  - nature_of_disposal duplicates disposal_nature; consolidate.
- backend/ecourts/results/details_runs_2026-04-12/bombay_hc_details.json:1
  - Needs alias normalization (cnr_no, filing_no, pet_name, res_name, first_hearing_date).
  - Missing acts, additional_info, disposal_nature, purpose_next.
  - history[*].purpose values look malformed/junk-coded like ,528144721634,3 rather than readable purpose text.
