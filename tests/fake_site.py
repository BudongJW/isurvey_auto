"""테스트용 가짜 패널파워 — 실제 사이트에 접속하지 않고 브라우저 요청을 가로채 응답한다.

로그인 폼의 셀렉터, 토큰 저장 방식(localStorage), API 응답 형식은 실제 사이트와 같게 흉내 낸다.
"""
import json
import struct
import zlib
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import BrowserContext, Route

from src import config

USER_ID = "tester01"
PASSWORD = "correct-horse-1!"
CAPTCHA = "12345"

_LOGIN_PAGE = """<!doctype html><html lang="ko"><body>
<form class="auth-form-wrap auth-form-wrap__login">
  <input id="loginId" name="loginId" type="text">
  <input id="loginPassword" name="loginPassword" type="password">
  <div class="captcha"><div class="captcha__image"><span class="img-wrap"></span></div>
    <input type="text" name="captchaVal"></div>
  <button type="submit">로그인</button>
</form>
<script>
const KEY = %s;
if (localStorage.getItem(KEY)) location.replace("/");   // 로그인 상태면 메인으로 보낸다
async function loadCaptcha() {
  const blob = await (await fetch("/api/auth/captcha?t=" + Date.now())).blob();
  const img = document.createElement("img");
  img.width = 200; img.height = 80; img.src = URL.createObjectURL(blob);
  document.querySelector(".img-wrap").replaceChildren(img);
}
loadCaptcha();
document.querySelector("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json;charset=UTF-8" },
    body: JSON.stringify({
      userId: form.loginId.value, password: form.loginPassword.value, captchaVal: form.captchaVal.value,
    }),
  });
  const json = await res.json();
  form.captchaVal.value = "";
  if (res.status === 200 && json.code === 200) {
    const d = json.data;
    localStorage.setItem(KEY, JSON.stringify({
      accessToken: d.accessToken, refreshToken: d.refreshToken, user: { userId: d.userId, name: d.name },
    }));
    location.assign("/");
    return;
  }
  form.loginPassword.value = "";
  if (json.data.result === "invalid-captcha") loadCaptcha();
});
</script></body></html>""" % json.dumps(config.AUTH_STORAGE_KEY)

_HOME_PAGE = "<!doctype html><html lang='ko'><body><div id='root'>패널파워</div></body></html>"


def _png(width: int = 20, height: int = 10) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = (b"\x00" + b"\x00" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _survey(n: int, **extra) -> dict:
    return {"surveyId": 1000 + n, "srvNm": f"조사 {n}", "payTpCd": "0", "srvPay": 500 * n,
            "srvTpCd": "2", "isCpltYn": "N", **extra}


class FakePanelSite:
    def __init__(self):
        self.token_no = 0
        self.access_token = ""
        self.refresh_token = ""
        self.calls: list[str] = []          # "METHOD /path" 호출 기록
        self.surveys = [_survey(n) for n in range(1, 12)]   # 11건 → 9건 + 2건 두 페이지

    async def install(self, context: BrowserContext) -> None:
        await context.route(f"{config.BASE_URL}/**", self._handle)

    def expire_access_token(self) -> None:
        """서버 쪽 액세스 토큰만 바꿔, 브라우저가 가진 토큰이 만료된 것처럼 만든다."""
        self.access_token = "expired-on-server"

    def count(self, call: str) -> int:
        return self.calls.count(call)

    def _issue_tokens(self) -> dict:
        self.token_no += 1
        self.access_token = f"access-{self.token_no}"
        self.refresh_token = f"refresh-{self.token_no}"
        return {"accessToken": self.access_token, "refreshToken": self.refresh_token}

    async def _handle(self, route: Route) -> None:
        request = route.request
        url = urlsplit(request.url)
        if not url.path.startswith("/api/"):
            html = _LOGIN_PAGE if url.path == "/auth/login" else _HOME_PAGE
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            return

        self.calls.append(f"{request.method} {url.path}")
        if url.path == "/api/auth/captcha":
            await route.fulfill(status=200, content_type="image/png", body=_png())
            return

        body = json.loads(request.post_data or "{}") if request.method == "POST" else {}
        status, payload = self._api(request.method, url.path, parse_qs(url.query), body,
                                    request.headers.get("authorization", ""))
        await route.fulfill(status=status, content_type="application/json",
                            body=json.dumps(payload, ensure_ascii=False))

    def _api(self, method: str, path: str, query: dict, body: dict, authorization: str):
        ok = lambda data: (200, {"code": 200, "message": "OK", "data": data})  # noqa: E731

        if path == "/api/auth/login":
            if body.get("captchaVal") != CAPTCHA:
                return 400, {"code": 400, "message": "보안문자를 확인해 주세요.",
                             "data": {"result": "invalid-captcha", "loginFailCnt": 0}}
            if body.get("userId") != USER_ID or not body.get("password"):
                return 400, {"code": 400, "data": {"result": "invalid-login", "loginFailCnt": 1}}
            return ok({**self._issue_tokens(), "userId": USER_ID, "name": "테스터"})

        if path == "/api/auth/refresh":
            if not self.refresh_token or body.get("refreshToken") != self.refresh_token:
                return 401, {"code": 401, "message": "만료된 토큰"}
            return ok(self._issue_tokens())

        if not self.access_token or authorization != f"Bearer {self.access_token}":
            return 401, {"code": 401, "message": "로그인이 필요합니다."}

        if path == "/api/auth/logout":
            self.access_token = self.refresh_token = ""
            return ok(None)
        if path == "/api/my/account-gate":
            return ok({"phoneRequired": False, "emailInputRequired": False, "emailConfirmRequired": True})
        if path == "/api/my/point/info":
            return ok({"myPointInfo": {"totalPoint": 15300, "giveCpltPoint": 10000, "remainPoint": 5300}})
        if path == "/api/survey/participate/list":
            page, size = int(query["page"][0]), int(query["size"][0])
            start = (page - 1) * size
            return ok({"items": self.surveys[start:start + size],
                       "hasMore": start + size < len(self.surveys), "badPanel": False})
        if path == "/api/my/survey-online/list":
            return ok({"totalCount": 2, "items": [
                {"entryDt": "2026-11-03T10:00:00", "surveyId": 1001, "srvNm": "조사 1", "exitDetailCode": "001"},
                {"entryDt": "2026-11-01T09:00:00", "surveyId": 990, "srvNm": "지난 조사", "exitDetailCode": "002"},
            ]})
        if path == "/api/my/point/save-list":
            return ok({"totalCount": 1, "items": [
                {"saveDt": "2026-11-03", "saveVal": 500, "saveRoute": "온라인조사", "surveyNm": "조사 1"}]})
        if path == "/api/my/point/give-list":
            return ok({"totalCount": 0, "items": []})
        return 404, {"code": 404, "message": f"가짜 사이트에 없는 API: {method} {path}"}
