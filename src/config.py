import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


BASE_URL = "https://www.panel.co.kr"
LOGIN_URL = f"{BASE_URL}/auth/login"

# 사이트(React SPA)가 로그인 토큰을 보관하는 localStorage 키.
# 값: {"accessToken": ..., "refreshToken": ..., "user": {...}}
AUTH_STORAGE_KEY = "embrain.auth.web"

PANEL_ID = os.getenv("PANEL_ID", "").strip()
PANEL_PW = os.getenv("PANEL_PW", "")

HEADLESS = _bool_env("HEADLESS", True)
CHROMIUM_PATH = os.getenv("CHROMIUM_PATH", "").strip() or None

# 터미널 없이 로그인할 때 보안문자 값(captcha.txt)을 기다리는 시간(초)
CAPTCHA_WAIT_SEC = int(os.getenv("CAPTCHA_WAIT_SEC", "").strip() or 300)

# 로그인 세션(쿠키 + localStorage 토큰). 로그인마다 보안문자를 사람이 입력해야 하므로
# 세션을 저장해 두고 재사용한다.
STATE_PATH = Path(os.getenv("STATE_PATH", "").strip() or ".auth/state.json")
# 조회 결과 스냅샷 저장 폴더
DATA_DIR = Path(os.getenv("DATA_DIR", "").strip() or "data")
