#!/usr/bin/env bash
# keepalive systemd 사용자 서비스를 중지하고 제거한다.
#
#   bash deploy/uninstall-keepalive.sh
set -euo pipefail

UNIT_NAME="panelpower-keepalive.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

command -v systemctl >/dev/null 2>&1 || { printf 'systemctl 이 없습니다 — 할 일이 없습니다.\n'; exit 0; }

systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
rm -f "$UNIT_PATH"
systemctl --user daemon-reload 2>/dev/null || true

printf '제거 완료: %s\n' "$UNIT_NAME"
printf '로그아웃해도 계속 돌게 켰던 linger 를 끄려면(다른 사용자 서비스가 없을 때만):\n'
printf '        loginctl disable-linger %q\n' "$USER"
