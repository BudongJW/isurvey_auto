#!/usr/bin/env bash
# keepalive 를 systemd 사용자 서비스로 설치해 상시 실행한다.
#
#   bash deploy/install-keepalive.sh [갱신주기_분]   (기본 10분)
#
# keepalive 는 이미 로그인된 세션(.auth/state.json)을 주기적으로 갱신만 한다.
# 세션이 없으면 먼저 `.venv/bin/python -m src.main login` 으로 한 번 로그인해야 한다.
set -euo pipefail

INTERVAL="${1:-10}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON="$APP_DIR/.venv/bin/python"
TEMPLATE="$SCRIPT_DIR/keepalive.service.template"
UNIT_NAME="panelpower-keepalive.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

err() { printf '오류: %s\n' "$*" >&2; exit 1; }

command -v systemctl >/dev/null 2>&1 \
  || err "systemd(systemctl)가 없습니다 — deploy/README.md 의 '非 systemd' 방법을 쓰세요"
case "$INTERVAL" in
  ''|*[!0-9.]*) err "갱신 주기(분)는 숫자여야 합니다: $INTERVAL" ;;
esac
[ -x "$PYTHON" ] \
  || err "가상환경이 없습니다: $PYTHON — 먼저 'python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium'"
[ -f "$APP_DIR/.env" ] \
  || err ".env 가 없습니다 ($APP_DIR/.env) — .env.example 을 복사해 PANEL_ID·PANEL_PW 를 채우세요"
[ -f "$TEMPLATE" ] || err "서비스 템플릿이 없습니다: $TEMPLATE"

if [ ! -f "$APP_DIR/.auth/state.json" ]; then
  printf '주의: 저장된 세션(.auth/state.json)이 없습니다.\n'
  printf '      keepalive 는 기존 세션을 갱신만 합니다 — 먼저 한 번 로그인하세요:\n'
  printf '        cd %q && .venv/bin/python -m src.main login\n\n' "$APP_DIR"
fi

mkdir -p "$UNIT_DIR"
sed -e "s#@APP_DIR@#${APP_DIR}#g" \
    -e "s#@PYTHON@#${PYTHON}#g" \
    -e "s#@INTERVAL@#${INTERVAL}#g" \
    "$TEMPLATE" > "$UNIT_PATH"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

# 로그아웃하거나 세션이 없어도 계속 돌도록 linger 를 켠다 (권한 없으면 안내만)
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  if loginctl enable-linger "$USER" 2>/dev/null; then
    printf 'linger 활성화됨 — 로그아웃해도 keepalive 가 유지됩니다.\n'
  else
    printf '참고: linger 를 켜지 못했습니다. 로그아웃 후에도 유지하려면:\n'
    printf '        sudo loginctl enable-linger %q\n' "$USER"
  fi
fi

printf '\n설치 완료: %s (갱신 주기 %s분)\n' "$UNIT_PATH" "$INTERVAL"
printf '상태 보기:  systemctl --user status %s\n' "$UNIT_NAME"
printf '로그 보기:  journalctl --user -u %s -f\n' "$UNIT_NAME"
printf '제거:       bash deploy/uninstall-keepalive.sh\n'
