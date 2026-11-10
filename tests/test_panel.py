"""오프라인 테스트 — tests/fake_site.py 의 가짜 사이트를 상대로 실제 브라우저를 돌린다.

    pip install -r requirements-dev.txt && playwright install chromium
    pytest
"""
import asyncio
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from src import auth, config, main, panel, session
from src.api import PanelApi
from tests.fake_site import CAPTCHA, PASSWORD, USER_ID, FakePanelSite


@pytest.fixture
def site(tmp_path, monkeypatch):
    """세션·스냅샷을 임시 폴더에 두고, 모든 브라우저 세션을 가짜 사이트에 연결한다."""
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "auth" / "state.json")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "HEADLESS", True)
    monkeypatch.setattr(config, "PANEL_ID", USER_ID)
    monkeypatch.setattr(config, "PANEL_PW", PASSWORD)
    fake = FakePanelSite()

    @asynccontextmanager
    async def open_fake_session(**kwargs):
        async with session.open_session(**kwargs) as page:
            await fake.install(page.context)
            yield page

    monkeypatch.setattr(main, "open_session", open_fake_session)
    fake.open_session = open_fake_session
    return fake


def answers(*values: str):
    """보안문자 입력을 흉내 낸다 — 받은 이미지 파일이 실제 PNG 인지도 확인한다."""
    queue = list(values)

    async def ask(image_path: Path) -> str:
        assert image_path.read_bytes().startswith(b"\x89PNG")
        return queue.pop(0)

    return ask


def saved_tokens() -> dict:
    state = json.loads(config.STATE_PATH.read_text(encoding="utf-8"))
    items = {i["name"]: i["value"] for o in state["origins"] for i in o["localStorage"]}
    return json.loads(items[config.AUTH_STORAGE_KEY])


def run_cli(*argv: str) -> int:
    return asyncio.run(main.run(main._parse_args(list(argv))))


async def _login(site, *captchas: str, password: str = PASSWORD) -> auth.LoginOutcome:
    async with site.open_session() as page:
        return await auth.login(page, USER_ID, password, ask_captcha=answers(*captchas))


# ─── 응답 해석 (브라우저 불필요) ───

def test_parse_login_response():
    ok = auth.parse_login_response(200, {"code": 200, "data": {"accessToken": "a", "refreshToken": "r"}})
    assert ok.ok and ok.result == "success"

    captcha = auth.parse_login_response(400, {"message": "보안문자 오류", "data": {"result": "invalid-captcha"}})
    assert (captcha.ok, captcha.result, captcha.message) == (False, "invalid-captcha", "보안문자 오류")

    wrong = auth.parse_login_response(400, {"data": {"result": "invalid-login", "loginFailCnt": "3"}})
    assert (wrong.result, wrong.fail_count) == ("invalid-login", 3)
    assert "올바르지 않습니다" in wrong.message

    assert auth.parse_login_response(502, None).result == "unknown"


def test_parse_survey():
    fixed = panel.parse_survey({"surveyId": 77, "srvNm": "고정", "payTpCd": "0", "srvPay": "1,500", "srvTpCd": "1"})
    assert (fixed.id, fixed.min_pay, fixed.max_pay, fixed.platforms) == ("77", 1500, 1500, ["mobile"])

    ranged = panel.parse_survey({"srvIdAlias": "abc", "payTpCd": 1, "srvPaySta": 300, "srvPay": 900,
                                 "srvTpCd": 2, "giftNm": "상품권", "isCpltYn": "y"})
    assert (ranged.id, ranged.min_pay, ranged.max_pay) == ("abc", 300, 900)
    assert ranged.platforms == ["pc", "mobile"] and ranged.has_gift and ranged.completed


# ─── 로그인 ───

def test_login_retries_wrong_captcha_and_saves_session(site):
    outcome = asyncio.run(_login(site, "00000", CAPTCHA))

    assert outcome.ok
    assert site.count("POST /api/auth/login") == 2
    assert saved_tokens()["refreshToken"] == site.refresh_token
    assert not (config.STATE_PATH.parent / "captcha.png").exists()


def test_wrong_password_is_not_retried(site):
    outcome = asyncio.run(_login(site, CAPTCHA, CAPTCHA, password=""))

    assert (outcome.ok, outcome.result, outcome.fail_count) == (False, "invalid-login", 1)
    assert site.count("POST /api/auth/login") == 1     # 계정 잠금(5회)을 피하려고 한 번만 시도
    assert not config.STATE_PATH.exists()


def test_login_replaces_stale_session(site):
    asyncio.run(_login(site, CAPTCHA))
    site.access_token = site.refresh_token = ""        # 서버에서 세션이 완전히 만료됨

    assert asyncio.run(_login(site, CAPTCHA)).ok       # 남은 토큰 때문에 폼이 안 뜨면 여기서 멈춘다
    assert saved_tokens()["refreshToken"] == site.refresh_token


def test_captcha_answer_file_when_no_terminal(site, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    answer_path = config.STATE_PATH.parent / "captcha.txt"

    async def login_with_answer_file():
        async with site.open_session() as page:
            task = asyncio.create_task(auth.login(page, USER_ID, PASSWORD))
            while not (config.STATE_PATH.parent / "captcha.png").exists():
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)                   # 이미지가 저장된 뒤에 사람이 값을 적는다
            answer_path.write_text(CAPTCHA + "\n", encoding="utf-8")
            return await task

    assert asyncio.run(login_with_answer_file()).ok
    assert not answer_path.exists()


# ─── 세션 재사용 ───

def test_saved_session_is_reused_and_expired_token_refreshed(site):
    asyncio.run(_login(site, CAPTCHA))
    site.expire_access_token()

    async def fetch_points():
        async with site.open_session() as page:
            return await panel.fetch_points(PanelApi(page))

    assert asyncio.run(fetch_points()) == {"total": 15300, "paid": 10000, "remain": 5300}
    assert site.count("POST /api/auth/refresh") == 1
    assert site.count("POST /api/auth/login") == 1
    # 교체된 리프레시 토큰이 저장돼야 다음 실행에서도 세션이 이어진다
    assert saved_tokens()["refreshToken"] == site.refresh_token == "refresh-2"


# ─── CLI ───

def test_cli_requires_login_when_no_terminal(site, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    assert run_cli("status") == main.EXIT_LOGIN_REQUIRED
    assert site.count("POST /api/auth/login") == 0


def test_cli_status_history_logout(site, capsys):
    asyncio.run(_login(site, CAPTCHA))
    site.surveys[0]["isCpltYn"] = "Y"

    assert run_cli("status") == main.EXIT_OK
    out = capsys.readouterr().out
    assert "회원: 테스터 (tester01)" in out
    assert "잔여 5,300원 | 총 적립 15,300원 | 지급 완료 10,000원" in out
    assert "확인 필요: 이메일 확인" in out
    assert "참여 가능 조사 10건" in out and "신규" not in out     # 두 페이지(9+2) 중 완료 1건 제외
    assert "[1011] 조사 11 — 5,500원 · PC/모바일" in out
    snapshot = json.loads((config.DATA_DIR / "status.json").read_text(encoding="utf-8"))
    assert len(snapshot["surveys"]) == 10 and snapshot["points"]["remain"] == 5300

    site.surveys.append({"surveyId": 2000, "srvNm": "새 조사", "payTpCd": "1", "srvPaySta": 100,
                         "srvPay": 300, "srvTpCd": "1", "giftNm": "경품"})
    assert run_cli("status") == main.EXIT_OK
    out = capsys.readouterr().out
    assert "신규 1건" in out
    assert " + [2000] 새 조사 — 100원~300원 · 모바일 · 경품" in out

    assert run_cli("history", "--months", "6") == main.EXIT_OK
    out = capsys.readouterr().out
    assert "2026-11-03  [1001] 조사 1 — 참여완료" in out
    assert "2026-11-01  [990] 지난 조사 — 참여시도" in out
    assert "2026-11-03  500원  온라인조사  조사 1" in out
    assert "지급 내역이 없습니다." in out

    assert run_cli("logout") == main.EXIT_OK
    assert not config.STATE_PATH.exists() and site.refresh_token == ""
