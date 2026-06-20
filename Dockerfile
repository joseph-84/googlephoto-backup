FROM python:3.11-slim

# 시스템 패키지 설치 (cron + 타임존)
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 복사 (레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 코드 복사
COPY src/ ./src/
COPY config/ ./config/

# 크론탭 등록
COPY cron/crontab /etc/cron.d/photo-backup
RUN chmod 0644 /etc/cron.d/photo-backup \
    && crontab /etc/cron.d/photo-backup

# 로그 및 인증 디렉토리 생성
RUN mkdir -p /app/logs /auth

# 환경변수 파일이 있으면 크론 실행 시 로드되도록 설정
# (docker-compose 의 env_file 로 주입되므로 /etc/environment 에 복사)
RUN echo "#!/bin/bash" > /entrypoint.sh \
    && echo "printenv | grep -v 'no_proxy' >> /etc/environment" >> /entrypoint.sh \
    && echo "cron -f" >> /entrypoint.sh \
    && chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
