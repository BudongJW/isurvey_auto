# keepalive 상시 실행

`keepalive` 는 저장된 로그인 세션이 만료되지 않도록 주기적으로 토큰을 갱신한다.
리프레시 토큰은 30분이면 만료되고 갱신할 때마다 새 30분짜리로 교체되므로, 이보다
짧은 주기(기본 10분)로 계속 돌려 두면 **보안문자를 다시 입력하지 않고도 세션이 유지**된다.

조사 조회·관리 명령(`status`, `surveys`, `history`)은 이렇게 유지된 세션을 그대로 쓴다.

## 전제

1. 가상환경과 브라우저 준비
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/playwright install chromium
   ```
2. `.env` 에 계정 정보 설정 (`.env.example` 참고 — `PANEL_ID`, `PANEL_PW`)
3. **최초 1회 로그인** (보안문자는 사람이 입력)
   ```bash
   .venv/bin/python -m src.main login
   ```
   이 한 번으로 `.auth/state.json` 이 만들어지고, 이후 갱신은 keepalive 가 자동으로 한다.

> 브라우저 경로: `playwright install chromium` 으로 받으면 자동으로 찾는다. 다른 위치의
> Chromium/Chrome 을 쓰려면 `.env` 에 `CHROMIUM_PATH=/경로/chrome` 를 넣는다. systemd 는
> 셸 환경변수를 물려받지 않으므로, 이런 설정은 반드시 `.env` 에 둬야 한다.

## 설치 (systemd 사용자 서비스 — 권장)

루트 권한이 필요 없다.

```bash
bash deploy/install-keepalive.sh          # 갱신 주기 10분(기본)
bash deploy/install-keepalive.sh 7        # 갱신 주기 7분
```

스크립트가 하는 일:

- `.venv` · `.env` · 저장된 세션이 있는지 확인
- `~/.config/systemd/user/panelpower-keepalive.service` 생성 (저장소·venv 절대경로를 채워서)
- 서비스 활성화 및 시작
- `loginctl enable-linger` 로 **로그아웃·재부팅 후에도** 유지되게 설정 (권한이 없으면 안내만)

### 상태와 로그

```bash
systemctl --user status panelpower-keepalive.service
journalctl --user -u panelpower-keepalive.service -f
```

정상 동작 중이면 로그에 `세션 유지 중 — N분 뒤 다시 갱신` 이 주기적으로 찍힌다.

### 갱신 주기 변경

```bash
bash deploy/install-keepalive.sh 5        # 다시 설치하면 유닛을 덮어쓰고 재시작한다
```

### 제거

```bash
bash deploy/uninstall-keepalive.sh
```

## 세션이 끊겼을 때

드물게(네트워크 장기 단절, 서버 측 세션 무효화 등) 세션이 만료되면 keepalive 는
**종료 코드 3** 으로 멈춘다. 이때는 사람이 보안문자로 다시 로그인해야 한다.

```bash
.venv/bin/python -m src.main login        # 다시 로그인
bash deploy/install-keepalive.sh          # 서비스 재시작(또는 systemctl --user restart ...)
```

종료 코드 3 은 재시작해도 사람이 개입하지 않으면 또 멈추므로, 서비스는 일부러 자동
재시작하지 않는다(`RestartPreventExitStatus=3`). 끊겼을 때 **알림**을 받으려면 유닛에
`OnFailure=` 를 걸어 알림 유닛(메일·텔레그램 등)을 연결하면 된다.

## 서버 전체(root)로 설치하려면

사용자 세션이 없는 서버에서는 시스템 서비스로 두는 편이 단순하다. 위 템플릿을
`/etc/systemd/system/panelpower-keepalive.service` 로 복사하고 `[Service]` 에
`User=`(실행 계정)·`Group=` 을 추가한 뒤:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now panelpower-keepalive.service
```

`WorkingDirectory`·`ExecStart` 의 경로는 그 계정에서 접근 가능한 절대경로여야 하고,
`.env` 와 `.auth/` 도 그 계정 소유여야 한다.

## 非 systemd 환경 (fallback)

systemd 가 없으면 자동 재시작·부팅 시 시작은 안 되지만, 그냥 백그라운드로 돌릴 수는 있다.

```bash
nohup .venv/bin/python -m src.main keepalive --interval 10 >> keepalive.log 2>&1 &
```

끊기면(종료 코드 3) 프로세스가 사라지므로 로그로 확인하고 다시 로그인·실행한다.
`cron` 은 keepalive 가 '계속 도는 데몬'이라 잘 맞지 않는다 — 꼭 cron 을 써야 한다면
`@reboot` 로 위 명령을 한 번 띄우는 용도로만 쓴다.
