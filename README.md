# AccessistantAI — 장애인 정책 음성 Q&A 서비스 (백엔드)

장애인 복지 정책에 대한 자연어/음성 질의응답을 제공하는 FastAPI 기반 백엔드.

## 구성

- **`welfare_backend/`** — FastAPI + PostgreSQL + pgvector 백엔드
  - `main.py` — 정책 검색·요약 도구 5종을 노출하는 API 엔드포인트
  - `live_bridge.py` — 음성·텍스트 멀티모달 WebSocket 브릿지
  - `tool_handlers.py` — Function Calling 핸들러
  - `database.py` / `models.py` / `schemas.py` — DB 연결, ORM, 스키마
  - `policy_db/` — 정책 데이터 정의 (`items/`, `schema.json`), 인제스트(`ingest_v1.5.py`), 자동 갱신 크롤러(`crawler/`)
  - `scripts/` — 배치 작업 (임베딩 백필, 주간 리포트, 오래된 쿼리 정리 등)
  - `static/` — 마이크 워커, 라이브 테스트 페이지
  - `reports/unresolved/` — 미해결 질의 주간 리포트
- **`docs/handoff/`** — 설계·아키텍처 문서
  - `SERVICE_OVERVIEW.md` — 서비스 전반 개요 (구성·플로우·운영 가이드)
  - `system_architecture.html` — 시스템 아키텍처 도식

## 기술 스택

- 언어/프레임워크: Python 3.x, FastAPI, uvicorn
- DB: PostgreSQL + pgvector (`welfare_db`)
- 외부 API:
  - Gemini Multimodal Live API (음성·텍스트 멀티모달 대화 + 임베딩)
  - Anthropic Claude API (정기 정책 크롤러: 변경 감지 → 갱신안 생성, 주간 리포트 클러스터링)
- 데이터 처리: trafilatura, beautifulsoup4, readability-lxml, pypdf
- 빌드(별도 데모): PyInstaller

## 백엔드 실행

```bash
cd welfare_backend

# 1) 환경변수 준비
cp .env.example .env
# .env 를 열어 DB_PASS, GEMINI_API_KEY, ANTHROPIC_API_KEY 채우기

# 2) 의존성 설치 (필요 시)
pip install fastapi uvicorn psycopg2-binary pgvector google-genai anthropic
pip install trafilatura beautifulsoup4 readability-lxml pypdf jsonschema httpx

# 3) 서버 기동
uvicorn main:app --reload --host 127.0.0.1 --port 18000
```

필요한 환경변수 템플릿: `welfare_backend/.env.example`

## 정책 데이터

```bash
# (선택) 정기 정책 변경 감지 + 갱신안 생성
cd welfare_backend/policy_db
python -m crawler.crawler --skip-claude  # 감지+다운로드만 (비용 0)
python -m crawler.crawler                # 풀 실행 (감지+갱신안 staging 저장)
python -m crawler.confirm_apply          # staging → items 반영

# 신규 항목 인제스트 (임베딩 생성 + DB 적재)
python ingest_v1.5.py
```

원본 PDF/DOCX 자료는 본 레포에 포함되지 않습니다 (별도 보관소에서 동기화).
별도 보관소(내부 NAS 등) 에서 받아 인제스트하세요.

## 배치 스크립트

```bash
cd welfare_backend
python -m scripts.backfill_embeddings      # 누락 임베딩 채우기
python -m scripts.purge_old_queries        # 오래된 unresolved 정리
python -m scripts.show_recent_unresolved   # 최근 미해결 질의 조회
python -m scripts.weekly_report            # 주간 리포트 (기본 통계)
python -m scripts.weekly_report --use-llm  # 주간 리포트 + 의도 클러스터링
```

## 버전

- 레포 태그: **v0.1.0** (초기 등록)
- 백엔드 내부: v1.2
- 인제스트 스크립트: `ingest_v1.5.py`

## 라이선스

내부 프로젝트 (비공개)
