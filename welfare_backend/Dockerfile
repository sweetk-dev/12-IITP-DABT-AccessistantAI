# AccessistantAI welfare_backend — ServerA(Docker compose) 배포 이미지
# 베이스: slim + lxml/trafilatura 소스 빌드 폴백용 최소 빌드 의존성
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 레이어 (캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Gemini Live 폴백(온프레미스 음성) 의존성 — 별도 레이어(무거움: torch/faster-whisper)
COPY requirements-fallback.txt .
RUN pip install --no-cache-dir -r requirements-fallback.txt

# 앱 소스
COPY . .

EXPOSE 18000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:18000/health || exit 1

# 운영 기동 (reload 없음)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "18000"]
