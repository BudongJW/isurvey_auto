"""브라우저 세션 — Playwright 실행, 저장된 로그인 세션 불러오기/저장."""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import IO, AsyncIterator

from playwright.async_api import Page, async_playwright

from src import config

logger = logging.getLogger(__name__)

# 다른 실행이 세션을 쓰는 동안 기다리는 시간(초). 조회·갱신은 몇 초면 끝난다.
# 로그인(보안문자 입력 대기)처럼 오래 걸리는 실행과 겹치면 기다리다 포기한다.
LOCK_WAIT_SEC = 60


class SessionBusy(Exception):
    """다른 실행이 세션을 쓰고 있어 기다리다 포기함."""


if os.name == "nt":
    import msvcrt

    def _try_lock(file: IO[bytes]) -> bool:
        try:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(file: IO[bytes]) -> None:
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(file: IO[bytes]) -> bool:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(file: IO[bytes]) -> None:
        fcntl.flock(file, fcntl.LOCK_UN)


@asynccontextmanager
async def _session_lock(wait_sec: float | None = None) -> AsyncIterator[None]:
    """세션 파일을 한 번에 한 실행만 쓰게 한다 (불러오기 → 사용 → 저장을 통째로 잠근다).

    리프레시 토큰은 갱신할 때마다 교체된다. keepalive 와 다른 명령이 겹쳐 돌면 한쪽이
    교체 전 토큰을 나중에 저장해 세션이 끊길 수 있으므로, 앞선 실행이 끝나기를 기다린다.
    """
    wait_sec = LOCK_WAIT_SEC if wait_sec is None else wait_sec
    path = config.STATE_PATH.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as file:
        deadline = time.monotonic() + wait_sec
        waiting = False
        while not _try_lock(file):
            if time.monotonic() >= deadline:
                raise SessionBusy(
                    f"다른 실행이 세션을 사용 중입니다 ({wait_sec:g}초 대기) — 끝난 뒤 다시 실행하세요"
                )
            if not waiting:
                waiting = True
                logger.info(f"다른 실행이 세션을 사용 중입니다 — 끝나기를 기다립니다 (최대 {wait_sec:g}초)")
            await asyncio.sleep(0.2)
        try:
            yield
        finally:
            _unlock(file)


@asynccontextmanager
async def open_session(*, headless: bool | None = None) -> AsyncIterator[Page]:
    """저장된 세션이 있으면 불러온 브라우저 페이지를 연다.

    끝날 때 로그인 상태면 세션을 다시 저장한다. 리프레시 토큰은 쓸 때마다 새 값으로
    바뀌므로, 저장하지 않으면 다음 실행에서 세션이 끊긴다. 여는 동안에는 다른 실행이
    같은 세션 파일을 쓰지 못하게 잠근다.
    """
    async with _session_lock(), async_playwright() as p:
        browser = await p.chromium.launch(
            headless=config.HEADLESS if headless is None else headless,
            executable_path=config.CHROMIUM_PATH,
        )
        try:
            context = await browser.new_context(
                storage_state=_load_state(),
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            try:
                yield page
            finally:
                try:
                    if (await read_auth(page)).get("refreshToken"):
                        await save_state(page)
                except Exception as e:
                    logger.warning(f"세션 저장 실패: {_first_line(e)}")
        finally:
            await browser.close()


def _load_state() -> dict | None:
    try:
        return json.loads(config.STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning(f"세션 파일을 읽을 수 없어 새로 시작합니다 ({config.STATE_PATH}): {e}")
        return None


async def save_state(page: Page) -> None:
    """쿠키 + localStorage(토큰)를 파일로 저장한다. 본인만 읽을 수 있게 권한을 좁힌다."""
    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    await page.context.storage_state(path=str(config.STATE_PATH))
    os.chmod(config.STATE_PATH, 0o600)


async def goto(page: Page, url: str, attempts: int = 3) -> None:
    """페이지 이동. 연결이 끊기는 일시 오류는 잠시 쉬고 다시 시도한다."""
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            return
        except Exception as e:
            if attempt == attempts:
                raise
            logger.warning(f"접속 실패 {attempt}/{attempts} — 재시도: {_first_line(e)}")
            await asyncio.sleep(2 * attempt)


async def ensure_origin(page: Page) -> None:
    """localStorage 와 /api 호출은 사이트 출처에서만 가능하므로, 아니면 메인으로 이동한다."""
    if not page.url.startswith(config.BASE_URL):
        await goto(page, config.BASE_URL)


async def read_auth(page: Page) -> dict:
    """사이트가 localStorage 에 보관한 로그인 정보({accessToken, refreshToken, user})."""
    if not page.url.startswith(config.BASE_URL):
        return {}
    raw = await page.evaluate("key => localStorage.getItem(key)", config.AUTH_STORAGE_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _first_line(e: Exception) -> str:
    return (str(e).splitlines() or [type(e).__name__])[0][:200]
