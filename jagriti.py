import base64
import logging
import re
import time
from urllib.parse import urlencode
from typing import Any, Dict, Optional

import ddddocr
import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

try:
    from .order_storage import persist_orders_to_storage as _persist_orders_to_storage
except ImportError:
    from order_storage import persist_orders_to_storage as _persist_orders_to_storage

logger = logging.getLogger(__name__)

BASE_URL = "https://e-jagriti.gov.in/services"
CASE_SERVICE_URL = f"{BASE_URL}/case/caseFilingService/v2"
AUTH_SERVICE_URL = f"{BASE_URL}/user/auth/v2"
MASTER_SERVICE_URL = f"{BASE_URL}/master/master/v2"
JUDGEMENT_SERVICE_URL = f"{BASE_URL}/courtmaster/courtRoom/judgement/v1"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://e-jagriti.gov.in/case-history-case-status",
}


def _unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _clean_captcha_text(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (value or "")).strip()


def _build_daily_order_url(
    filing_reference_number: str | int,
    date_of_hearing: str,
    order_type_id: int = 1,
) -> str:
    return (
        f"{JUDGEMENT_SERVICE_URL}/getDailyOrderJudgementPdf?"
        + urlencode(
            {
                "filingReferenceNumber": str(filing_reference_number).strip(),
                "dateOfHearing": (date_of_hearing or "").strip(),
                "orderTypeId": order_type_id,
            }
        )
    )


def _inline_pdf_data_url(document_base64: str) -> str:
    return f"data:application/pdf;base64,{document_base64}"


class JagritiService:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._ocr: Optional[ddddocr.DdddOcr] = None

    def _get_ocr(self) -> ddddocr.DdddOcr:
        if self._ocr is None:
            self._ocr = ddddocr.DdddOcr(show_ad=False)
        return self._ocr

    def _raise_for_application_error(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        message = str(payload.get("message") or "").strip()
        status = payload.get("status")
        error = payload.get("error")
        if error in (False, "false", None) and (status in (200, "200", None) or not message):
            return
        lowered = message.lower()
        if "captcha" in lowered:
            raise ValueError(message or "Captcha verification failed")
        if status and str(status) not in {"200", "201"} and message:
            raise ValueError(message)

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
    )
    def generate_captcha(self) -> Dict[str, Any]:
        response = self.session.get(f"{AUTH_SERVICE_URL}/generateCaptcha", timeout=30)
        response.raise_for_status()
        payload = response.json()
        self._raise_for_application_error(payload)
        data = _unwrap_data(payload) or {}
        if not isinstance(data, dict) or not data.get("base64Image"):
            raise ValueError("Captcha response did not include an image")
        return data

    def solve_captcha(self, max_attempts: int = 5) -> str:
        last_error: Optional[Exception] = None
        for _ in range(max_attempts):
            try:
                captcha_data = self.generate_captcha()
                image_bytes = base64.b64decode(captcha_data["base64Image"])
                solved = _clean_captcha_text(self._get_ocr().classification(image_bytes))
                if len(solved) >= 4:
                    return solved
            except Exception as exc:  # pragma: no cover - network/OCR variability
                last_error = exc
            time.sleep(0.5)
        if last_error:
            raise ValueError("Unable to solve e-Jagriti captcha") from last_error
        raise ValueError("Unable to solve e-Jagriti captcha")

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=6),
    )
    def verify_captcha(self, captcha: str) -> bool:
        cleaned = _clean_captcha_text(captcha)
        if not cleaned:
            raise ValueError("captcha is required")
        response = self.session.post(
            f"{AUTH_SERVICE_URL}/verifyCaptcha",
            json={"captcha": cleaned, "onlyVerifyCaptcha": False},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        message = str(payload.get("message") or "")
        if response.status_code == 200 and "successful" in message.lower():
            return True
        self._raise_for_application_error(payload)
        return False

    def ensure_verified(self, captcha: Optional[str] = None) -> str:
        provided = _clean_captcha_text(captcha or "")
        if provided:
            if not self.verify_captcha(provided):
                raise ValueError("Captcha verification failed")
            return provided

        last_error: Optional[Exception] = None
        for _ in range(5):
            solved = self.solve_captcha()
            try:
                if self.verify_captcha(solved):
                    return solved
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)

        if last_error:
            raise ValueError("Captcha verification failed") from last_error
        raise ValueError("Captcha verification failed")

    @staticmethod
    def _build_case_status_params(identifier: str, commission_id: Optional[int] = None) -> Dict[str, Any]:
        token = (identifier or "").strip().upper()
        if not token:
            raise ValueError("identifier is required")

        params: Dict[str, Any] = {}
        if token.startswith("A"):
            params["fileApplicationNumber"] = token
        elif "/" in token:
            params["caseNumber"] = token
        elif token.isdigit():
            params["filingReferenceNumber"] = int(token)
        else:
            params["caseNumber"] = token

        if commission_id:
            params["commissionId"] = commission_id
        return params

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=6),
    )
    def get_case_status(
        self,
        identifier: str,
        commission_id: Optional[int] = None,
        captcha: Optional[str] = None,
        verify: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if verify:
            self.ensure_verified(captcha)
        response = self.session.get(
            f"{CASE_SERVICE_URL}/getCaseStatus",
            params=self._build_case_status_params(identifier, commission_id),
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_application_error(payload)
        data = _unwrap_data(payload)
        return data if isinstance(data, dict) and data.get("fillingReferenceNumber") else data

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=6),
    )
    def get_case_history(
        self,
        case_number: str,
        captcha: Optional[str] = None,
        verify: bool = True,
    ) -> list[dict]:
        token = (case_number or "").strip().upper()
        if not token:
            raise ValueError("case_number is required")
        if verify:
            self.ensure_verified(captcha)
        response = self.session.get(
            f"{CASE_SERVICE_URL}/getCaseDetailForHistoryDashBoard",
            params={"caseNumber": token},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_application_error(payload)
        data = _unwrap_data(payload)
        return data if isinstance(data, list) else []

    def get_case_status_with_history(
        self,
        identifier: str,
        commission_id: Optional[int] = None,
        captcha: Optional[str] = None,
        verify: bool = True,
    ) -> Dict[str, Any]:
        solved_captcha = self.ensure_verified(captcha) if verify else None
        status = self.get_case_status(
            identifier,
            commission_id=commission_id,
            captcha=solved_captcha,
            verify=False,
        )
        history: list[dict] = []
        case_number = None
        if isinstance(status, dict):
            case_number = status.get("caseNumber")
        if case_number:
            history = self.get_case_history(case_number, captcha=solved_captcha, verify=False)
        return {
            "case_status": status,
            "case_history": history,
            "captcha_used": solved_captcha,
        }

    @staticmethod
    def create_free_text_payload(
        commission_id: int,
        free_text: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        date_request_type: int = 0,
        page: int = 0,
        size: int = 30,
        order_type: int = 2,
    ) -> Dict[str, Any]:
        return {
            "commissionId": commission_id,
            "page": page,
            "size": size,
            "fromDate": from_date,
            "toDate": to_date,
            "dateRequestType": date_request_type,
            "serchType": 8,
            "serchTypeValue": (free_text or "").strip().upper(),
            "judgeId": None,
            "orderType": order_type,
        }

    @staticmethod
    def create_advanced_payload(
        commission_id: int,
        search_type: int,
        search_value: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        date_request_type: int = 0,
        judge_id: str = "",
        page: int = 0,
        size: int = 30,
        order_type: int = 2,
    ) -> Dict[str, Any]:
        return {
            "commissionId": commission_id,
            "page": page,
            "size": size,
            "fromDate": from_date,
            "toDate": to_date,
            "dateRequestType": date_request_type,
            "serchType": search_type,
            "serchTypeValue": (search_value or "").strip().upper(),
            "judgeId": judge_id or "",
            "orderType": order_type,
        }

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=6),
    )
    def search_case_details(
        self,
        payload: Dict[str, Any],
        captcha: Optional[str] = None,
        verify: bool = True,
    ) -> list[dict]:
        if verify:
            self.ensure_verified(captcha)
        response = self.session.post(
            f"{CASE_SERVICE_URL}/getCaseDetailsBySearchType",
            json=payload,
            timeout=45,
        )
        response.raise_for_status()
        body = response.json()
        self._raise_for_application_error(body)
        data = _unwrap_data(body)
        return data if isinstance(data, list) else []

    def get_commissions(self) -> list[dict]:
        response = self.session.get(f"{MASTER_SERVICE_URL}/getAllCommission", timeout=30)
        response.raise_for_status()
        payload = response.json()
        self._raise_for_application_error(payload)
        data = _unwrap_data(payload)
        return data if isinstance(data, list) else []

    def get_districts(self, state_id: int) -> list[dict]:
        response = self.session.get(
            f"{MASTER_SERVICE_URL}/getAllDistrict",
            params={"stateId": state_id},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_application_error(payload)
        data = _unwrap_data(payload)
        return data if isinstance(data, list) else []

    def get_commissions_v2(self) -> list[dict]:
        """
        Fetch all commissions including NCDRC, SCDRCs, and DCDRCs.
        Note: DCDRCs are fetched by iterating through states and districts.
        CommissionId formula: 11000000 + (state_id * 10000) + district_id
        """
        all_commissions = self.get_commissions()
        # NCDRC and SCDRCs are already in all_commissions.
        # SCDRCs have commissionId in 11XX0000 format where XX is stateId.

        # Extract state codes from SCDRCs
        # To avoid massive overhead, we only fetch districts for states we find.
        states = {}
        for comm in all_commissions:
            sid = comm.get("stateId")
            if sid is not None and sid > 0:
                states[sid] = comm.get("commissionNameEn")

        # NCDRC is usually stateId 0 or missing.

        # For each state, fetch districts and add as DCDRCs
        for state_id, state_name in states.items():
            try:
                districts = self.get_districts(state_id)
                for dist in districts:
                    district_id = dist.get("districtId")
                    district_name = dist.get("districtNameEn")
                    if district_id:
                        # Construct commissionId for DCDRC using the formula:
                        # 11000000 (base) + (state_id * 10000) + district_id
                        commission_id = 11000000 + (state_id * 10000) + district_id
                        all_commissions.append({
                            "commissionId": str(commission_id),
                            "commissionNameEn": f"{district_name} DCDRC",
                            "stateId": state_id,
                            "districtId": district_id,
                            "is_dcdrc": True
                        })
            except Exception as e:
                # Log error but continue processing other states
                logger.warning(f"Failed to fetch districts for state {state_id}: {e}")
                continue

        return all_commissions

    @retry(
        retry=retry_if_exception_type((requests.RequestException, ValueError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=6),
    )
    def get_daily_order_judgement_pdf(
        self,
        filing_reference_number: str | int,
        date_of_hearing: str,
        order_type_id: int = 1,
    ) -> Dict[str, Any]:
        filing_reference = str(filing_reference_number).strip()
        hearing_date = (date_of_hearing or "").strip()
        if not filing_reference:
            raise ValueError("filing_reference_number is required")
        if not hearing_date:
            raise ValueError("date_of_hearing is required")

        response = self.session.get(
            f"{JUDGEMENT_SERVICE_URL}/getDailyOrderJudgementPdf",
            params={
                "filingReferenceNumber": filing_reference,
                "dateOfHearing": hearing_date,
                "orderTypeId": order_type_id,
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_application_error(payload)
        data = _unwrap_data(payload)
        if not isinstance(data, dict):
            return {"raw": data}

        document = (
            data.get("dailyOrderPdf")
            or data.get("judgmentPdf")
            or data.get("dailyOrderJudgementPdf")
            or data.get("pdfBase64")
        )
        if not document:
            return {
                "document": None,
                "document_type": None,
                "filename": data.get("fileName"),
                "content_type": data.get("contentType"),
                "raw": data,
            }

        lowered = document.lstrip().lower()
        document_type = "html" if lowered.startswith("<!doctype html") or lowered.startswith("<html") else "base64"
        return {
            "document": document,
            "document_type": document_type,
            "filename": data.get("fileName"),
            "content_type": data.get("contentType"),
            "raw": data,
        }

    @staticmethod
    def extract_orders_from_case_status(case_status: Dict[str, Any]) -> list[dict]:
        if not isinstance(case_status, dict):
            return []

        filing_reference_number = case_status.get("fillingReferenceNumber")
        hearings = case_status.get("caseHearingDetails") or []
        orders: list[dict] = []

        for hearing in hearings:
            if not isinstance(hearing, dict):
                continue

            hearing_date = (hearing.get("dateOfHearing") or "").strip()
            order_type_id = hearing.get("orderTypeId") or 1
            inline_document = hearing.get("judgmentOrderDocumentBase64")
            has_public_order = hearing.get("dailyOrderAvailabilityStatus") in {1, 2, "1", "2"}

            document_url = None
            if inline_document:
                document_url = _inline_pdf_data_url(inline_document)
            elif filing_reference_number and hearing_date and has_public_order:
                document_url = _build_daily_order_url(
                    filing_reference_number=filing_reference_number,
                    date_of_hearing=hearing_date,
                    order_type_id=order_type_id,
                )

            if not document_url:
                continue

            next_hearing = (hearing.get("dateOfNextHearing") or "").strip()
            court_room = hearing.get("courtRoomName")
            description_parts = ["Daily order/judgement"]
            if court_room:
                description_parts.append(f"Court Room {court_room}")
            if next_hearing:
                description_parts.append(f"Next hearing {next_hearing}")

            source_document_url = None
            if filing_reference_number and hearing_date:
                source_document_url = _build_daily_order_url(
                    filing_reference_number=filing_reference_number,
                    date_of_hearing=hearing_date,
                    order_type_id=order_type_id,
                )

            orders.append(
                {
                    "date": hearing_date or None,
                    "description": " | ".join(description_parts),
                    "document_url": document_url,
                    "source_document_url": source_document_url or document_url,
                    "order_type_id": order_type_id,
                    "court_room_name": court_room,
                    "next_hearing_date": next_hearing or None,
                }
            )

        return orders


_service = JagritiService()


def get_jagriti_commissions():
    """
    Fetch all e-Jagriti commissions including NCDRC, SCDRCs, and DCDRCs.
    """
    return _service.get_commissions_v2()


def get_jagriti_districts(state_id: int):
    return _service.get_districts(state_id)


def get_jagriti_case_status(
    identifier: str,
    commission_id: Optional[int] = None,
    captcha: Optional[str] = None,
    verify: bool = True,
):
    return _service.get_case_status(identifier, commission_id=commission_id, captcha=captcha, verify=verify)


def get_jagriti_case_history(
    case_number: str,
    captcha: Optional[str] = None,
    verify: bool = True,
):
    return _service.get_case_history(case_number, captcha=captcha, verify=verify)


def get_jagriti_case_status_with_history(
    identifier: str,
    commission_id: Optional[int] = None,
    captcha: Optional[str] = None,
    verify: bool = True,
):
    return _service.get_case_status_with_history(
        identifier,
        commission_id=commission_id,
        captcha=captcha,
        verify=verify,
    )


def jagriti_search_case_details(
    payload: Dict[str, Any],
    captcha: Optional[str] = None,
    verify: bool = True,
):
    return _service.search_case_details(payload, captcha=captcha, verify=verify)


def get_jagriti_daily_order_judgement_pdf(
    filing_reference_number: str | int,
    date_of_hearing: str,
    order_type_id: int = 1,
):
    return _service.get_daily_order_judgement_pdf(
        filing_reference_number,
        date_of_hearing=date_of_hearing,
        order_type_id=order_type_id,
    )


def extract_jagriti_orders_from_case_status(case_status: Dict[str, Any]) -> list[dict]:
    return _service.extract_orders_from_case_status(case_status)


def _fetch_order_document(order_url: str, referer: Optional[str] = None) -> requests.Response:
    if order_url.startswith("data:application/pdf;base64,"):
        encoded = order_url.split(",", 1)[1]
        response = requests.Response()
        response.status_code = 200
        response._content = base64.b64decode(encoded)
        response.headers["content-type"] = "application/pdf"
        response.url = order_url
        return response

    headers = {}
    if referer:
        headers["Referer"] = referer
    return _service.session.get(order_url, timeout=45, headers=headers)


async def persist_orders_to_storage(
    orders: list[dict] | None,
    case_id: str | None = None,
) -> list[dict] | None:
    return await _persist_orders_to_storage(
        orders,
        case_id=case_id,
        fetch_fn=_fetch_order_document,
        referer="https://e-jagriti.gov.in/",
    )
