# 2026년 6월 20일 현재 google photo API가 막힌 것으로 확인되어 사용이 불가합니다.

# Google Photos → Drive 자동 이관 백업 시스템

구글 포토의 1년 이상 된 사진을 구글 드라이브 공유 폴더로 자동 이관하고  
포토에서 영구 삭제하여 용량을 확보하는 무인 Docker 백업 시스템입니다.

---

## 목차

1. [아키텍처](#아키텍처)
2. [사전 준비: Google Cloud Console 설정](#사전-준비-google-cloud-console-설정)
3. [설치 및 배포](#설치-및-배포)
4. [시놀로지 NAS에서 실행하는 법](#시놀로지-nas에서-실행하는-법)
5. [업데이트 방법](#업데이트-방법)
6. [수동 실행 및 테스트](#수동-실행-및-테스트)
7. [로그 파일 구조](#로그-파일-구조)
8. [중요 제약 사항 및 주의점](#중요-제약-사항-및-주의점)
9. [파일 구조](#파일-구조)

---

## 아키텍처

```
Docker Container (항상 실행)
  ├── cron 02:00  →  backup.py   (포토 → 드라이브 이관 + 삭제)
  └── cron 08:00  →  slack_notify.py  (슬랙 통계 알림)
```

**파이프라인 순서 (계정별):**
1. Photos API 로 365일 이전 사진 조회 (오래된 순)
2. Drive 공유폴더에서 `photoId` 중복 확인 → 이미 있으면 스킵
3. 사진 다운로드 → Drive 업로드 (`[userId]_파일명`, description=photoId)
4. 추가 사용자 파일은 소유권을 드라이브 오너로 이전 (추가 사용자 용량 소비 없음)
5. Photos에서 사진 삭제 (API 제약으로 일부 삭제 불가 시 로그 기록)

---

## 사전 준비: Google Cloud Console 설정

### 1. 프로젝트 생성 및 API 활성화

1. [Google Cloud Console](https://console.cloud.google.com) 접속
2. 새 프로젝트 생성 (예: `photo-backup`)
3. **API 및 서비스 → 라이브러리** 에서 아래 두 API 활성화:
   - `Photos Library API`
   - `Google Drive API`

### 2. OAuth 동의 화면 구성

1. **API 및 서비스 → OAuth 동의 화면** 클릭
2. User Type: **외부** 선택 후 저장
3. 앱 이름, 지원 이메일 입력 후 저장
4. **스코프 추가**:
   - `https://www.googleapis.com/auth/photoslibrary`
   - `https://www.googleapis.com/auth/photoslibrary.readonly`
   - `https://www.googleapis.com/auth/drive`
5. **테스트 사용자** 탭: 백업할 모든 계정의 이메일 추가

### 3. OAuth 2.0 클라이언트 ID 생성 (계정별)

각 계정마다 아래를 반복합니다:

1. **API 및 서비스 → 사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID**
2. 애플리케이션 유형: **데스크톱 앱**
3. 이름: `photo-backup-user1` (구분하기 쉽게)
4. 생성 후 **JSON 다운로드**
5. 파일 이름을 `user1_credentials.json` 으로 변경 후 `auth/` 폴더에 저장

> **클라이언트 ID는 1개만 만들어도 됩니다.**  
> 동일한 credentials.json 파일을 `user1_credentials.json`, `user2_credentials.json` 등으로 복사해서 사용하면 됩니다.  
>
> 단, **OAuth 토큰(token.json)은 계정마다 반드시 별도로 발급해야 합니다.**  
> 토큰 발급 시 브라우저에서 각자의 구글 계정으로 로그인하는 과정이 필요하며,  
> 이 과정을 거쳐야 해당 계정의 구글 포토에 접근할 수 있습니다.

---

## 설치 및 배포

### 1. 저장소 클론 및 설정 파일 준비

```bash
git clone <this-repo> googlephoto_backup
cd googlephoto_backup

# 환경 설정 파일 생성
cp .env.example .env
```

`.env` 파일을 열어 필요한 값을 입력합니다:

```env
# 슬랙 알림 (선택사항)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

TZ=Asia/Seoul
LOG_LEVEL=INFO
MAX_ITEMS_PER_RUN=100
DELETE_FROM_PHOTOS=true
```

### 2. 계정 설정 (`config/users.json`)

```bash
cp config/users.json.example config/users.json
```

```json
{
  "drive_owner": {
    "id": "user1",
    "email": "남편_이메일@gmail.com",
    "credentials_file": "/auth/user1_credentials.json",
    "token_file": "/auth/user1_token.json"
  },
  "additional_users": [
    {
      "id": "user2",
      "email": "아내_이메일@gmail.com",
      "credentials_file": "/auth/user2_credentials.json",
      "token_file": "/auth/user2_token.json"
    },
    {
      "id": "user3",
      "email": "자녀_이메일@gmail.com",
      "credentials_file": "/auth/user3_credentials.json",
      "token_file": "/auth/user3_token.json"
    }
  ],
  "shared_drive_folder_id": "구글_드라이브_폴더_ID"
}
```

> **드라이브 폴더 ID 찾기**: 공유 폴더를 브라우저에서 열었을 때 URL의 마지막 부분  
> `https://drive.google.com/drive/folders/여기가_폴더ID`

### 3. credentials.json 파일 배치

```
auth/
├── user1_credentials.json   ← Google Cloud에서 다운로드한 OAuth JSON
├── user2_credentials.json
└── user3_credentials.json
```

### 4. OAuth 토큰 발급 (계정별 최초 1회)

NAS는 브라우저가 없으므로 **로컬 PC에서 토큰을 발급한 뒤 NAS로 복사**합니다.

#### 4-1. 로컬 PC에서 토큰 발급

Python 3.11+ 가 설치된 로컬 PC에서 실행합니다:

```bash
# 저장소 클론 (로컬 PC)
git clone https://github.com/joseph-84/googlephoto-backup.git
cd googlephoto-backup

# credentials 파일 배치
# auth/user1_credentials.json, auth/user2_credentials.json ... 복사

# 의존성 설치
pip install -r requirements.txt

# 계정마다 실행 (브라우저가 열리며 구글 로그인 요청)
python src/auth_setup.py --user user1
python src/auth_setup.py --user user2
python src/auth_setup.py --user user3
```

인증 완료 후 `auth/` 폴더에 token 파일이 생성됩니다:

```
auth/
├── user1_credentials.json
├── user1_token.json   ← 자동 생성
├── user2_credentials.json
├── user2_token.json   ← 자동 생성
...
```

#### 4-2. 토큰 파일을 NAS로 복사

```bash
# NAS의 프로젝트 경로 예시: /volume1/googlephoto-backup/auth/
scp auth/user1_token.json your_nas_user@nas_ip:/volume1/googlephoto-backup/auth/
scp auth/user2_token.json your_nas_user@nas_ip:/volume1/googlephoto-backup/auth/
```

> **Windows 사용자**는 `scp` 대신 WinSCP나 파일 스테이션으로 복사해도 됩니다.  
> `auth/` 폴더는 `.gitignore` 에 등록되어 있으므로 git에 올라가지 않습니다.

> **토큰 갱신**: 토큰은 만료 시 자동으로 갱신됩니다. 재발급이 필요한 경우에만 이 과정을 반복하세요.

### 5. Docker 실행

```bash
docker-compose up -d
```

컨테이너가 실행되면 매일 새벽 2시에 자동 백업이 시작됩니다.

---

## 시놀로지 NAS에서 실행하는 법

### 1. SSH 활성화

DSM → 제어판 → 터미널 및 SNMP → **SSH 서비스 활성화** 체크 → 적용

### 2. SSH 접속

```bash
ssh your_user@nas_ip -p 50022
```

> 포트는 DSM 터미널 설정에서 확인 (기본값: 22, 변경한 경우 해당 포트 사용)

### 3. 저장소 클론

```bash
cd /volume1/docker
git clone https://github.com/joseph-84/googlephoto-backup.git
cd googlephoto-backup
```

> git 이 없으면 DSM **패키지 센터**에서 **Git** 패키지를 먼저 설치하세요.

### 4. 설정 파일 준비

```bash
cp .env.example .env
vi .env

cp config/users.json.example config/users.json
vi config/users.json
```

### 5. 토큰 파일 복사

로컬 PC에서 발급한 token 파일을 파일 스테이션으로 `/volume1/docker/googlephoto-backup/auth/` 에 업로드합니다.

### 6. 컨테이너 실행

```bash
sudo docker-compose up -d --build
```

### 7. 터미널 접속 (테스트)

컨테이너 매니저 UI 터미널은 이 구조에서 동작하지 않습니다. SSH에서 아래 명령어로 접속하세요:

```bash
sudo docker exec -it googlephoto_backup bash
```

### 8. 백업 수동 실행 (테스트)

```bash
python /app/src/backup.py
```

---

## 업데이트 방법

코드가 변경되었을 때 NAS에 반영하는 방법입니다.

### 1. SSH로 NAS 접속

```bash
ssh your_user@nas_ip
cd /volume1/googlephoto-backup
```

### 2. 최신 코드 받기

```bash
git pull
```

### 3. 컨테이너 재시작

```bash
sudo docker-compose up -d --build
```

> ⚠️ `--build` 옵션은 코드가 바뀐 경우 이미지를 새로 빌드합니다.  
> `.env`, `config/users.json`, `auth/` 폴더는 git 관리 대상이 아니므로 pull 해도 덮어써지지 않습니다.

---

## 수동 실행 및 테스트

```bash
# 즉시 백업 실행 (테스트)
docker exec -it googlephoto_backup python /app/src/backup.py

# 슬랙 알림 즉시 전송 (테스트)
docker exec -it googlephoto_backup python /app/src/slack_notify.py

# 로그 실시간 확인
tail -f logs/backup_$(date +%Y-%m-%d).log

# 통계 파일 확인
cat logs/stats_$(date +%Y-%m-%d).json
```

---

## 로그 파일 구조

```
logs/
├── backup_2024-01-15.log      # 상세 작업 로그 (파일별 성공/실패/스킵)
├── stats_2024-01-15.json      # 슬랙 알림용 통계 (JSON)
└── cron.log                   # 크론 실행 로그
```

### stats JSON 예시

```json
{
  "date": "2024-01-15",
  "total": 142,
  "success": 138,
  "failed": 2,
  "skipped": 2,
  "freed_bytes": 512345678,
  "per_user": {
    "user1": { "success": 80, "failed": 1, "freed_bytes": 300000000 },
    "user2": { "success": 58, "failed": 1, "freed_bytes": 212345678 }
  }
}
```

---

## 중요 제약 사항 및 주의점

### Google Photos 삭제 API 제약

Google Photos Library API는 **해당 앱이 직접 업로드한 항목만** 삭제할 수 있습니다.  
카메라, 다른 앱으로 업로드된 사진은 API로 삭제가 불가능합니다 (403 응답).

- 삭제 불가 사진은 로그에 `⚠️ Photos 삭제 불가 (수동 삭제 필요)` 로 기록됩니다.
- Drive 업로드는 정상 유지됩니다.
- 수동으로 Google Photos 앱에서 해당 날짜의 사진을 삭제하거나,  
  [Google Photos 설정 > 저장용량 관리](https://photos.google.com/storage) 를 활용하세요.

### 소유권 이전

- 추가 사용자(아내, 자녀 등)의 사진을 Drive에 업로드 후 드라이브 오너로 소유권이 이전됩니다.
- 소유권 이전 후 해당 파일은 드라이브 오너의 용량에서만 소비됩니다.
- 드라이브 오너는 구글 원(Google One) 구독자인 것을 권장합니다.

### 토큰 자동 갱신

- OAuth 토큰은 만료 시 자동으로 갱신됩니다.
- 갱신에 실패하면 해당 계정의 백업이 건너뛰어지고 에러 로그에 기록됩니다.
- 장기간 갱신 실패 시 `auth_setup.py --user <id>` 를 다시 실행하여 재인증하세요.

---

## 파일 구조

```
googlephoto_backup/
├── src/
│   ├── backup.py          # 핵심 백업 파이프라인
│   ├── slack_notify.py    # 슬랙 통계 알림
│   └── auth_setup.py      # OAuth 토큰 최초 발급
├── auth/                  # OAuth credentials & token (git에 절대 커밋 금지)
├── logs/                  # 백업 로그 및 통계
├── config/
│   └── users.json         # 계정 매핑 설정
├── cron/
│   └── crontab            # 크론 스케줄
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

> ⚠️ `auth/` 폴더와 `.env` 파일을 절대 git에 커밋하지 마세요.  
> `.gitignore` 에 반드시 추가하세요:
> ```
> auth/
> .env
> logs/
> ```
