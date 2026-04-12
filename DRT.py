import re
from datetime import datetime
from functools import lru_cache
from typing import Any, Optional
from bs4 import BeautifulSoup
import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from tenacity import RetryError
from .order_storage import persist_orders_to_storage as _persist_orders_to_storage
from lxml import html as lxml_html
import re
import hashlib
from bs4 import BeautifulSoup


BASE_URL = "https://drt.gov.in"
API_URL = f"{BASE_URL}/drtapi"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/#/casedetail",
}

CASE_TYPE_ALIASES = {
    "oa": "Original Application",
    "original application": "Original Application",
    "ra": "Review Application",
    "review application": "Review Application",
    "ma": "Misc Application",
    "misc application": "Misc Application",
    "miscellaneous application": "Misc Application",
    "appeal": "Appeal",
    "ta": "Transfer Application",
    "transfer application": "Transfer Application",
    "ura": "URA",
    "sa": "Securitization Application",
    "securitization application": "Securitization Application",
    "air": "AIR",
    "counter claim": "COUNTER CLAIM",
    "cc": "COUNTER CLAIM",
    "trc": "Transfer Recovery Certificate",
    "transfer recovery certificate": "Transfer Recovery Certificate",
    "tsa": "Transfer Securitization Application",
    "transfer securitization application": "Transfer Securitization Application",
    "execution": "Execution",
    "te": "Transfer Execution",
    "transfer execution": "Transfer Execution",
    "chamber appeal": "Chamber Appeal",
    "tma": "Transfer Miscellaneous Application",
    "transfer miscellaneous application": "Transfer Miscellaneous Application",
    "transfer misc application": "Transfer Miscellaneous Application",
    "tra": "Transfer Review Application",
    "transfer review application": "Transfer Review Application",
    "tfa": "Transfer Appeal",
    "transfer appeal": "Transfer Appeal",
    "transfer counter claim": "Transfer counter claim",
    "tca": "Transfer chamber appeal",
    "transfer chamber appeal": "Transfer chamber appeal",
    "ibc-c": "IBC-C",
    "ibcc": "IBC-C",
    "ibc-a": "IBC-A",
    "ibca": "IBC-A",
}

TRIBUNAL_TYPE_DRT = "DRT"
TRIBUNAL_TYPE_DRAT = "DRAT"

CASE_NUMBER_ENDPOINTS = {
    TRIBUNAL_TYPE_DRT: "getCaseDetailCaseNoWise",
    TRIBUNAL_TYPE_DRAT: "getDratCaseDetailCaseNoWise",
}

DIARY_NUMBER_ENDPOINTS = {
    TRIBUNAL_TYPE_DRT: "getCaseDetailDiaryNoWise",
    TRIBUNAL_TYPE_DRAT: "getDratCaseDetailDiaryNoWise",
}

PARTY_NAME_ENDPOINTS = {
    TRIBUNAL_TYPE_DRT: "casedetail_party_name_wise",
    TRIBUNAL_TYPE_DRAT: "drat_party_name_wise",
}


SCHEME_NAME_DRT_ID_LIST =  [
  {"id": 9, "name": "DEBTS RECOVERY TRIBUNAL AHMEDABAD (DRT 1)"},
  {"id": 10, "name": "DEBTS RECOVERY TRIBUNAL AHMEDABAD (DRT 2)"},
  {"id": 11, "name": "DEBTS RECOVERY TRIBUNAL ALLAHABAD"},
  {"id": 12, "name": "DEBTS RECOVERY TRIBUNAL AURANGABAD"},
  {"id": 13, "name": "DEBTS RECOVERY TRIBUNAL BANGALORE (DRT 1)"},
  {"id": 39, "name": "DEBTS RECOVERY TRIBUNAL BANGALORE (DRT 2)"},
  {"id": 14, "name": "DEBTS RECOVERY TRIBUNAL CHANDIGARH (DRT 1)"},
  {"id": 15, "name": "DEBTS RECOVERY TRIBUNAL CHANDIGARH (DRT 2)"},
  {"id": 40, "name": "DEBTS RECOVERY TRIBUNAL CHANDIGARH (DRT 3)"},
  {"id": 16, "name": "DEBTS RECOVERY TRIBUNAL CHENNAI (DRT 1)"},
  {"id": 17, "name": "DEBTS RECOVERY TRIBUNAL CHENNAI (DRT 2)"},
  {"id": 18, "name": "DEBTS RECOVERY TRIBUNAL CHENNAI (DRT 3)"},
  {"id": 19, "name": "DEBTS RECOVERY TRIBUNAL COIMBATORE"},
  {"id": 20, "name": "DEBTS RECOVERY TRIBUNAL CUTTACK"},
  {"id": 41, "name": "DEBTS RECOVERY TRIBUNAL DEHRADUN"},
  {"id": 1, "name": "DEBTS RECOVERY TRIBUNAL DELHI (DRT 1)"},
  {"id": 2, "name": "DEBTS RECOVERY TRIBUNAL DELHI (DRT 2)"},
  {"id": 3, "name": "DEBTS RECOVERY TRIBUNAL DELHI (DRT 3)"},
  {"id": 21, "name": "DEBTS RECOVERY TRIBUNAL ERNAKULAM (DRT 1)"},
  {"id": 42, "name": "DEBTS RECOVERY TRIBUNAL ERNAKULAM (DRT 2)"},
  {"id": 22, "name": "DEBTS RECOVERY TRIBUNAL GUWAHATI"},
  {"id": 23, "name": "DEBTS RECOVERY TRIBUNAL HYDERABAD (DRT 1)"},
  {"id": 43, "name": "DEBTS RECOVERY TRIBUNAL HYDERABAD (DRT 2)"},
  {"id": 24, "name": "DEBTS RECOVERY TRIBUNAL JABALPUR"},
  {"id": 25, "name": "DEBTS RECOVERY TRIBUNAL JAIPUR"},
  {"id": 26, "name": "DEBTS RECOVERY TRIBUNAL KOLKATA (DRT 1)"},
  {"id": 27, "name": "DEBTS RECOVERY TRIBUNAL KOLKATA (DRT 2)"},
  {"id": 28, "name": "DEBTS RECOVERY TRIBUNAL KOLKATA (DRT 3)"},
  {"id": 29, "name": "DEBTS RECOVERY TRIBUNAL LUCKNOW"},
  {"id": 30, "name": "DEBTS RECOVERY TRIBUNAL MADURAI"},
  {"id": 31, "name": "DEBTS RECOVERY TRIBUNAL MUMBAI (DRT 1)"},
  {"id": 32, "name": "DEBTS RECOVERY TRIBUNAL MUMBAI (DRT 2)"},
  {"id": 33, "name": "DEBTS RECOVERY TRIBUNAL MUMBAI (DRT 3)"},
  {"id": 34, "name": "DEBTS RECOVERY TRIBUNAL NAGPUR"},
  {"id": 35, "name": "DEBTS RECOVERY TRIBUNAL PATNA"},
  {"id": 36, "name": "DEBTS RECOVERY TRIBUNAL PUNE"},
  {"id": 37, "name": "DEBTS RECOVERY TRIBUNAL RANCHI"},
  {"id": 44, "name": "DEBTS RECOVERY TRIBUNAL SILIGURI"},
  {"id": 38, "name": "DEBTS RECOVERY TRIBUNAL VISHAKHAPATNAM"}
]

SCHEME_NAME_DRAT_ID_LIST = [
  { "id": "101", "name": "DEBT RECOVERY APPELLATE TRIBUNAL - ALLAHABAD" },
  { "id": "102", "name": "DEBT RECOVERY APPELLATE TRIBUNAL - CHENNAI" },
  { "id": "100", "name": "DEBT RECOVERY APPELLATE TRIBUNAL - DELHI" },
  { "id": "104", "name": "DEBT RECOVERY APPELLATE TRIBUNAL - KOLKATA" },
  { "id": "103", "name": "DEBT RECOVERY APPELLATE TRIBUNAL - MUMBAI" }
]

def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


session = _new_session()


def _normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _normalize_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _normalize_date(value: str | None) -> Optional[str]:
    raw = _normalize_space(value)
    if not raw or raw in {"0", "NA", "N/A", "-"}:
        return None

    for fmt in (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d %b %Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    match = re.search(r"(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})", raw)
    if not match:
        return None

    day, month, year = match.groups()
    year = f"20{year}" if len(year) == 2 else year
    try:
        return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _clean_party(value: str | None) -> Optional[str]:
    cleaned = _normalize_space(value)
    if not cleaned or cleaned == "0":
        return None
    return cleaned


def _clean_advocate(value: str | None) -> Optional[str]:
    cleaned = _normalize_space(value)
    if not cleaned or cleaned == "0":
        return None
    return cleaned


def _make_case_no(case_type: str | None, case_no: str | None, case_year: str | None) -> Optional[str]:
    parts = [_normalize_space(case_type), _normalize_space(case_no), _normalize_space(case_year)]
    if not all(parts):
        return None
    return "/".join(parts)


def _multipart_payload(payload: dict[str, Any]) -> list[tuple[str, tuple[None, str]]]:
    items: list[tuple[str, tuple[None, str]]] = []
    for key, value in payload.items():
        if value is None:
            continue
        items.append((key, (None, str(value))))
    return items


@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def _post(endpoint: str, payload: Optional[dict[str, Any]] = None) -> Any:
    response = session.post(
        f"{API_URL}/{endpoint}",
        files=_multipart_payload(payload or {}),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _classify_tribunal_type(item: dict[str, Any]) -> str | None:
    scheme_id = str(item.get("schemeNameDrtId") or "").strip()
    schema_name = _normalize_key(item.get("SchemaName"))

    if "drat" in schema_name:
        return TRIBUNAL_TYPE_DRAT
    if "drt" in schema_name:
        return TRIBUNAL_TYPE_DRT
    if scheme_id.isdigit():
        return TRIBUNAL_TYPE_DRAT if int(scheme_id) >= 100 else TRIBUNAL_TYPE_DRT
    return None


@lru_cache(maxsize=2)
def get_tribunal_locations(tribunal_type: str = TRIBUNAL_TYPE_DRT) -> list[dict[str, str]]:
    normalized_type = _normalize_key(tribunal_type).upper()
    if normalized_type not in {TRIBUNAL_TYPE_DRT, TRIBUNAL_TYPE_DRAT}:
        raise ValueError(f"Unknown tribunal type: {tribunal_type}")

    data = _post("getDrtDratScheamName")
    results: list[dict[str, str]] = []
    for item in data or []:
        scheme_id = str(item.get("schemeNameDrtId") or "").strip()
        schema_name = _normalize_space(item.get("SchemaName"))
        if not scheme_id or not schema_name:
            continue
        if _classify_tribunal_type(item) != normalized_type:
            continue
        results.append({"schemeNameDrtId": scheme_id, "SchemaName": schema_name})
    return results


def get_drt_locations() -> list[dict[str, str]]:
    return get_tribunal_locations(TRIBUNAL_TYPE_DRT)


def get_drat_locations() -> list[dict[str, str]]:
    return get_tribunal_locations(TRIBUNAL_TYPE_DRAT)


@lru_cache(maxsize=128)
def get_tribunal_case_types(scheme_name_drt_id: str) -> list[dict[str, str]]:
    data = _post("getDrtDratCaseTyepName", {"schemeNameDrtId": scheme_name_drt_id})
    results: list[dict[str, str]] = []
    for item in data or []:
        case_type_id = str(item.get("caseType") or "").strip()
        case_type_name = _normalize_space(item.get("caseTypeName"))
        if case_type_id and case_type_name:
            results.append({"caseType": case_type_id, "caseTypeName": case_type_name})
    return results


def get_drt_case_types(scheme_name_drt_id: str) -> list[dict[str, str]]:
    return get_tribunal_case_types(scheme_name_drt_id)


def get_drat_case_types(scheme_name_drt_id: str) -> list[dict[str, str]]:
    return get_tribunal_case_types(scheme_name_drt_id)


def _resolve_drt_id(drt: str, tribunal_type: str = TRIBUNAL_TYPE_DRT) -> str:
    value = _normalize_space(drt)
    if not value:
        raise ValueError("drt is required")
    if value.isdigit():
        return value

    normalized = _normalize_key(value)
    locations = get_tribunal_locations(tribunal_type)

    for item in locations:
        if _normalize_key(item["SchemaName"]) == normalized:
            return item["schemeNameDrtId"]

    for item in locations:
        if normalized and normalized in _normalize_key(item["SchemaName"]):
            return item["schemeNameDrtId"]

    raise ValueError(f"Unknown DRT location: {drt}")


def _resolve_case_type_id(drt_id: str, case_type: str) -> str:
    value = _normalize_space(case_type)
    if not value:
        raise ValueError("case_type is required")
    if value.isdigit():
        return value

    normalized = _normalize_key(value)
    normalized = _normalize_key(CASE_TYPE_ALIASES.get(normalized, value))

    case_types = get_tribunal_case_types(drt_id)
    for item in case_types:
        if _normalize_key(item["caseTypeName"]) == normalized:
            return item["caseType"]

    raise ValueError(f"Unknown DRT case type: {case_type}")


def _get_drt_name(drt_id: str, tribunal_type: str = TRIBUNAL_TYPE_DRT) -> Optional[str]:
    for item in get_tribunal_locations(tribunal_type):
        if item["schemeNameDrtId"] == str(drt_id):
            return item["SchemaName"]
    return None


def _standardize_search_result(
    item: dict[str, Any],
    drt_id: str,
    tribunal_type: str = TRIBUNAL_TYPE_DRT,
) -> dict[str, Any]:
    case_no = _normalize_space(item.get("caseno"))
    diary_no = _normalize_space(item.get("diaryno"))
    filing_no = _normalize_space(item.get("filingNo"))
    case_type = _normalize_space(item.get("casetype"))
    applicant = _clean_party(item.get("applicant"))
    respondent = _clean_party(item.get("respondent"))

    return {
        "cino": filing_no or None,
        "filing_no": filing_no or None,
        "case_no": case_no or None,
        "diary_no": diary_no or None,
        "date_of_decision": None,
        "registration_date": _normalize_date(item.get("dateoffiling")),
        "pet_name": applicant,
        "res_name": respondent,
        "type_name": case_type or None,
        "bench": _get_drt_name(drt_id, tribunal_type),
        "court_name": _get_drt_name(drt_id, tribunal_type),
        "advocate_petitioner": _clean_advocate(item.get("applicantadvocate")),
        "advocate_respondent": _clean_advocate(item.get("respondentadvocate")),
    }


def _proceeding_to_order(item: dict[str, Any]) -> dict[str, Any]:
    order_url = _normalize_space(item.get("orderUrl")) or None
    cause_date = item.get("causelistdate")
    purpose = _normalize_space(item.get("purpose"))
    ascourt_name = _normalize_space(item.get("ascourtName"))
    court_name = _normalize_space(item.get("courtName"))
    court_no = _normalize_space(item.get("courtNo"))

    desc_parts = [f"Purpose: {purpose}" if purpose else None]
    if ascourt_name:
        desc_parts.append(f"As Court: {ascourt_name}")
    if court_name or court_no:
        desc_parts.append(f"Court: {' / '.join(x for x in [court_name, court_no] if x)}")

    return {
        "date": _normalize_date(cause_date) or cause_date,
        "description": " | ".join(part for part in desc_parts if part) or None,
        "document_url": order_url,
        "source_document_url": order_url,
        "listing_date": cause_date,
        "purpose": purpose or None,
        "court_name": court_name or ascourt_name or None,
        "court_no": court_no or None,
    }


def _ia_to_detail(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ia_no": _normalize_space(item.get("iano")) or None,
        "ia_number": _normalize_space(item.get("iano")) or None,
        "filing_date": _normalize_date(item.get("iadateoffiling")),
        "order_date": _normalize_date(item.get("iaorderdate")),
        "document_url": _normalize_space(item.get("iaUrl")) or None,
        "item_no": _normalize_space(item.get("item_no")) or None,
    }


def _normalize_detail(
    data: dict[str, Any],
    drt_id: str,
    filing_no: str | None = None,
    tribunal_type: str = TRIBUNAL_TYPE_DRT,
) -> dict[str, Any]:
    resolved_filing_no = (
        filing_no
        or _normalize_space(data.get("filingNo"))
        or _normalize_space(data.get("filingno"))
        or None
    )
    petitioner = _clean_party(data.get("petitionerName"))
    respondent = _clean_party(data.get("respondentName"))
    pet_adv = _clean_advocate(data.get("advocatePetName"))
    res_adv = _clean_advocate(data.get("advocateResName"))

    proceedings = data.get("caseProceedingDetails") or []
    orders = [_proceeding_to_order(item) for item in proceedings]
    ia_details = [_ia_to_detail(item) for item in (data.get("iaDetails") or [])]

    advocates = "\n".join(
        part
        for part in [
            f"Petitioner: {pet_adv}" if pet_adv else None,
            f"Respondent: {res_adv}" if res_adv else None,
        ]
        if part
    ) or None

    return {
        "cin_no": resolved_filing_no,
        "filing_no": resolved_filing_no,
        "registration_no": _make_case_no(data.get("casetype"), data.get("caseno"), data.get("caseyear")),
        "case_no": _make_case_no(data.get("casetype"), data.get("caseno"), data.get("caseyear")),
        "diary_no": (
            f"{_normalize_space(data.get('diaryno'))}/{_normalize_space(data.get('diaryyear'))}"
            if _normalize_space(data.get("diaryno")) and _normalize_space(data.get("diaryyear"))
            else None
        ),
        "registration_date": _normalize_date(data.get("dateoffiling")),
        "filing_date": _normalize_date(data.get("dateoffiling")),
        "next_listing_date": _normalize_date(data.get("nextlistingdate")),
        "decision_date": _normalize_date(data.get("dateofdisposal")),
        "court_no": _normalize_space(data.get("courtNo")) or None,
        "court_name": _normalize_space(data.get("courtName")) or _get_drt_name(drt_id, tribunal_type),
        "bench_name": _get_drt_name(drt_id, tribunal_type),
        "disposal_nature": _normalize_space(data.get("disposalNature")) or None,
        "purpose_next": _normalize_space(data.get("nextListingPurpose")) or None,
        "case_type": _normalize_space(data.get("casetype")) or None,
        "pet_name": [petitioner] if petitioner else [],
        "res_name": [respondent] if respondent else [],
        "advocates": advocates,
        "orders": orders,
        "ia_details": ia_details,
        "additional_info": {
            "case_status": _normalize_space(data.get("casestatus")) or None,
            "status_code": _normalize_space(data.get("status")) or None,
            "status_label": {
                "P": "PENDING",
                "D": "DISPOSAL",
            }.get(_normalize_space(data.get("status"))),
            "petitioner_address": _clean_party(data.get("petitionerApplicantAddress")),
            "respondent_address": _clean_party(data.get("respondentDefendentAddress")),
            "main_case_case_no": _normalize_space(data.get("maincasecaseno")) or None,
            "additional_party_petitioner": _clean_party(data.get("additionalpartypet")),
            "additional_party_respondent": _clean_party(data.get("additionalpartyres")),
            "suit_amount": _normalize_space(data.get("suit_amount")) or None,
            "rc_detail": data.get("rcdetail"),
            "listing_history": proceedings,
        },
        "original_json": data,
    }


def _fetch_rich_case_detail(
    drt_id: str,
    data: dict[str, Any],
    tribunal_type: str = TRIBUNAL_TYPE_DRT,
) -> dict[str, Any]:
    diary_no = _normalize_space(data.get("diaryno"))
    diary_year = _normalize_space(data.get("diaryyear"))
    case_no = _normalize_space(data.get("caseno"))
    case_year = _normalize_space(data.get("caseyear"))
    case_type = _normalize_space(data.get("casetype"))

    if diary_no and diary_year:
        rich = _post(
            DIARY_NUMBER_ENDPOINTS[tribunal_type],
            {
                "schemeNameDrtId": drt_id,
                "diaryNo": diary_no,
                "diaryYear": diary_year,
            },
        )
        if isinstance(rich, dict) and (
            rich.get("caseProceedingDetails") or rich.get("iaDetails") or rich.get("orderUrl")
        ):
            return rich

    if case_no and case_year and case_type:
        case_type_id = _resolve_case_type_id(drt_id, case_type)
        payload = {
            "schemeNameDrtId": drt_id,
            "caseNo": case_no,
            "caseYear": case_year,
        }
        if tribunal_type == TRIBUNAL_TYPE_DRAT:
            payload["casetype"] = case_type_id
        else:
            payload["casetypeId"] = case_type_id
        rich = _post(
            CASE_NUMBER_ENDPOINTS[tribunal_type],
            payload,
        )
        if isinstance(rich, dict):
            return rich

    return data


@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def drt_search_by_case_number(
    drt: str,
    case_type: str,
    case_number: str,
    case_year: str,
    tribunal_type: str = TRIBUNAL_TYPE_DRT,
) -> dict[str, Any]:
    drt_id = _resolve_drt_id(drt, tribunal_type=tribunal_type)
    case_type_id = _resolve_case_type_id(drt_id, case_type)
    payload = {
        "schemeNameDrtId": drt_id,
        "caseNo": case_number,
        "caseYear": case_year,
    }
    if tribunal_type == TRIBUNAL_TYPE_DRAT:
        payload["casetype"] = case_type_id
    else:
        payload["casetypeId"] = case_type_id
    data = _post(
        CASE_NUMBER_ENDPOINTS[tribunal_type],
        payload,
    )
    detail = _normalize_detail(data or {}, drt_id, tribunal_type=tribunal_type)
    detail["search_result"] = {
        "cino": detail.get("filing_no"),
        "filing_no": detail.get("filing_no"),
        "case_no": detail.get("case_no"),
        "diary_no": detail.get("diary_no"),
        "date_of_decision": detail.get("decision_date"),
        "registration_date": detail.get("registration_date"),
        "pet_name": detail.get("pet_name", [None])[0] if detail.get("pet_name") else None,
        "res_name": detail.get("res_name", [None])[0] if detail.get("res_name") else None,
        "type_name": detail.get("case_type"),
        "bench": detail.get("bench_name"),
        "court_name": detail.get("court_name"),
    }
    return detail


def drat_search_by_case_number(drat: str, case_type: str, case_number: str, case_year: str) -> dict[str, Any]:
    return drt_search_by_case_number(
        drat,
        case_type,
        case_number,
        case_year,
        tribunal_type=TRIBUNAL_TYPE_DRAT,
    )


@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def drt_search_by_diary_number(
    drt: str,
    diary_number: str,
    diary_year: str,
    tribunal_type: str = TRIBUNAL_TYPE_DRT,
) -> dict[str, Any]:
    drt_id = _resolve_drt_id(drt, tribunal_type=tribunal_type)
    data = _post(
        DIARY_NUMBER_ENDPOINTS[tribunal_type],
        {
            "schemeNameDrtId": drt_id,
            "diaryNo": diary_number,
            "diaryYear": diary_year,
        },
    )
    return _normalize_detail(data or {}, drt_id, tribunal_type=tribunal_type)


def drat_search_by_diary_number(drat: str, diary_number: str, diary_year: str) -> dict[str, Any]:
    return drt_search_by_diary_number(
        drat,
        diary_number,
        diary_year,
        tribunal_type=TRIBUNAL_TYPE_DRAT,
    )






@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def drt_search_by_party_name(
    drt: str,
    party_name: str,
    tribunal_type: str = TRIBUNAL_TYPE_DRT,
) -> list[dict[str, Any]]:
    drt_id = _resolve_drt_id(drt, tribunal_type=tribunal_type)
    data = _post(
        PARTY_NAME_ENDPOINTS[tribunal_type],
        {
            "schemeNameDratDrtId": drt_id,
            "partyName": party_name,
        },
    )
    return [_standardize_search_result(item, drt_id, tribunal_type) for item in (data or [])]


def drat_search_by_party_name(drat: str, party_name: str) -> list[dict[str, Any]]:
    return drt_search_by_party_name(
        drat,
        party_name,
        tribunal_type=TRIBUNAL_TYPE_DRAT,
    )


@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def drt_get_details(
    drt: str,
    filing_no: str,
    tribunal_type: str = TRIBUNAL_TYPE_DRT,
) -> dict[str, Any]:
    drt_id = _resolve_drt_id(drt, tribunal_type=tribunal_type)
    data = _post(
        "getCaseDetailPartyWise",
        {
            "schemeNameDrtId": drt_id,
            "filingNo": filing_no,
        },
    )
    if isinstance(data, dict) and not (data.get("caseProceedingDetails") or data.get("iaDetails")):
        data = _fetch_rich_case_detail(drt_id, data, tribunal_type=tribunal_type)
    return _normalize_detail(
        data or {},
        drt_id,
        filing_no=filing_no,
        tribunal_type=tribunal_type,
    )


def drat_get_details(drat: str, filing_no: str) -> dict[str, Any]:
    return drt_get_details(
        drat,
        filing_no,
        tribunal_type=TRIBUNAL_TYPE_DRAT,
    )


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
        referer=f"{BASE_URL}/#/casedetail",
    )

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": "https://drt.gov.in/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


def drt_fetch_causelist(listing_date: str, location: str):

    cause_list_links = []

    # Query logic
    #Date
    listing_date = _normalize_date(listing_date)
    listing_date = datetime.strptime(listing_date, "%Y-%m-%d").strftime("%d/%m/%Y")

    session.get("https://drt.gov.in/", headers=headers)

    try:
        #Location
        for loc in SCHEME_NAME_DRAT_ID_LIST:
            if location.upper() in loc['name']:
                location = loc
                break
            elif  loc['name'].upper() in location.upper():
                location = loc
                break

        
        # Fetching court names
        court_names =  _post("getCourtName", {
        "schemeNameDrtId": location['id'],
        "listingDate" : str(listing_date),  
        })

        # Featching court No
        for court_name in court_names:

            court_no = _post("getDrtDratCourtNo", {
            "schemeNameDrtId": location['id'],
            "listingDate" : str(listing_date),
            "courtNameId" : court_name['courtNameId']
            })
    
            try:
        
                # #Fetching causelist links
                links = _post("getDrtCauselistReport", {
                "schemeNameDrtId": location['id'],
                "causeListDate" : str(listing_date),
                "courtNameId" : court_name['courtNameId'],
                "courtNo" : court_no['courtNo'],
                })
                for link in links.values():
                    cause_list_links.append(link)


                
            except RetryError as e:
                original_exception = e.last_attempt.exception()
                print(original_exception)  
                print("No cause-list available for :" , listing_date, location['name'])
                continue


    except Exception as e:
            print("DRT fetch causelist failed.")
            original_exception = e.last_attempt.exception()
            print(original_exception)  

    return cause_list_links






def parse_cause_list(response_text: str, source_link: str) -> list[dict]:
    soup = BeautifulSoup(response_text, 'lxml')
    table = soup.find('table', id='content')
    if not table:
        return []

    thead = table.find('thead')
    tbody = table.find('tbody')

    # --- Coram & VC link ---
    header_rows = thead.find_all('tr')

    coram_text = ""
    if len(header_rows) > 4:
        raw = header_rows[4].get_text(" ", strip=True)
        coram_text = re.sub(r'\s+', ' ', raw.replace('\xa0', ' ')).strip()

    vc_link = None
    if len(header_rows) > 5:
        time_row_text = header_rows[5].get_text(" ", strip=True)
        vc_match = re.search(r'https?://\S+', time_row_text)
        vc_link = vc_match.group(0).rstrip('.') if vc_match else None

    # --- Body rows ---
    entries = []
    current_section = ""

    for row in tbody.find_all('tr'):
        section_tag = row.select_one('td[colspan] b u')
        if section_tag:
            current_section = section_tag.get_text(strip=True)
            continue

        cells = row.find_all('td', recursive=False)
        visible = [c for c in cells if 'display:none' not in c.get('style', '')]
        if len(visible) < 4:
            continue

        sl_no = visible[0].get_text(strip=True)
        if not sl_no.isdigit():
            continue

        # case_nos — all case numbers including linked IAs
        case_cell_text = visible[1].get_text(" ", strip=True)
        case_nos = re.findall(
            r'(?:OA|SA|NDN|MA|IA|CA|SCA|WP|RCA|RFA)/[\w/\-\(\) ]+?(?=\s+IN\b|\s*$)',
            case_cell_text, re.IGNORECASE
        )
        case_nos = [c.strip() for c in case_nos if c.strip()]
        primary_case_no = case_nos[0] if case_nos else case_cell_text.strip()

        # parties — list of strings matching GHC format:
        # [petitioner_name, petitioner_advocate, respondent_name, respondent_advocate]
        parties_cell = visible[2]
        parties_raw = parties_cell.get_text("\n", strip=True)
        vs_split = re.split(r'\n\s*[Vv][Ss]\.?\s*\n', parties_raw)
        petitioner_block = vs_split[0].strip() if len(vs_split) > 0 else ""
        respondent_block = vs_split[1].strip() if len(vs_split) > 1 else ""

        advocate_cell = visible[3]
        advocate_raw = advocate_cell.get_text("\n", strip=True)
        adv_split = re.split(r'-{3,}', advocate_raw)
        petitioner_adv = adv_split[0].strip() if len(adv_split) > 0 else ""
        respondent_adv = adv_split[1].strip() if len(adv_split) > 1 else ""

        # parties list: [petitioner, petitioner_adv, respondent, respondent_adv]
        parties = [
            p for p in [petitioner_block, petitioner_adv, respondent_block, respondent_adv]
            if p
        ]

        # advocates — remark cell text (matches GHC pattern of putting hearing notes here)
        remark = re.sub(r'\s+', ' ', visible[4].get_text(" ", strip=True)).strip() if len(visible) > 4 else ""

        # raw text
        raw_text = "\n".join([
            sl_no,
            case_cell_text,
            parties_cell.get_text("\n", strip=True),
            advocate_cell.get_text("\n", strip=True),
        ])

        entry_hash = hashlib.sha256(raw_text.encode()).hexdigest()

        entries.append({
            "item_no": sl_no,
            "page_no": None,
            "case_no": primary_case_no,
            "case_nos": case_nos,
            "parties": parties,
            "petitioner": petitioner_block or None,
            "respondent": respondent_block or None,
            "party_names": None,
            "advocates": remark or None,
            "court_name": f"CORAM: {coram_text}",
            "vc_link": vc_link,
            "text": raw_text,
            "entry_hash": entry_hash,
        })

    return entries

def drt_find_entries_in_cause_list(cause_list_links: list, registration_no: str) -> list[dict]:
    all_entries = []
    matched = []

    for cause_list_link in cause_list_links:
        response = session.get(cause_list_link, headers=DEFAULT_HEADERS)

        if not response.text.strip():
            print("Empty response:", cause_list_link)
            continue

        entries = parse_cause_list(response.text, cause_list_link)
        all_entries.extend(entries)

        for entry in entries:
            if (registration_no.upper() in entry['case_no'].upper() or
                registration_no.upper() in entry['petitioner'].upper() or
                registration_no.upper() in entry['respondent'].upper()):
                matched.append(entry)

    return matched  



def drat_fetch_causelist(listing_date: str, location: str):

    cause_list_links = []

    # Query logic
    #Date
    listing_date = _normalize_date(listing_date)
    listing_date = datetime.strptime(listing_date, "%Y-%m-%d").strftime("%d/%m/%Y")

    session.get("https://drt.gov.in/", headers=headers)

    try:
        #Location
        for loc in SCHEME_NAME_DRAT_ID_LIST:
            if location.upper() in loc['name']:
                location = loc
                break
            elif  loc['name'].upper() in location.upper():
                location = loc
                break

        
        # Fetching court names
        court_names =  _post("getDratCourtName", {
        "schemeNameDrtId": location['id'],
        "listingDate" : str(listing_date),  
        })

        # Featching court No
        for court_name in court_names:

            court_nos = _post("getDratCourtNo", {
            "schemeNameDrtId": location['id'],
            "listingDate" : str(listing_date),
            "benchNature" : court_name['courtNameId']
            })

            for c_no in court_nos:
    
                try:
            
                    # #Fetching causelist links
                    links = _post("getDratCauselistReport", {
                    "schemeNameDrtId": location['id'],
                    "causeListDate" : str(listing_date),
                    "courtNameId" : court_name['courtNameId'],
                    "courtNo" : c_no['courtNo'],
                    })
                    for link in links.values():
                        cause_list_links.append(link)


                    
                except RetryError as e:
                    original_exception = e.last_attempt.exception()
                    print(original_exception)  
                    print("No cause-list available for :" , listing_date, location['name'])
                    continue


    except Exception as e:
            print("DRT fetch causelist failed.")
            original_exception = e.last_attempt.exception()
            print(original_exception)  

    return cause_list_links


def parse_drat_cause_list(response_text: str, source_link: str) -> list[dict]:
    soup = BeautifulSoup(response_text, 'lxml')

    tables = soup.find_all('table')
    main_table = None
    for t in tables:
        if t.find('td', string=re.compile(r'DEBT RECOVERY APPELLATE TRIBUNAL', re.I)):
            main_table = t
            break
    if not main_table:
        return []

    all_rows = main_table.find_all('tr')

    # --- Header ---
# --- Header --- replace the existing coram/vc loop with this ---

    coram_text = ""
    vc_link = None

    for row in all_rows:
        cells = row.find_all('td')
        if not cells:
            continue

        raw = row.get_text(" ", strip=True).replace('\xa0', ' ')
        cleaned = re.sub(r'\s+', ' ', raw).strip()

        # Coram: must contain HON'BLE or JUSTICE, must not be a section/data row
        if not coram_text and re.search(r"HON'BLE|JUSTICE|REGISTRAR", cleaned, re.I):
            # Skip rows that are clearly address/title rows (contain "Floor", "Road" etc)
            if not re.search(r'\d+th Floor|Shastri|Haddows|Building|Chennai \d{6}', cleaned, re.I):
                coram_text = cleaned

        # VC link
        if not vc_link:
            vc_match = re.search(r'https?://\S+', cleaned)
            if vc_match:
                vc_link = vc_match.group(0).rstrip('.')
        # --- Body ---
    entries = []
    current_section = ""

    for row in all_rows:
        cells = row.find_all('td')
        if not cells:
            continue

        # Section header
        section_tag = row.select_one('b u') or row.select_one('u b')
        if section_tag and len(cells) <= 2:
            current_section = section_tag.get_text(strip=True)
            continue

        if len(cells) < 6:
            continue

        sl_no = cells[0].get_text(strip=True)
        if not sl_no.isdigit():
            continue

        case_cell_text = cells[1].get_text(" ", strip=True)

        # Primary case no — DN/.../... or REGULAR APPEAL/... or MISC APPEAL/...
        # These are always the first identifiable case number in the cell
        primary_match = re.search(
            r'(?:DN/\s*\d+/\d+\s*\([^)]+\)|'
            r'(?:REGULAR|MISC)\s+APPEAL/\d+/\d+|'
            r'(?:OA|SA|TA|TSA)/\d+/\d+)',
            case_cell_text, re.IGNORECASE
        )
        primary_case_no = re.sub(r'\s+', ' ', primary_match.group(0)).strip() if primary_match else case_cell_text.strip()

        # All case nos — filter out empty IA// 
        raw_case_nos = re.findall(
            r'(?:DN/\s*\d+/\d+\s*\([^)]+\)|'
            r'(?:REGULAR|MISC)\s+APPEAL/\d+/\d+|'
            r'IA/\d+/\d+|'
            r'(?:OA|SA|TA|TSA)/\d+/\d+(?:\s*\([^)]+\))?)',
            case_cell_text, re.IGNORECASE
        )
        case_nos = [re.sub(r'\s+', ' ', c).strip() for c in raw_case_nos if c.strip()]

        appellant      = cells[2].get_text(" ", strip=True).strip()
        respondent     = cells[3].get_text(" ", strip=True).strip()
        appellant_adv  = re.sub(r'[\s\xa0&;]+', ' ', cells[4].get_text(" ", strip=True)).strip()
        respondent_adv = re.sub(r'[\s\xa0&;]+', ' ', cells[5].get_text(" ", strip=True)).strip()

        # Drop placeholder values
        for placeholder in ('', '&nbsp;', '\xa0', '0', '&nbsp', 'nbsp'):
            if appellant_adv == placeholder:
                appellant_adv = None
            if respondent_adv == placeholder:
                respondent_adv = None

        parties = [p for p in [appellant, appellant_adv, respondent, respondent_adv] if p]

        advocates_str = " | ".join(filter(None, [appellant_adv, respondent_adv])) or None

        raw_text = "\n".join([
            sl_no,
            case_cell_text,
            appellant,
            respondent,
            appellant_adv or "",
            respondent_adv or "",
        ])
        entry_hash = hashlib.sha256(raw_text.encode()).hexdigest()

        entries.append({
            "item_no": sl_no,
            "page_no": None,
            "case_no": primary_case_no,
            "case_nos": case_nos,
            "parties": parties,
            "petitioner": appellant or None,
            "respondent": respondent or None,
            "party_names": None,
            "advocates": advocates_str,
            "court_name": f"CORAM: {coram_text}",
            "vc_link": vc_link,
            "text": raw_text,
            "entry_hash": entry_hash,
        })

    return entries


def drat_find_entries_in_cause_list(cause_list_links: list, registration_no: str) -> list[dict]:
    matched = []


    registration_no =  re.sub(r'[^0-9/]', '', registration_no).strip('/')

    for cause_list_link in cause_list_links:
        response = session.get(cause_list_link, headers=DEFAULT_HEADERS)

        if not response.text.strip():
            print("Empty response:", cause_list_link)
            continue

        entries = parse_drat_cause_list(response.text, cause_list_link)
    
        for entry in entries:
            searchable = " ".join(filter(None, [
                entry.get('case_no', ''),
                entry.get('petitioner', ''),
                entry.get('respondent', ''),
            ])).upper()

            if registration_no.upper() in searchable:
                matched.append(entry)

    return matched

if __name__ == '__main__':
    a = drat_fetch_causelist(
        '13/04/2026',
        'chennai'
    )

    print("fetch_causelist  output :",a)    
    a = drat_find_entries_in_cause_list(a, 'DN/1048/2024')
    print("find entreies output: " ,a)

    