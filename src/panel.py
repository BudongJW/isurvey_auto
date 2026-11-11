"""계정 관리용 조회 — 적립금, 참여 가능 조사, 참여/적립/지급 내역.

엔드포인트와 필드 이름은 사이트 프런트엔드(마이페이지·조사참여 화면)가 쓰는 것을 따른다.
응답 원본은 raw 로 함께 보관한다.
"""
from dataclasses import asdict, dataclass, field

from src.api import PanelApi

# 조사참여 목록 정렬 (화면의 사례비순/마감순/최신순)
SURVEY_SORTS = {"pay": "payVal", "deadline": "regDt", "latest": "creDt"}
# 마이페이지 내역 조회 기간 (화면의 1/3/6/12개월)
HISTORY_MONTHS = (1, 3, 6, 12)

_SURVEY_PAGE_SIZE = 9
_HISTORY_PAGE_SIZE = 10


@dataclass
class Survey:
    id: str
    title: str
    min_pay: int | None      # 사례비(원). 범위형이면 min~max, 고정이면 같은 값
    max_pay: int | None
    has_gift: bool           # 경품 조사 여부
    platforms: list[str]     # "pc", "mobile"
    completed: bool          # 이미 참여 완료한 조사
    deadline: str = ""       # 마감일 (YYYY-MM-DD)
    minutes: int | None = None   # 예상 소요 시간(분)
    raw: dict = field(repr=False, default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def to_int(value: object) -> int | None:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def parse_survey(item: dict) -> Survey:
    """조사참여 목록의 한 항목을 정리한다 (사이트 화면과 같은 규칙)."""
    pay_type = str(item.get("payTpCd") if item.get("payTpCd") is not None else "")
    min_pay = max_pay = None
    if pay_type == "0":
        min_pay = max_pay = to_int(item.get("srvPay"))
    elif pay_type == "1":
        min_pay, max_pay = to_int(item.get("srvPaySta")), to_int(item.get("srvPay"))

    survey_type = str(item.get("srvTpCd") if item.get("srvTpCd") is not None else "")
    platforms = []
    if survey_type in ("0", "2", "3"):
        platforms.append("pc")
    if survey_type in ("1", "4", "2", "3"):
        platforms.append("mobile")

    survey_id = str(item.get("surveyId") if item.get("surveyId") is not None else "").strip()
    return Survey(
        id=survey_id or str(item.get("srvIdAlias") or ""),
        title=str(item.get("srvNm") or ""),
        min_pay=min_pay,
        max_pay=max_pay,
        has_gift=bool(str(item.get("giftNm") or "").strip()),
        platforms=platforms,
        completed=str(item.get("isCpltYn") or "").upper() == "Y",
        deadline=str(item.get("srvEndDt") or "")[:10],
        minutes=to_int(item.get("needTime")),
        raw=item,
    )


async def fetch_account_gate(api: PanelApi) -> dict:
    """계정에 처리할 일이 있는지 (휴대폰 인증·이메일 입력/확인 필요 여부)."""
    data = await api.get("/api/my/account-gate") or {}
    return {
        "phone_required": bool(data.get("phoneRequired")),
        "email_input_required": bool(data.get("emailInputRequired")),
        "email_confirm_required": bool(data.get("emailConfirmRequired")),
    }


async def fetch_points(api: PanelApi) -> dict:
    """적립금 현황(원): 총 적립 / 지급 완료 / 잔여."""
    info = (await api.post("/api/my/point/info") or {}).get("myPointInfo") or {}
    return {
        "total": to_int(info.get("totalPoint")),
        "paid": to_int(info.get("giveCpltPoint")),
        "remain": to_int(info.get("remainPoint")),
    }


async def fetch_available_surveys(
    api: PanelApi, sort: str = "latest", max_pages: int = 10
) -> tuple[list[Survey], bool]:
    """참여 가능한 온라인 조사 목록. (조사 목록, 참여 제한 패널 여부)를 돌려준다."""
    surveys: list[Survey] = []
    for page_no in range(1, max_pages + 1):
        data = await api.get(
            "/api/survey/participate/list",
            page=page_no,
            size=_SURVEY_PAGE_SIZE,
            alignColVal=SURVEY_SORTS[sort],
        ) or {}
        if data.get("badPanel"):
            return [], True
        surveys += [parse_survey(item) for item in data.get("items") or []]
        if not data.get("hasMore"):
            break
    return surveys, False


async def _fetch_history(api: PanelApi, path: str, months: int, max_pages: int, **filters) -> list[dict]:
    items: list[dict] = []
    for page_no in range(1, max_pages + 1):
        data = await api.post(
            path,
            {"page": page_no, "pageSize": _HISTORY_PAGE_SIZE, "searchMonthLimit": -months, **filters},
        ) or {}
        batch = data.get("items") or []
        items += batch
        if not batch or len(items) >= (to_int(data.get("totalCount")) or 0):
            break
    return items


async def fetch_survey_history(api: PanelApi, months: int = 3, max_pages: int = 5) -> list[dict]:
    """온라인 조사 참여 내역. 항목: entryDt, surveyId, srvNm, exitDetailCode, giftNm ..."""
    return await _fetch_history(
        api, "/api/my/survey-online/list", months, max_pages,
        srvJoinStatCd="B", searchCondition="", searchKeyword="",
    )


async def fetch_point_savings(api: PanelApi, months: int = 3, max_pages: int = 5) -> list[dict]:
    """적립 내역. 항목: saveDt, saveVal, saveRoute, surveyId, surveyNm ..."""
    return await _fetch_history(
        api, "/api/my/point/save-list", months, max_pages,
        searchCondition="", searchKeyword="",
    )


async def fetch_point_payouts(api: PanelApi, months: int = 3, max_pages: int = 5) -> list[dict]:
    """지급(현금이체·상품권·기부) 신청 내역. 항목: reqDt, giveDt, giveVal, giveStatus ..."""
    return await _fetch_history(
        api, "/api/my/point/give-list", months, max_pages,
        giveTpCd="", staDt="", endDt="",
    )


def is_participation_complete(row: dict) -> bool:
    """참여 내역이 '참여완료'인지 ('참여시도'가 아닌지) — 사이트 화면과 같은 규칙."""
    return str(row.get("exitDetailCode") or "").strip() not in ("002", "003")
