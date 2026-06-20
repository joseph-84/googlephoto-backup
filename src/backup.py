"""
Google Photos → Google Drive 자동 이관 백업 엔진.

파이프라인 (계정별 순서):
  1. 로컬 SQLite DB 로 이미 처리된 photoId 즉시 스킵 (API 호출 없음)
  2. Photos API 로 365일 이전 사진 조회 (오래된 순, 페이지네이션)
  3. 사진 다운로드 → Drive 업로드 ([userId]_filename, description=photoId)
  4. Drive 파일 소유권을 드라이브 오너로 이전 (추가 사용자만)
  5. DB 에 처리 완료 기록 → 다음 실행 시 즉시 스킵
  6. 실패 시 Drive 파일 롤백 + DB 미기록 (다음 날 재시도)

중복/재시작 처리:
  - processed_ids.db (SQLite) 가 단일 진실 공급원
  - Drive API 중복 조회 없음 → API 할당량 대폭 절감
  - 10만 장 중 9만 장 완료 상태에서 재실행 시 9만 장은 DB 조회만으로 즉시 스킵
"""

import io
import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

load_dotenv()

# ── 환경 설정 ─────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/config/users.json"))
AUTH_DIR = Path(os.environ.get("AUTH_DIR", "/auth"))
LOGS_DIR = Path(os.environ.get("LOGS_DIR", "/app/logs"))
DB_PATH = Path(os.environ.get("DB_PATH", "/app/logs/processed_ids.db"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
MAX_ITEMS = int(os.environ.get("MAX_ITEMS_PER_RUN", "500"))

PHOTOS_SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary",
    "https://www.googleapis.com/auth/photoslibrary.readonly",
]
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
ALL_SCOPES = list(set(PHOTOS_SCOPES + DRIVE_SCOPES))

TODAY = date.today()
CUTOFF_DATE = TODAY - timedelta(days=365)

# ── 로거 설정 ─────────────────────────────────────────────────────────────────
LOGS_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOGS_DIR / f"backup_{TODAY.strftime('%Y-%m-%d')}.log"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("backup")


# ── SQLite 체크포인트 DB ───────────────────────────────────────────────────────
class QuotaExceededError(Exception):
    """Google API 할당량 초과 — 오늘은 더 이상 진행 불가."""


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed (
            photo_id      TEXT NOT NULL,
            user_id       TEXT NOT NULL,
            drive_file_id TEXT NOT NULL,
            processed_at  TEXT NOT NULL,
            PRIMARY KEY (photo_id, user_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_id ON processed(photo_id)")
    # 페이지 토큰 체크포인트: 사용자별로 마지막으로 읽던 Photos API 페이지 토큰 저장
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_cursor (
            user_id    TEXT PRIMARY KEY,
            page_token TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()


@contextmanager
def open_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def db_is_processed(conn: sqlite3.Connection, photo_id: str, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed WHERE photo_id = ? AND user_id = ?",
        (photo_id, user_id),
    ).fetchone()
    return row is not None


def db_mark_done(
    conn: sqlite3.Connection, photo_id: str, user_id: str, drive_file_id: str
):
    conn.execute(
        """
        INSERT OR REPLACE INTO processed (photo_id, user_id, drive_file_id, processed_at)
        VALUES (?, ?, ?, datetime('now', 'localtime'))
        """,
        (photo_id, user_id, drive_file_id),
    )
    conn.commit()


def db_count(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM processed WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0]


def db_save_cursor(conn: sqlite3.Connection, user_id: str, page_token: Optional[str]):
    """현재 페이지 토큰 저장 — None 이면 처음부터 다시 시작."""
    conn.execute(
        """
        INSERT OR REPLACE INTO page_cursor (user_id, page_token, updated_at)
        VALUES (?, ?, datetime('now', 'localtime'))
        """,
        (user_id, page_token),
    )
    conn.commit()


def db_load_cursor(conn: sqlite3.Connection, user_id: str) -> Optional[str]:
    """저장된 페이지 토큰 로드. 없으면 None (처음부터)."""
    row = conn.execute(
        "SELECT page_token FROM page_cursor WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["page_token"] if row else None


def db_clear_cursor(conn: sqlite3.Connection, user_id: str):
    """사용자 페이지 커서 초기화 (전체 완료 시)."""
    conn.execute("DELETE FROM page_cursor WHERE user_id = ?", (user_id,))
    conn.commit()


# ── 인증 헬퍼 ─────────────────────────────────────────────────────────────────
def _resolve_auth_path(path_str: str) -> Path:
    p = Path(path_str)
    if p.exists():
        return p
    return AUTH_DIR / p.name


def get_credentials(user_cfg: dict) -> Credentials:
    token_file = _resolve_auth_path(user_cfg["token_file"])

    if not token_file.exists():
        raise FileNotFoundError(
            f"토큰 파일 없음: {token_file}. "
            f"먼저 'python src/auth_setup.py --user {user_cfg['id']}' 를 실행하세요."
        )

    creds = Credentials.from_authorized_user_file(str(token_file), ALL_SCOPES)
    if creds.expired and creds.refresh_token:
        log.info("[%s] 토큰 갱신 중...", user_cfg["id"])
        creds.refresh(Request())
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return creds


# ── Google Photos API (REST) ──────────────────────────────────────────────────
PHOTOS_BASE = "https://photoslibrary.googleapis.com/v1"


def photos_search(session: requests.Session, page_token: Optional[str] = None) -> dict:
    """CUTOFF_DATE 이전 사진 목록을 오래된 순으로 조회 (페이지당 100건).

    429 / 할당량 초과 응답 시 QuotaExceededError 발생.
    """
    body = {
        "pageSize": 100,
        "filters": {
            "dateFilter": {
                "ranges": [
                    {
                        "startDate": {"year": 2000, "month": 1, "day": 1},
                        "endDate": {
                            "year": CUTOFF_DATE.year,
                            "month": CUTOFF_DATE.month,
                            "day": CUTOFF_DATE.day,
                        },
                    }
                ]
            },
            "mediaTypeFilter": {"mediaTypes": ["PHOTO"]},
        },
        "orderBy": "MediaMetadata.creation_time",
    }
    if page_token:
        body["pageToken"] = page_token
    resp = session.post(f"{PHOTOS_BASE}/mediaItems:search", json=body)

    if resp.status_code == 429 or (
        resp.status_code == 403
        and "quota" in resp.text.lower()
    ):
        raise QuotaExceededError(f"Photos API 할당량 초과: {resp.status_code} {resp.text[:200]}")

    resp.raise_for_status()
    return resp.json()


def make_photos_session(creds: Credentials) -> requests.Session:
    sess = requests.Session()
    sess.headers.update({"Authorization": f"Bearer {creds.token}"})
    return sess


# ── Google Drive API ──────────────────────────────────────────────────────────
def build_drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _check_quota(e: HttpError):
    """Drive HttpError 가 할당량 초과이면 QuotaExceededError 로 변환."""
    if e.resp.status in (429, 403) and (
        "quota" in str(e).lower() or "rateLimitExceeded" in str(e)
    ):
        raise QuotaExceededError(f"Drive API 할당량 초과: {e}") from e


def drive_upload(
    drive_svc,
    folder_id: str,
    user_id: str,
    filename: str,
    photo_id: str,
    content: bytes,
    mime_type: str,
) -> str:
    """Drive 에 파일 업로드 후 Drive 파일 ID 반환."""
    file_metadata = {
        "name": f"{user_id}_{filename}",
        "parents": [folder_id],
        "description": photo_id,  # 수동 확인용 (중복 체크는 DB로)
    }
    media = MediaIoBaseUpload(
        io.BytesIO(content),
        mimetype=mime_type or "image/jpeg",
        resumable=True,
    )
    try:
        result = (
            drive_svc.files()
            .create(body=file_metadata, media_body=media, fields="id,name")
            .execute()
        )
    except HttpError as e:
        _check_quota(e)
        raise
    return result["id"]


def drive_transfer_ownership(drive_svc, file_id: str, owner_email: str):
    try:
        drive_svc.permissions().create(
            fileId=file_id,
            transferOwnership=True,
            body={"role": "owner", "type": "user", "emailAddress": owner_email},
            sendNotificationEmail=False,
        ).execute()
    except HttpError as e:
        _check_quota(e)
        raise


def drive_delete(drive_svc, file_id: str):
    """Drive 파일 삭제 (롤백용)."""
    try:
        drive_svc.files().delete(fileId=file_id).execute()
    except HttpError as e:
        log.error("롤백 중 Drive 삭제 실패 (fileId=%s): %s", file_id, e)


# ── 통계 집계 ─────────────────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.total = 0
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.freed_bytes = 0
        self.per_user: dict[str, dict] = {}

    def init_user(self, uid: str):
        self.per_user.setdefault(uid, {"success": 0, "failed": 0, "freed_bytes": 0})

    def record_success(self, uid: str, size_bytes: int):
        self.total += 1
        self.success += 1
        self.freed_bytes += size_bytes
        self.per_user[uid]["success"] += 1
        self.per_user[uid]["freed_bytes"] += size_bytes

    def record_failed(self, uid: str):
        self.total += 1
        self.failed += 1
        self.per_user[uid]["failed"] += 1

    def record_skipped(self):
        self.total += 1
        self.skipped += 1

    def save(self):
        stats_file = LOGS_DIR / f"stats_{TODAY.strftime('%Y-%m-%d')}.json"
        data = {
            "date": TODAY.isoformat(),
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "skipped": self.skipped,
            "freed_bytes": self.freed_bytes,
            "per_user": self.per_user,
        }
        with open(stats_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.info("통계 저장: %s", stats_file)


# ── 단일 사용자 백업 ──────────────────────────────────────────────────────────
def backup_user(
    user_cfg: dict,
    owner_email: str,
    folder_id: str,
    stats: Stats,
    db_conn: sqlite3.Connection,
) -> bool:
    """
    단일 사용자 백업 실행.
    반환값: True = 정상 완료 또는 MAX_ITEMS 도달, False = 할당량 초과로 중단.
    """
    uid = user_cfg["id"]
    stats.init_user(uid)
    is_owner = user_cfg["email"] == owner_email

    already_done = db_count(db_conn, uid)
    saved_cursor = db_load_cursor(db_conn, uid)

    log.info("=" * 60)
    log.info(
        "[%s] 백업 시작 (email=%s, DB 기완료=%d건, 저장된 커서=%s)",
        uid, user_cfg["email"], already_done,
        "있음" if saved_cursor else "없음(처음부터)",
    )

    try:
        creds = get_credentials(user_cfg)
    except FileNotFoundError as e:
        log.error("[%s] 인증 오류 — 이 계정은 건너뜁니다: %s", uid, e)
        return True

    photos_sess = make_photos_session(creds)
    drive_svc = build_drive_service(creds)

    newly_uploaded = 0
    page_token: Optional[str] = saved_cursor
    page_num = 0

    while True:
        if MAX_ITEMS > 0 and newly_uploaded >= MAX_ITEMS:
            log.info("[%s] MAX_ITEMS_PER_RUN(%d) 도달 — 커서 저장 후 종료", uid, MAX_ITEMS)
            # 현재 page_token 저장: 내일 이 페이지부터 재개
            db_save_cursor(db_conn, uid, page_token)
            break

        # ── Photos API 페이지 조회 ─────────────────────────────────────────
        try:
            result = photos_search(photos_sess, page_token)
        except QuotaExceededError as e:
            log.warning("[%s] Photos API 할당량 초과 — 커서 저장 후 오늘 중단: %s", uid, e)
            db_save_cursor(db_conn, uid, page_token)
            return False  # 호출자에게 할당량 초과 신호
        except requests.HTTPError as e:
            log.error("[%s] Photos 검색 오류: %s", uid, e)
            db_save_cursor(db_conn, uid, page_token)
            break

        items = result.get("mediaItems", [])
        if not items:
            log.info("[%s] 모든 사진 처리 완료!", uid)
            db_clear_cursor(db_conn, uid)  # 완전히 끝났으므로 커서 초기화
            break

        page_num += 1
        db_skipped_in_page = 0

        for item in items:
            if MAX_ITEMS > 0 and newly_uploaded >= MAX_ITEMS:
                break

            photo_id = item["id"]
            filename = item.get("filename", f"{photo_id}.jpg")
            mime_type = item.get("mimeType", "image/jpeg")
            creation_time = item.get("mediaMetadata", {}).get("creationTime", "")

            # ── DB 체크: API 호출 없이 즉시 스킵 ─────────────────────────────
            if db_is_processed(db_conn, photo_id, uid):
                db_skipped_in_page += 1
                stats.record_skipped()
                continue

            log.info("[%s] 처리 중: %s (created=%s)", uid, filename, creation_time)

            # ── 사진 다운로드 ──────────────────────────────────────────────────
            download_url = item.get("baseUrl", "") + "=d"
            try:
                dl_resp = photos_sess.get(download_url, timeout=120)
                if dl_resp.status_code == 429:
                    log.warning("[%s] 다운로드 429 — 커서 저장 후 중단", uid)
                    db_save_cursor(db_conn, uid, page_token)
                    return False
                dl_resp.raise_for_status()
                content = dl_resp.content
                actual_size = len(content)
            except QuotaExceededError:
                db_save_cursor(db_conn, uid, page_token)
                return False
            except Exception as e:
                log.error("[%s] 다운로드 실패 (%s): %s — 다음 항목으로", uid, filename, e)
                stats.record_failed(uid)
                continue

            # ── Drive 업로드 ──────────────────────────────────────────────────
            drive_file_id = None
            try:
                drive_file_id = drive_upload(
                    drive_svc, folder_id, uid, filename, photo_id, content, mime_type
                )
            except QuotaExceededError as e:
                log.warning("[%s] Drive API 할당량 초과 — 커서 저장 후 중단: %s", uid, e)
                db_save_cursor(db_conn, uid, page_token)
                return False
            except HttpError as e:
                log.error("[%s] Drive 업로드 실패 (%s): %s", uid, filename, e)
                stats.record_failed(uid)
                continue

            # ── 소유권 이전 (추가 사용자 → 오너) ─────────────────────────────
            if not is_owner:
                try:
                    drive_transfer_ownership(drive_svc, drive_file_id, owner_email)
                except QuotaExceededError as e:
                    log.warning("[%s] Drive 할당량 초과(소유권이전) — 롤백 후 중단: %s", uid, e)
                    drive_delete(drive_svc, drive_file_id)
                    db_save_cursor(db_conn, uid, page_token)
                    return False
                except HttpError as e:
                    log.error("[%s] 소유권 이전 실패 (%s): %s — Drive 롤백", uid, filename, e)
                    drive_delete(drive_svc, drive_file_id)
                    stats.record_failed(uid)
                    continue

            # ── DB 기록 (성공 확정) ────────────────────────────────────────────
            db_mark_done(db_conn, photo_id, uid, drive_file_id)
            stats.record_success(uid, actual_size)
            newly_uploaded += 1

            log.info(
                "[%s] ✅ %s (%.1f MB) — 오늘 %d건 / 누적 %d건",
                uid, filename, actual_size / 1024 / 1024,
                newly_uploaded, db_count(db_conn, uid),
            )
            time.sleep(0.3)

        if db_skipped_in_page > 0:
            log.debug("[%s] 페이지 %d: DB 스킵 %d건", uid, page_num, db_skipped_in_page)

        page_token = result.get("nextPageToken")
        if not page_token:
            log.info("[%s] 마지막 페이지 도달 — 완료", uid)
            db_clear_cursor(db_conn, uid)
            break

    log.info(
        "[%s] 종료 — 오늘 업로드 %d건 | 누적 완료 %d건",
        uid, newly_uploaded, db_count(db_conn, uid),
    )
    return True


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    log.info("▶ 백업 파이프라인 시작: %s (기준일 이전: %s)", TODAY, CUTOFF_DATE)
    log.info("   DB: %s", DB_PATH)

    config_file = CONFIG_PATH if CONFIG_PATH.exists() else Path("config/users.json")
    if not config_file.exists():
        log.error("설정 파일 없음: %s", config_file)
        sys.exit(1)

    with open(config_file, encoding="utf-8") as f:
        config = json.load(f)

    folder_id = config.get("shared_drive_folder_id", "")
    if not folder_id or folder_id == "REPLACE_WITH_YOUR_DRIVE_FOLDER_ID":
        log.error("config/users.json 에 shared_drive_folder_id 를 설정해주세요.")
        sys.exit(1)

    owner_cfg = config["drive_owner"]
    owner_email = owner_cfg["email"]
    stats = Stats()

    with open_db() as db_conn:
        all_users = [owner_cfg] + config.get("additional_users", [])
        for user_cfg in all_users:
            try:
                quota_ok = backup_user(user_cfg, owner_email, folder_id, stats, db_conn)
            except Exception as e:
                log.exception("[%s] 예상치 못한 오류: %s", user_cfg.get("id"), e)
                quota_ok = True  # 알 수 없는 오류는 다음 계정 계속 진행

            if not quota_ok:
                log.warning(
                    "API 할당량 초과 감지 — 나머지 계정 건너뜀. "
                    "내일 저장된 커서부터 자동 재개됩니다."
                )
                break

    stats.save()
    log.info(
        "▶ 전체 완료 — 총 %d건 (성공 %d / 실패 %d / DB스킵 %d) | 이관 %.1f MB",
        stats.total, stats.success, stats.failed, stats.skipped,
        stats.freed_bytes / 1024 / 1024,
    )


if __name__ == "__main__":
    main()
