"""패널파워 계정 관리 CLI.

    python -m src.main login [--headed] [--force]   보안문자를 직접 입력해 로그인, 세션 저장
    python -m src.main status                       적립금 + 참여 가능 조사 (기본)
    python -m src.main surveys [--sort pay|deadline|latest]
    python -m src.main history [--months 1|3|6|12]  참여/적립/지급 내역
    python -m src.main keepalive [--interval 분]    세션이 만료되지 않게 주기적으로 토큰 갱신
    python -m src.main logout
"""
import argparse
import asyncio
import getpass
import logging
import sys

from playwright.async_api import Page

from src import auth, config, panel, report
from src.api import ApiError, PanelApi
from src.session import SessionBusy, ensure_origin, open_session, read_auth

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOGIN_REQUIRED = 3


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m src.main", description="패널파워 계정 관리")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("login", help="로그인하고 세션을 저장한다")
    p.add_argument("--headed", action="store_true", help="브라우저 창을 띄워 보안문자를 창에서 직접 입력")
    p.add_argument("--force", action="store_true", help="세션이 유효해도 다시 로그인")

    sub.add_parser("status", help="적립금과 참여 가능 조사 요약 (기본)")

    p = sub.add_parser("surveys", help="참여 가능 조사 목록")
    p.add_argument("--sort", choices=list(panel.SURVEY_SORTS), default="latest",
                   help="pay=사례비순, deadline=마감순, latest=최신순")

    p = sub.add_parser("history", help="조사 참여 / 적립 / 지급 내역")
    p.add_argument("--months", type=int, choices=panel.HISTORY_MONTHS, default=3)

    p = sub.add_parser("keepalive", help="저장된 세션이 만료되지 않게 주기적으로 갱신한다 (Ctrl+C 로 종료)")
    p.add_argument("--interval", type=float, default=10, metavar="분",
                   help="갱신 주기(분). 리프레시 토큰 수명이 30분이라 그보다 충분히 짧아야 한다 (기본 10)")

    sub.add_parser("logout", help="로그아웃하고 저장된 세션을 지운다")

    args = parser.parse_args(argv)
    args.command = args.command or "status"
    return args


def _credentials(in_browser: bool) -> tuple[str, str]:
    """환경변수의 계정 정보. 없으면 터미널에서 묻는다 (브라우저 입력 모드에서는 묻지 않음)."""
    user_id, password = config.PANEL_ID, config.PANEL_PW
    if in_browser:
        return user_id, password
    if not (user_id and password) and not sys.stdin.isatty():
        raise auth.LoginAborted(
            "아이디/비밀번호가 없고 입력받을 터미널도 없습니다 — "
            "PANEL_ID, PANEL_PW 를 .env 나 환경변수로 설정하세요"
        )
    if not user_id:
        user_id = input("패널파워 아이디: ").strip()
    if not password:
        password = getpass.getpass("패널파워 비밀번호: ")
    return user_id, password


async def _login(page: Page, in_browser: bool) -> bool:
    user_id, password = await asyncio.to_thread(_credentials, in_browser)
    return (await auth.login(page, user_id, password, in_browser=in_browser)).ok


async def _status(page: Page, api: PanelApi) -> None:
    user = (await read_auth(page)).get("user") or {}
    gate = await panel.fetch_account_gate(api)
    points = await panel.fetch_points(api)
    surveys, bad_panel = await panel.fetch_available_surveys(api)
    previous_ids = report.previous_survey_ids()

    print(report.render_status(user, gate, points, surveys, bad_panel, previous_ids))
    path = report.save_snapshot("status", {
        "user_id": user.get("userId"),
        "gate": gate,
        "points": points,
        "bad_panel": bad_panel,
        "surveys": [s.to_dict() for s in surveys if not s.completed],
    })
    logger.info(f"스냅샷 저장: {path}")


async def _surveys(api: PanelApi, sort: str) -> None:
    surveys, bad_panel = await panel.fetch_available_surveys(api, sort=sort)
    print(report.render_surveys(surveys, bad_panel))


async def _history(api: PanelApi, months: int) -> None:
    participations = await panel.fetch_survey_history(api, months)
    savings = await panel.fetch_point_savings(api, months)
    payouts = await panel.fetch_point_payouts(api, months)

    print(report.render_history(months, participations, savings, payouts))
    path = report.save_snapshot("history", {
        "months": months,
        "participations": participations,
        "savings": savings,
        "payouts": payouts,
    })
    logger.info(f"스냅샷 저장: {path}")


async def _wait(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _keepalive(interval_min: float) -> int:
    """저장된 세션을 주기적으로 갱신한다.

    리프레시 토큰은 발급 후 30분이면 만료되고, 갱신할 때마다 새 30분짜리로 교체된다.
    그래서 30분 넘게 아무 호출이 없으면 보안문자를 넣고 다시 로그인해야 한다.
    매번 브라우저를 새로 열어 확인하고, 닫을 때 교체된 토큰이 파일에 저장된다.
    """
    while True:
        try:
            async with open_session() as page:
                alive = await auth.is_logged_in(PanelApi(page))
        except (ApiError, SessionBusy) as e:
            # 일시 오류는 다음 주기에 다시 시도한다 (주기가 수명보다 충분히 짧아야 하는 이유)
            logger.warning(f"세션 확인 실패 — 다음 주기에 재시도: {e}")
        else:
            if not alive:
                logger.error("세션이 만료되었습니다 — `python -m src.main login` 으로 다시 로그인하세요")
                return EXIT_LOGIN_REQUIRED
            logger.info(f"세션 유지 중 — {interval_min:g}분 뒤 다시 갱신")
        await _wait(interval_min * 60)


async def run(args: argparse.Namespace) -> int:
    if args.command == "keepalive":
        return await _keepalive(args.interval)

    in_browser = args.command == "login" and args.headed
    async with open_session(headless=False if in_browser else None) as page:
        api = PanelApi(page)
        await ensure_origin(page)

        if args.command == "logout":
            await auth.logout(page, api)
            logger.info("로그아웃 완료 — 저장된 세션을 지웠습니다")
            return EXIT_OK

        logged_in = await auth.is_logged_in(api)

        if args.command == "login":
            if logged_in and not args.force:
                logger.info("이미 로그인되어 있습니다 (다시 로그인하려면 --force)")
                return EXIT_OK
            return EXIT_OK if await _login(page, in_browser) else EXIT_FAILED

        if not logged_in:
            # 보안문자는 사람이 입력해야 하므로, 입력받을 터미널이 없으면 여기서 멈춘다
            if not sys.stdin.isatty():
                logger.error("로그인이 필요합니다 — 먼저 `python -m src.main login` 을 실행하세요")
                return EXIT_LOGIN_REQUIRED
            logger.info("저장된 세션이 없거나 만료되었습니다 — 로그인을 진행합니다")
            if not await _login(page, in_browser=False):
                return EXIT_FAILED

        if args.command == "status":
            await _status(page, api)
        elif args.command == "surveys":
            await _surveys(api, args.sort)
        elif args.command == "history":
            await _history(api, args.months)
        return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return asyncio.run(run(_parse_args(argv)))
    except (ApiError, auth.LoginAborted, SessionBusy) as e:
        logger.error(str(e))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
