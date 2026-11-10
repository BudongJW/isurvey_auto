"""패널파워 로그인/로그아웃.

로그인 폼에는 항상 보안문자(자동입력 방지문자)가 있다. 보안문자는 사람이 읽어 입력하고
(자동 인식하지 않는다), 한 번 로그인한 세션을 저장해 두고 재사용한다.

- 기본: 보안문자 이미지를 파일로 저장하고 터미널에서 값을 입력받는다.
  터미널이 없으면(백그라운드·원격 실행) 이미지 옆 captcha.txt 에 값이 적히기를 기다린다.
- in_browser=True: 열린 브라우저 창에서 사용자가 직접 보안문자를 넣고 [로그인]을 누른다.
"""
import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from playwright.async_api import Page, Request
from playwright.async_api import TimeoutError as PlaywrightTimeout

from src import config
from src.api import ApiError, PanelApi, SessionExpired
from src.session import ensure_origin, goto, read_auth, save_state

logger = logging.getLogger(__name__)

ID_INPUT = "#loginId"
PW_INPUT = "#loginPassword"
CAPTCHA_INPUT = "input[name='captchaVal']"
CAPTCHA_IMAGE = ".captcha__image img"
LOGIN_FORM = "form.auth-form-wrap__login"
SUBMIT_BUTTON = f"{LOGIN_FORM} button[type='submit']"

LOGIN_API_PATH = "/api/auth/login"
# 보안문자를 틀렸을 때만 다시 시도한다. 아이디/비밀번호 오류는 5회 누적 시 이용이
# 제한되므로 자동으로 재시도하지 않는다.
MAX_CAPTCHA_TRIES = 3

# [로그인]을 누른 뒤 로그인 요청이 나가기를 기다리는 시간. 값이 비었거나 형식이 틀리면
# (아이디 4~12자, 비밀번호 10~20자) 브라우저나 사이트가 막아 요청이 나가지 않는다.
SUBMIT_WAIT_MS = 10_000
RESPONSE_WAIT_SEC = 30
IN_BROWSER_WAIT_MS = 300_000

_FAIL_MESSAGES = {
    "invalid-captcha": "보안문자를 확인해 주세요.",
    "invalid-login": "입력하신 아이디와 비밀번호가 올바르지 않습니다.",
    "temp-panel": "이메일 인증이 필요합니다.",
}

CaptchaPrompt = Callable[[Path], Awaitable[str]]


@dataclass
class LoginOutcome:
    ok: bool
    # success / invalid-captcha / invalid-login / temp-panel / unknown
    # not-submitted: 사이트가 입력값을 받아들이지 않아 로그인 요청 자체가 나가지 않음
    # timeout: 브라우저 창에서 제한 시간 안에 로그인하지 않음 / no-response: 응답을 받지 못함
    result: str
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


class LoginAborted(Exception):
    """계정 정보나 보안문자 값을 받지 못해 로그인 요청을 보내지 않고 중단함."""


async def prompt_captcha(image_path: Path) -> str:
    """보안문자 이미지를 사람이 보고 입력한다 — 터미널, 터미널이 없으면 captcha.txt 파일로."""
    if sys.stdin.isatty():
        print(f"\n보안문자 이미지를 저장했습니다: {image_path.resolve()}")
        return (await asyncio.to_thread(input, "이미지에 보이는 보안문자를 입력하세요: ")).strip()

    answer_path = image_path.with_suffix(".txt")
    answer_path.unlink(missing_ok=True)
    logger.info(
        f"보안문자 이미지: {image_path.resolve()} — 값을 {answer_path.resolve()} 에 적어 주세요"
        f" (최대 {config.CAPTCHA_WAIT_SEC}초 대기)"
    )
    for _ in range(config.CAPTCHA_WAIT_SEC * 2):
        try:
            answer = answer_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            answer = ""
        if answer:
            answer_path.unlink(missing_ok=True)
            return answer
        await asyncio.sleep(0.5)
    raise LoginAborted(f"보안문자 값을 받지 못했습니다 ({answer_path})")


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

        captcha_src = ""
        if in_browser:
            logger.info("브라우저 창에서 보안문자를 입력하고 [로그인]을 눌러 주세요 (최대 5분 대기)")
        else:
            captcha_src = await _save_captcha(page)
            await page.fill(CAPTCHA_INPUT, await ask_captcha(_captcha_path()))

        request = await _submit(page, in_browser)
        if request is None:
            outcome = await _not_submitted(page, in_browser)
        else:
            outcome = await _read_outcome(page, request)
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


def _is_login_request(request: Request) -> bool:
    return request.method == "POST" and urlsplit(request.url).path == LOGIN_API_PATH


async def _submit(page: Page, in_browser: bool) -> Request | None:
    """[로그인]을 누르고(창에서 직접 입력할 때는 사용자가 누르기를 기다리고) 나간 로그인 요청을 돌려준다.

    제한 시간 안에 요청이 나가지 않으면 None.
    """
    clicked = False
    try:
        timeout = IN_BROWSER_WAIT_MS if in_browser else SUBMIT_WAIT_MS
        async with page.expect_request(_is_login_request, timeout=timeout) as info:
            if not in_browser:
                await page.click(SUBMIT_BUTTON)
            clicked = True
        return await info.value
    except PlaywrightTimeout:
        if not clicked:
            raise       # 버튼을 누르지도 못한 것은 다른 문제다
        return None


async def _read_outcome(page: Page, request: Request) -> LoginOutcome:
    """보낸 로그인 요청의 응답을 기다려 해석한다."""
    try:
        response = await asyncio.wait_for(request.response(), RESPONSE_WAIT_SEC)
    except Exception:
        response = None
    if response is None:
        return LoginOutcome(ok=False, result="no-response",
                            message="로그인 응답을 받지 못했습니다 — 네트워크 상태를 확인하고 다시 시도하세요")
    try:
        payload = await response.json()
    except Exception:
        payload = None
    if payload is None and response.status == 200 and await _wait_tokens(page):
        # 로그인 직후 페이지가 이동하면 응답 본문을 못 읽을 수 있다 — 저장된 토큰으로 판단한다
        return LoginOutcome(ok=True, result="success")
    return parse_login_response(response.status, payload)


_FORM_PROBLEM_JS = """
sel => {
  const form = document.querySelector(sel);
  if (!form) return "";
  // 빈 칸·짧은 값은 브라우저가 먼저 막는다 (required, minlength) — 이때는 사이트 안내문이 뜨지 않는다
  const invalid = form.querySelector("input:invalid");
  if (invalid) {
    const label = invalid.id ? form.querySelector(`label[for="${invalid.id}"]`) : null;
    const name = (label ? label.textContent : invalid.name || "").trim() || "입력";
    if (invalid.validity.valueMissing) return `${name} 칸이 비어 있습니다`;
    if (invalid.validity.tooShort) {
      return `${name} 칸은 ${invalid.minLength}자 이상이어야 합니다 (현재 ${invalid.value.length}자)`;
    }
    return `${name}: ${invalid.validationMessage}`;
  }
  const notice = form.querySelector(".login-fail-msg, p.box__info[role='alert']");
  return notice ? notice.textContent.trim() : "";
}
"""


async def _not_submitted(page: Page, in_browser: bool) -> LoginOutcome:
    """로그인 요청이 나가지 않은 까닭을 폼에서 읽어 온다."""
    if in_browser:
        return LoginOutcome(ok=False, result="timeout",
                            message="제한 시간 안에 브라우저 창에서 로그인하지 않았습니다")
    try:
        reason = await page.evaluate(_FORM_PROBLEM_JS, LOGIN_FORM)
    except Exception:
        reason = ""
    message = reason or "사이트가 로그인 요청을 보내지 않았습니다 — 아이디(4~12자)·비밀번호(10~20자)를 확인하세요"
    return LoginOutcome(ok=False, result="not-submitted", message=message)


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
