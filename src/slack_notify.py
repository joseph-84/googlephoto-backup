"""
매일 아침 8시 크론에 의해 실행되며,
전날 새벽 2시 백업이 생성한 stats 파일을 읽어 슬랙으로 전송합니다.
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

LOGS_DIR = Path(os.environ.get("LOGS_DIR", "/app/logs"))
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")


def send_slack(message: str):
    if not SLACK_WEBHOOK_URL:
        print("[슬랙] SLACK_WEBHOOK_URL 미설정 — 알림 건너뜀")
        return
    payload = {"text": message}
    if SLACK_CHANNEL:
        payload["channel"] = SLACK_CHANNEL
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json=payload,
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"[슬랙] 전송 실패: {resp.status_code} {resp.text}")
    else:
        print("[슬랙] 알림 전송 완료")


def format_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def build_message(stats: dict, stats_date: date) -> str:
    date_str = stats_date.strftime("%Y년 %m월 %d일")
    total = stats.get("total", 0)
    success = stats.get("success", 0)
    failed = stats.get("failed", 0)
    skipped = stats.get("skipped", 0)
    freed_bytes = stats.get("freed_bytes", 0)
    per_user = stats.get("per_user", {})

    lines = [
        f"*📸 구글 포토 백업 리포트 — {date_str}*",
        "",
        f"• 총 처리: *{total}* 장",
        f"• ✅ 성공: *{success}* 장",
        f"• ❌ 실패: *{failed}* 장",
        f"• ⏭ 스킵(중복): *{skipped}* 장",
        f"• 💾 확보된 용량 추정: *{format_bytes(freed_bytes)}*",
        "",
        "*계정별 상세*",
    ]
    for uid, info in per_user.items():
        freed = format_bytes(info.get("freed_bytes", 0))
        lines.append(
            f"  - `{uid}`: 성공 {info.get('success', 0)} / 실패 {info.get('failed', 0)} / 용량 {freed}"
        )

    if failed > 0:
        lines += ["", "⚠️ 실패 항목이 있습니다. `logs/backup_*.log` 를 확인하세요."]

    return "\n".join(lines)


def main():
    today = date.today()
    # 아침 8시에 실행 → 새벽 2시에 생성된 당일 stats 파일을 읽음
    stats_file = LOGS_DIR / f"stats_{today.strftime('%Y-%m-%d')}.json"

    if not stats_file.exists():
        # 전날 파일도 확인
        yesterday = today - timedelta(days=1)
        stats_file = LOGS_DIR / f"stats_{yesterday.strftime('%Y-%m-%d')}.json"

    if not stats_file.exists():
        send_slack(f"⚠️ *구글 포토 백업* — {today} stats 파일을 찾을 수 없습니다.")
        sys.exit(0)

    with open(stats_file) as f:
        stats = json.load(f)

    stats_date_str = stats_file.stem.replace("stats_", "")
    stats_date = date.fromisoformat(stats_date_str)
    message = build_message(stats, stats_date)
    send_slack(message)


if __name__ == "__main__":
    main()
