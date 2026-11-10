"""조회 결과 출력과 스냅샷 저장."""
import json
import os
from datetime import datetime
from pathlib import Path

from src import config
from src.panel import Survey, is_participation_complete, to_int

_GATE_LABELS = {
    "phone_required": "휴대폰 인증",
    "email_input_required": "이메일 입력",
    "email_confirm_required": "이메일 확인",
}


def won(value: object) -> str:
    return f"{value:,}원" if isinstance(value, int) else "-"


def day(value: object) -> str:
    return str(value or "")[:10] or "-"


def save_snapshot(name: str, payload: dict) -> Path:
    """조회 결과를 data/<name>.json 으로 저장한다 (개인정보이므로 본인만 읽기)."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DATA_DIR / f"{name}.json"
    payload = {"fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"), **payload}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def previous_survey_ids() -> set[str] | None:
    """직전 status 스냅샷에 있던 조사 ID. 스냅샷이 없으면 None (첫 실행)."""
    try:
        data = json.loads((config.DATA_DIR / "status.json").read_text(encoding="utf-8"))
        return {str(s["id"]) for s in data["surveys"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _survey_line(survey: Survey, is_new: bool = False) -> str:
    if survey.min_pay is None and survey.max_pay is None:
        pay = "사례비 미표기"
    elif survey.min_pay == survey.max_pay:
        pay = won(survey.max_pay)
    else:
        pay = f"{won(survey.min_pay)}~{won(survey.max_pay)}"
    tags = [pay]
    if survey.platforms:
        tags.append("/".join("PC" if p == "pc" else "모바일" for p in survey.platforms))
    if survey.has_gift:
        tags.append("경품")
    if survey.completed:
        tags.append("참여완료")
    mark = "+" if is_new else " "
    return f" {mark} [{survey.id or '-'}] {survey.title} — {' · '.join(tags)}"


def render_surveys(surveys: list[Survey], bad_panel: bool, previous_ids: set[str] | None = None) -> str:
    if bad_panel:
        return "참여 가능 조사: 조회 불가 — 조사 참여가 제한된 계정입니다 (사이트에서 확인 필요)"
    open_surveys = [s for s in surveys if not s.completed]
    new_ids = set() if previous_ids is None else {s.id for s in open_surveys} - previous_ids
    header = f"참여 가능 조사 {len(open_surveys)}건"
    if previous_ids is not None:
        header += f" (지난 조회 이후 신규 {len(new_ids)}건, '+' 표시)"
    lines = [header] + [_survey_line(s, s.id in new_ids) for s in surveys]
    return "\n".join(lines)


def render_status(
    user: dict, gate: dict, points: dict,
    surveys: list[Survey], bad_panel: bool, previous_ids: set[str] | None,
) -> str:
    lines = [f"=== 패널파워 계정 현황 ({datetime.now():%Y-%m-%d %H:%M}) ==="]
    if user:
        lines.append(f"회원: {user.get('name') or '-'} ({user.get('userId') or '-'})")
    lines.append(
        f"적립금: 잔여 {won(points.get('remain'))} | 총 적립 {won(points.get('total'))}"
        f" | 지급 완료 {won(points.get('paid'))}"
    )
    todo = [label for key, label in _GATE_LABELS.items() if gate.get(key)]
    if user.get("tempPassword"):
        todo.append("임시 비밀번호 변경")
    if user.get("needsProfileUpdate"):
        todo.append("회원정보 갱신")
    if todo:
        lines.append(f"확인 필요: {', '.join(todo)} — 사이트에서 처리해 주세요")
    lines += ["", render_surveys(surveys, bad_panel, previous_ids)]
    return "\n".join(lines)


def _section(title: str, rows: list[str], empty: str) -> list[str]:
    return [f"[{title}] {len(rows)}건"] + (rows or [f"  {empty}"]) + [""]


def render_history(months: int, participations: list[dict], savings: list[dict], payouts: list[dict]) -> str:
    lines = [f"=== 최근 {months}개월 내역 ===", ""]
    lines += _section("조사 참여", [
        f"  {day(r.get('entryDt'))}  [{r.get('surveyId') or '-'}] {r.get('srvNm') or ''}"
        f" — {'참여완료' if is_participation_complete(r) else '참여시도'}"
        for r in participations
    ], "참여 내역이 없습니다.")
    lines += _section("적립", [
        f"  {day(r.get('saveDt'))}  {won(to_int(r.get('saveVal')))}  {r.get('saveRoute') or ''}"
        f"  {r.get('surveyNm') or ''}".rstrip()
        for r in savings
    ], "적립 내역이 없습니다.")
    lines += _section("지급 신청", [
        f"  {day(r.get('reqDt') or r.get('giveDt'))}  {won(to_int(r.get('giveVal')))}"
        f"  {r.get('giveStatus') or ''}".rstrip()
        for r in payouts
    ], "지급 내역이 없습니다.")
    return "\n".join(lines).rstrip()

