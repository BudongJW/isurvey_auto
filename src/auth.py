"""패널파워 로그인/로그아웃.

로그인 폼에는 항상 보안문자(자동입력 방지문자)가 있다. 보안문자는 사람이 읽어 입력하고
(자동 인식하지 않는다), 한 번 로그인한 세션을 저장해 두고 재사용한다.

- 기본: 보안문자 이미지를 파일로 저장하고 터미널에서 값을 입력받는다.
- in_browser=True: 열린 브라우저 창에서 사용자가 직접 보안문자를 넣고 [로그인]을 누른다.
"""
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from playwright.async_api import Page, Response

from src import config
from src.api import ApiError, PanelApi, SessionExpired
from src.session import ensure_origin, goto, read_auth, save_state

logger = logging.getLogger(__name__)

ID_INPUT = "#loginId"
PW_INPUT = "#loginPassword"
CAPTCHA_INPUT = "input[name='captchaVal']"
CAPTCHA_IMAGE = ".captcha__image img"
SUBMIT_BUTTON = "form.auth-form-wrap__login button[type='submit']"

LOGIN_API_PATH = "/api/auth/login"
# 보안문자를 틀렸을 때만 다시 시도한다. 아이디/비밀번호 오류는 5회 누적 시 이용이
# 제한되므로 자동으로 재시도하지 않는다.
MAX_CAPTCHA_TRIES = 3

_FAIL_MESSAGES = {
    "invalid-captcha": "보안문자를 확인해 주세요.",
    "invalid-login": "입력하신 아이디와 비밀번호가 올바르지 않습니다.",
    "temp-panel": "이메일 인증이 필요합니다.",
}

CaptchaPrompt = Callable[[Path], Awaitable[str]]


@dataclass
class LoginOutcome:
    ok: bool
    result: str               # success / invalid-captcha / invalid-login / temp-panel / unknown
    message: str = ""
    fail_count: int | None = None   # 서버가 알려 준 누적 로그인 실패 횟수


def parse_login_response(status: int, payload: object) -> LoginOutcome:
    """/api/auth/login 응답을 해석한다 (사이트 프런트엔드와 같은 규칙)."""
    payload = payload if isinstance(payload, dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    if status == 200 and payload.get("code") == 200 and data.get("accessToken"):
        return LoginOutcome(ok=True, result="success")

    result = str(data.get("result") or payload.get("result") or "unknown")
    fail_count = None
    for raw in (data.get("loginFailCnt"), data.get("login_fail_cnt"), payload.get("loginFailCnt")):
        try:
            fail_count = max(0, int(raw))
            break
        except (TypeError, ValueError):
            continue
    message = payload.get("message") or _FAIL_MESSAGES.get(result) or f"로그인 실패 (HTTP {status})"
    return LoginOutcome(ok=False, result=result, message=str(message), fail_count=fail_count)


async def prompt_captcha(image_path: Path) -> str:
    """보안문자 이미지를 사람이 보고 터미널에 입력한다."""
    print(f"\n보안문자 이미지를 저장했습니다: {image_path.resolve()}")
    return (await asyncio.to_thread(input, "이미지에 보이는 보안문자를 입력하세요: ")).strip()


async def is_logged_in(api: PanelApi) -> bool:
    """저장된 세션이 아직 유효한지 로그인 전용 API로 확인한다 (필요하면 토큰 갱신)."""
    try:
        await api.get("/api/my/account-gate")
        return True
    except SessionExpired:
        return False


async def login(
    page: Page,
    user_id: str = "",
    password: str = "",
    *,
    in_browser: bool = False,
    ask_captcha: CaptchaPrompt = prompt_captcha,
) -> LoginOutcome:
    """로그인 폼으로 로그인하고 세션을 저장한다."""
    await _open_login_form(page)
    if user_id:
        await page.fill(ID_INPUT, user_id)

    outcome = LoginOutcome(ok=False, result="unknown")
    for attempt in range(1, MAX_CAPTCHA_TRIES + 1):
        if password:
            # 로그인에 실패하면 사이트가 비밀번호 칸을 비우므로 매번 다시 채운다
            await page.fill(PW_INPUT, password)

        if in_browser:
            logger.info("브라우저 창에서 보안문자를 입력하고 [로그인]을 눌러 주세요 (최대 5분 대기)")
            async with page.expect_response(_is_login_response, timeout=300_000) as info:
                pass
        else:
            captcha_src = await _save_captcha(page)
            await page.fill(CAPTCHA_INPUT, await ask_captcha(_captcha_path()))
            async with page.expect_response(_is_login_response, timeout=30_000) as info:
                await page.click(SUBMIT_BUTTON)

        response = await info.value
        payload = await _json(response)
        outcome = parse_login_response(response.status, payload)
        if payload is None and response.status == 200 and await _wait_tokens(page):
            # 로그인 직후 페이지가 이동하면 응답 본문을 못 읽을 수 있다 — 저장된 토큰으로 판단한다
            outcome = LoginOutcome(ok=True, result="success")
        if outcome.ok:
            await _finish_login(page)
            logger.info("로그인 성공 — 세션 저장: %s", config.STATE_PATH)
            return outcome

        fails = f" (누적 실패 {outcome.fail_count}회)" if outcome.fail_count else ""
        logger.warning(f"로그인 실패 [{outcome.result}] {outcome.message}{fails}")
        if outcome.result != "invalid-captcha":
            break
        if attempt < MAX_CAPTCHA_TRIES and not in_browser:
            await _wait_new_captcha(page, captcha_src)

    _captcha_path().unlink(missing_ok=True)
    return outcome


async def logout(page: Page, api: PanelApi) -> None:
    """서버 세션을 끊고 저장된 세션 파일을 지운다."""
    await ensure_origin(page)
    refresh_token = (await read_auth(page)).get("refreshToken")
    if refresh_token:
        try:
            await api.post("/api/auth/logout", {"refreshToken": refresh_token})
        except ApiError as e:
            logger.warning(f"서버 로그아웃 요청 실패 — 저장된 세션만 지웁니다: {e}")
    await page.evaluate("key => localStorage.removeItem(key)", config.AUTH_STORAGE_KEY)
    await page.context.clear_cookies()
    config.STATE_PATH.unlink(missing_ok=True)


def _captcha_path() -> Path:
    return config.STATE_PATH.parent / "captcha.png"


def _is_login_response(response: Response) -> bool:
    return response.request.method == "POST" and urlsplit(response.url).path == LOGIN_API_PATH


async def _json(response: Response) -> object:
    try:
        return await response.json()
    except Exception:
        return None


async def _open_login_form(page: Page) -> None:
    # 만료된 토큰이 남아 있으면 사이트가 '로그인됨'으로 보고 로그인 폼 대신 메인으로 보낸다
    await ensure_origin(page)
    await page.evaluate("key => localStorage.removeItem(key)", config.AUTH_STORAGE_KEY)
    await goto(page, config.LOGIN_URL)
    await page.locator(ID_INPUT).wait_for(state="visible", timeout=30_000)


async def _save_captcha(page: Page) -> str:
    """보안문자 이미지가 뜨면 파일로 저장하고, 그 이미지의 src 를 돌려준다."""
    image = page.locator(CAPTCHA_IMAGE)
    await image.wait_for(state="visible", timeout=30_000)
    await page.wait_for_function(
        "sel => { const el = document.querySelector(sel); return el && el.complete && el.naturalWidth > 0; }",
        arg=CAPTCHA_IMAGE,
    )
    path = _captcha_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    await image.screenshot(path=str(path))
    return await image.get_attribute("src") or ""


async def _wait_new_captcha(page: Page, previous_src: str) -> None:
    """보안문자를 틀리면 사이트가 새 이미지를 받아 온다 — 바뀔 때까지 기다린다."""
    try:
        await page.wait_for_function(
            "([sel, prev]) => { const el = document.querySelector(sel); return el && el.src !== prev; }",
            arg=[CAPTCHA_IMAGE, previous_src],
            timeout=10_000,
        )
    except Exception:
        logger.warning("새 보안문자 이미지를 확인하지 못했습니다 — 현재 이미지로 진행합니다")


async def _wait_tokens(page: Page, timeout_ms: int = 15_000) -> bool:
    """사이트가 로그인 토큰을 localStorage 에 넣을 때까지 기다린다."""
    try:
        await page.wait_for_function(
            "key => { try { return !!JSON.parse(localStorage.getItem(key) || '{}').accessToken; }"
            " catch (e) { return false; } }",
            arg=config.AUTH_STORAGE_KEY,
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


async def _finish_login(page: Page) -> None:
    """토큰이 저장되고 로그인 후처리가 끝나면 세션을 파일로 저장한다."""
    if not await _wait_tokens(page):
        raise RuntimeError("로그인 응답은 성공했지만 사이트가 토큰을 저장하지 않았습니다")
    try:
        # 로그인 직후 사이트가 붙이는 쿠키 설정용 iframe 등이 끝나도록 잠시 기다린다
        await page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass
    await save_state(page)
    _captcha_path().unlink(missing_ok=True)
