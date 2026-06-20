"""
OAuth 2.0 초기 인증 토큰 발급 헬퍼.
각 계정마다 최초 1회 실행하여 token.json 을 생성합니다.

사용법:
  python src/auth_setup.py --user user1
  python src/auth_setup.py --user user2
"""

import argparse
import json
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary",
    "https://www.googleapis.com/auth/photoslibrary.readonly",
    "https://www.googleapis.com/auth/drive",
]

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/users.json")
AUTH_DIR = os.environ.get("AUTH_DIR", "/auth")


def load_user_config(user_id: str) -> dict:
    config_file = Path(CONFIG_PATH)
    if not config_file.exists():
        # 로컬 개발용 fallback
        config_file = Path("config/users.json")
    with open(config_file) as f:
        config = json.load(f)

    if config["drive_owner"]["id"] == user_id:
        return config["drive_owner"]
    for user in config.get("additional_users", []):
        if user["id"] == user_id:
            return user
    raise ValueError(f"users.json 에서 user_id='{user_id}' 를 찾을 수 없습니다.")


def resolve_path(path_str: str) -> Path:
    """컨테이너 내부 경로를 로컬 경로로 변환 (로컬 실행 시)."""
    p = Path(path_str)
    if not p.is_absolute():
        return p
    # /auth/... → auth/... (로컬 실행 fallback)
    if not p.exists():
        relative = Path(*p.parts[2:])  # /auth/user1_... → user1_...
        local = Path(AUTH_DIR.lstrip("/")) / relative.name
        if local.exists() or local.parent.exists():
            return local
    return p


def run_auth(user_id: str):
    user = load_user_config(user_id)
    creds_file = resolve_path(user["credentials_file"])
    token_file = resolve_path(user["token_file"])

    if not creds_file.exists():
        print(f"[오류] credentials 파일이 없습니다: {creds_file}")
        print(
            "  Google Cloud Console > API 및 서비스 > 사용자 인증 정보에서\n"
            "  OAuth 2.0 클라이언트 ID를 생성하고 JSON을 다운로드하세요."
        )
        sys.exit(1)

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        print(f"[{user_id}] 이미 유효한 토큰이 있습니다: {token_file}")
        return

    if creds and creds.expired and creds.refresh_token:
        print(f"[{user_id}] 토큰 갱신 중...")
        creds.refresh(Request())
    else:
        print(f"[{user_id}] 브라우저 인증을 시작합니다...")
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
        creds = flow.run_local_server(port=0)

    token_file.parent.mkdir(parents=True, exist_ok=True)
    with open(token_file, "w") as f:
        f.write(creds.to_json())
    print(f"[{user_id}] 토큰 저장 완료: {token_file}")


def main():
    parser = argparse.ArgumentParser(description="Google OAuth 토큰 발급")
    parser.add_argument("--user", required=True, help="users.json 의 user id (예: user1)")
    args = parser.parse_args()
    run_auth(args.user)


if __name__ == "__main__":
    main()
