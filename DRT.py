import re
from datetime import datetime
from functools import lru_cache
from typing import Any, Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .order_storage import persist_orders_to_storage as _persist_orders_to_storage

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
        "listing_date": _normalize_date(cause_date),
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

    _today_str = datetime.today().strftime("%Y-%m-%d")
    _order_dates = sorted(
        o["date"] for o in orders
        if o.get("date") and re.match(r"\d{4}-\d{2}-\d{2}", o["date"])
    )
    first_listing_date = _order_dates[0] if _order_dates else None
    _past_dates = [d for d in _order_dates if d <= _today_str]
    last_listing_date = _past_dates[-1] if _past_dates else None

    _disposal_str = _normalize_space(data.get("disposalNature"))
    _status_code = _normalize_space(data.get("status"))
    disposal_nature = 0 if (_disposal_str or _status_code == "D") else 1

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
        "first_listing_date": first_listing_date,
        "last_listing_date": last_listing_date,
        "disposal_nature": disposal_nature,
        "purpose_next": _normalize_space(data.get("nextListingPurpose")) or None,
        "case_type": _normalize_space(data.get("casetype")) or None,
        "pet_name": [petitioner] if petitioner else [],
        "res_name": [respondent] if respondent else [],
        "advocates": advocates,
        "judges": None,
        "history": proceedings,
        "acts": [],
        "orders": orders,
        "ia_details": ia_details,
        "connected_matters": [],
        "application_appeal_matters": [],
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


def _parse_filing_no(filing_no: str) -> dict[str, str | None]:
    """Extract diary_no and diary_year from a 15-digit DRT/DRAT filing number.

    Format: <5-digit court code> + <6-digit zero-padded diary no> + <4-digit year>
    e.g. "070110005432019" → {"diaryno": "543", "diaryyear": "2019"}
         "071090023872025" → {"diaryno": "2387", "diaryyear": "2025"}
    """
    s = (filing_no or "").strip()
    if len(s) == 15 and s.isdigit():
        diary_no = str(int(s[5:11]))
        diary_year = s[11:15]
        return {"diaryno": diary_no, "diaryyear": diary_year}
    return {"diaryno": None, "diaryyear": None}


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
    if tribunal_type == TRIBUNAL_TYPE_DRAT:
        # DRAT has no filing-no-wise lookup endpoint; extract diary info from the
        # 15-digit filing number (<5-char court code><6-digit diary no><4-digit year>)
        # and go directly to the diary-number endpoint.
        stub = _parse_filing_no(filing_no)
        data = _fetch_rich_case_detail(drt_id, stub, tribunal_type=tribunal_type)
    else:
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
