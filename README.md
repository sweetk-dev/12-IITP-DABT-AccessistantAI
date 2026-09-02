# AccessistantAI — 장애인 정책 음성 Q&A 서비스 (백엔드)

장애인 복지 정책에 대한 자연어/음성 질의응답을 제공하는 FastAPI 기반 백엔드.

## 구성

- **`welfare_backend/`** — FastAPI + PostgreSQL + pgvector 백엔드
  - `main.py` — 정책 검색·요약 도구 5종을 노출하는 API 엔드포인트
  - `live_bridge.py` — 음성·텍스트 멀티모달 WebSocket 브릿지
  - `tool_handlers.py` — Function Calling 핸들러
  - `database.py` / `models.py` / `schemas.py` — DB 연결, ORM, 스키마
  - `policy_db/` — 정책 데이터 정의 (`items/`, `schema.json`), 인제스트(`ingest_sync.py`), 자동 갱신 크롤러(`crawler/`)
  - `scripts/` — 배치 작업 (임베딩 백필, 주간 리포트, 오래된 쿼리 정리 등)
  - `static/` — 마이크 워커, 진입 페이지(accessistant.html)·관리자 콘솔
  - `reports/unresolved/` — 미해결 질의 주간 리포트
- **`docs/handoff/`** — 설계·아키텍처 문서
  - `SERVICE_OVERVIEW.md` — 서비스 전반 개요 (구성·플로우·운영 가이드)
  - `system_architecture.html` — 시스템 아키텍처 도식

## 기술 스택

- 언어/프레임워크: Python 3.x, FastAPI, uvicorn
- DB: PostgreSQL + pgvector (`welfare_db`)
- 외부 API:
  - Gemini Multimodal Live API (음성·텍스트 멀티모달 대화 + 임베딩)
  - LLM (정기 크롤러 갱신안 생성 + 주간 리포트 의도 클러스터링): 기본 Gemini. 환경변수 `LLM_BACKEND` 로 온프레미스 Gemma 교체 가능 (외부 API는 Google로 단일화)
- 데이터 처리: trafilatura, beautifulsoup4, readability-lxml, pypdf
- 빌드(별도 데모): PyInstaller

## 백엔드 실행

```bash
cd welfare_backend

# 1) 환경변수 준비
cp .env.example .env
# .env 를 열어 DB_PASS, GEMINI_API_KEY 채우기

# 2) 의존성 설치 (필요 시)
pip install fastapi uvicorn psycopg2-binary pgvector google-genai
pip install trafilatura beautifulsoup4 readability-lxml pypdf jsonschema httpx

# 3) 서버 기동
uvicorn main:app --reload --host 127.0.0.1 --port 18000
```

필요한 환경변수 템플릿: `welfare_backend/.env.example`

## 대중교통 구간 안내 — 실시간 저상버스·역 편의시설 (v1.39.0)

경로 안내의 버스 구간은 "저상버스 정차는 보장되지 않는다"는 고정 경고만 냈고, 지하철 구간은 승강기 개수만 알았다.
경로 서비스(02 v1.19.0)가 경기버스정보(GBIS) 도착·위치 API 와 국가철도공단 역 설비 자료를 붙이면서 이 비서도 그것을 말하고 보여 준다.

| 도구 | 발화 예 | 동작 |
|---|---|---|
| `get_bus_arrivals` | "저상버스 언제 와", "다음 버스 저상이야", "51번 몇 분 남았어" | 정류장의 실시간 도착정보. 안내 중이면 승차 정류장·노선이 세션에서 자동 주입되고, 아니면 말한 장소·현재 위치의 가장 가까운 정류장을 쓴다. 저상 차량이 안 잡히면 "지금 오는 차 중엔 없다"로 말한다(없다고 단정하지 않음) |
| `get_station_facilities` | "범계역 엘리베이터 어디 있어", "안양역 장애인 화장실 있어" | 승강기·리프트 출입구별 위치, 장애인화장실 게이트 안/밖·출구, 승강장 안전발판·열차 이격거리. 유무는 3상태 — `unknown` 은 "자료 없음"이지 "없음"이 아니다 |
| `plan_accessible_route` (보강) | — | 대중교통 조합 요청 시 `realtime=true` 로 승차 정류장 도착정보를 함께 받아 `low_floor_note` 로 요약. 지하철 leg 에 승·하차 역 설비 요약 |
| `find_nearby_transit` (보강) | — | 역 항목에 승강기 수·리프트 수·장애인화장실 3상태를 실어 보낸다 |

화면(`accessistant.html`): 버스 구간 카드가 실시간 확인 결과("저상 2번 약 4분 후 도착")를 고정 경고 대신 보여 주고, 안내 중 승차 스텝에서는 20초 간격으로 갱신하며 저상 차량이 확인되면 1회 음성으로 알린다. 지하철 카드에는 승차역 승강기 출입구와 장애인화장실 유무가 붙는다. REST 미러: `GET /api/v1/tools/bus_arrivals`, `GET /api/v1/tools/station_facilities`.

## 상담 음성 품질 — 에코 억제·숫자 표기 (v1.40.0)

- **에코 억제**: 스피커로 나간 상담원 음성의 끝말("…드릴게요"의 "요")이 마이크로 되돌아와 사용자 발화로 전사·응답되는 일이 잦았다. 상담 화면에서도 상담원 음성 중·직후(재생 종료 후 800ms)에는 지속 발화(barge-in)가 감지된 뒤부터 마이크 프레임을 보내고, 그래도 들어온 짧은 전사가 직전 발화의 끝말과 같으면 서버·화면 모두 사용자 발화로 다루지 않는다(`text_normalize.looks_like_echo`).
- **숫자 표기**: 상담원 텍스트는 음성 전사라 "일오칠칠에 천번", "관평로 백팔십이"처럼 적혔다. 화면 말풍선과 대화 이력에서 전화번호(낱자 3자 이상 + 전화 맥락 → `1577-1000`)와 단위 수(십·백·천·만 + 단위/도로명 → `182`)를 아라비아 숫자로 바꾼다(`text_normalize.normalize_numbers`, 프런트 동일 규칙). 한 글자 수("이 층", "삼일")와 낱말("구사일생", "하십시오")은 손대지 않는다.

## 정책 데이터

```bash
# (선택) 정기 정책 변경 감지 + 갱신안 생성
cd welfare_backend/policy_db
python -m crawler.crawler --skip-llm  # 감지+다운로드만 (비용 0)
python -m crawler.crawler                # 풀 실행 (감지+갱신안 staging 저장)
python -m crawler.confirm_apply          # staging → items 반영 (반영 성공 시 baseline 전진)
python -m crawler.crawler --mark-reviewed all  # 수동 검토 타겟 검토일 기록

# 빈 DB 초기 구축 (스키마 생성 + 전량 임베딩 적재)
python ingest_sync.py --rebuild
# 증분 동기화 (변경된 항목만 재적재)
python ingest_sync.py
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

- 레포 태그: **v1.40.0** (상담 음성 에코 억제 · 답변 숫자 표기 정규화) / v1.39.0 (실시간 저상버스 도착정보 · 역 편의시설 설비 단위 안내)
- 백엔드 내부: v1.2
- 인제스트 스크립트: `ingest_sync.py` (초기 구축 `--rebuild`, 증분 동기화 기본)

## 라이선스

이 프로젝트는 MIT 라이선스로 배포됩니다. 전문은 [LICENSE](LICENSE) 파일을 참고하십시오.

본 연구는 정부(과학기술정보통신부)의 재원으로 정보통신기획평가원의 지원을 받아 수행된 연구입니다.
(연구개발과제번호 RS-2024-003976, 데이터 기반 장애인 데이터 탐색·활용 해결기술 개발)

