"""패널파워 JSON API 호출 — 로그인된 브라우저 페이지 안에서 fetch 로 부른다.

사이트 프런트엔드와 같은 방식이다.
- 인증: localStorage 의 accessToken 을 Authorization: Bearer 헤더로 보낸다.
- 401 이면 refreshToken 으로 /api/auth/refresh 를 불러 토큰을 교체하고 한 번 더 시도한다.
- 응답: {"code": 200, "message": ..., "data": ...}
"""
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import Page

from src import config
from src.session import ensure_origin


class ApiError(Exception):
    def __init__(self, path: str, status: int, code: Any, message: str):
        super().__init__(f"{path} 실패 (HTTP {status}, code {code}): {message or '응답 메시지 없음'}")
        self.status = status
        self.code = code


class SessionExpired(ApiError):
    """로그인하지 않았거나 세션이 만료됨 — 다시 로그인해야 한다."""


_FETCH_JS = """
async ({ path, method, body, storageKey }) => {
  const readAuth = () => {
    try { return JSON.parse(localStorage.getItem(storageKey) || "null") || {}; }
    catch (e) { return {}; }
  };
  const send = async (url, init) => {
    const res = await fetch(url, { credentials: "include", ...init });
    let json = null;
    try { json = await res.json(); } catch (e) {}
    return { status: res.status, json };
  };
  const jsonHeaders = { "Content-Type": "application/json;charset=UTF-8" };
  const call = (token) => send(path, {
    method,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body === null ? {} : jsonHeaders),
    },
    body: body === null ? undefined : JSON.stringify(body),
  });

  const before = readAuth();
  const first = await call(before.accessToken);
  if (first.status !== 401) return first;

  let current = readAuth();
  // 그 사이 사이트가 스스로 토큰을 갱신했다면 새 토큰으로 다시 부르기만 한다
  if (current.accessToken === before.accessToken) {
    if (!current.refreshToken) return first;
    const refreshed = await send("/api/auth/refresh", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ refreshToken: current.refreshToken }),
    });
    const data = refreshed.json && refreshed.json.data;
    const ok = refreshed.status === 200 && refreshed.json.code === 200
      && data && data.accessToken && data.refreshToken;
    if (!ok) return { ...first, refreshStatus: refreshed.status };
    current = { ...readAuth(), accessToken: data.accessToken, refreshToken: data.refreshToken };
    localStorage.setItem(storageKey, JSON.stringify(current));
  }
  return call(current.accessToken);
}
"""


class PanelApi:
    def __init__(self, page: Page):
        self.page = page

    async def get(self, path: str, **params: Any) -> Any:
        query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
        return await self._call("GET", f"{path}?{query}" if query else path, None)

    async def post(self, path: str, body: dict | None = None) -> Any:
        return await self._call("POST", path, body or {})

    async def _call(self, method: str, path: str, body: dict | None) -> Any:
        await ensure_origin(self.page)
        res = await self.page.evaluate(
            _FETCH_JS,
            {"path": path, "method": method, "body": body, "storageKey": config.AUTH_STORAGE_KEY},
        )
        status = res["status"]
        payload = res["json"] if isinstance(res["json"], dict) else {}
        code, message = payload.get("code"), payload.get("message") or ""

        if status == 401:
            # 토큰 갱신이 서버 오류(5xx)로 실패한 것은 만료가 아니라 일시 오류다
            if (res.get("refreshStatus") or 0) >= 500:
                raise ApiError(path, res["refreshStatus"], code, "토큰 갱신 중 서버 오류")
            raise SessionExpired(path, status, code, message)
        if status != 200 or code != 200:
            raise ApiError(path, status, code, message)
        return payload.get("data")
