# 장애인 AI 비서 서비스 — 전체 플로우 개요

> 한국 장애인 복지 정책을 음성으로 안내하는 실시간 AI 상담 시스템.
> 사용자 음성 → DB 검색 → 음성 답변까지 전 과정 + 운영 자동화(크롤러·관리자 콘솔·데이터 플라이휠)를 정리.
> 작성: 2026-06-19 갱신 · 기준 v0.16.1 (Phase 5-A: 데이터 플라이휠 + 관리자 콘솔 + ServerA 배포) 시점

---

## 1. 한눈에 보는 전체 아키텍처

```
┌─────────────────────────────────────────────────────────────────────┐
│                       사용자 (시각·청각 장애인 등)                       │
└────────────────────────┬────────────────────────────────────────────┘
                         │ 음성 입력
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 프론트엔드 (브라우저)                                                  │
│  ├─ test_live.html  — 채팅 말풍선 UI + 마이크 + 인터럽션 처리         │
│  ├─ mic-processor.js (AudioWorklet) — PCM 16kHz + 노이즈 게이트     │
│  └─ admin.html      — 관리자 콘솔(HITL 검토 큐 + 정책 CRUD + 스케줄) │
└────────────────────────┬────────────────────────────────────────────┘
                         │ WebSocket (PCM Base64) · /admin REST
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 백엔드 — FastAPI (welfare_backend/)                                  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ live_bridge.py  — Gemini Live API ↔ WebSocket 양방향 브릿지    │  │
│  │  · System Instruction (DB 우선 → 외부 검색 폴백 라우팅)         │  │
│  │  · Barge-in (인터럽션) 자동 처리                                │  │
│  │  · TurnTracker hook → 미해결 질의 적재 트리거                   │  │
│  └──────────────────────────────────────────────────────────────┘  │
│              │ Function Calling                                      │
│              ▼                                                       │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ tool_handlers.py — 5종 검색 도구                                │  │
│  │  1) search_policies_by_metadata  (카테고리·중증도 필터)         │  │
│  │  2) search_by_keyword            (자연어 벡터 검색)             │  │
│  │  3) get_policy_details           (특정 정책 전체)               │  │
│  │  4) check_eligibility_criteria   (자격 판정)                    │  │
│  │  5) find_operating_agencies      (지역·기관 벡터 검색)          │  │
│  │  + google_search (외부 검색 폴백, Gemini 내장)                  │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ admin_router.py   — /admin REST (검토 큐·정책 CRUD·즉시 실행)   │  │
│  │ scheduler.py      — APScheduler 인앱 스케줄(크롤/재검증/발굴/백업)│  │
│  │ unresolved_logger.py — 미해결 질의 PII 스크러빙 후 비동기 적재   │  │
│  │ discovery_core.py — 신규 정책 발굴(Track B, 후보 초안만)        │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────┬────────────────────────────────────────────┘
                         │ SQLAlchemy(asyncpg) · psycopg2(배치/스케줄)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 데이터베이스 — PostgreSQL + pgvector (<DB_HOST> · 내부망 VPN)       │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ welfare_policies (마스터, 43 행)                                │  │
│  │  · id, title, category, severity_levels[], age_min/max,        │  │
│  │    has_companion_benefit, full_data(JSONB), last_verified ...  │  │
│  │  · GIN 인덱스 (severity_levels, full_data)                     │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ policy_chunks (벡터)                                            │  │
│  │  · embedding VECTOR(768) — gemini-embedding-001                │  │
│  │  · HNSW partial 인덱스 (embedding IS NOT NULL, cosine)         │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ unresolved_queries (미해결 질의, 7개 인덱스)  ← 데이터 플라이휠  │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────┬────────────────────────────────────────────┘
                         ▲
                         │ ingest_sync.py (MD5 해시 기반 스마트 부분 재적재)
                         │
┌────────────────────────┴────────────────────────────────────────────┐
│ 데이터 레이어 — welfare_backend/policy_db/                            │
│  · items/B001~B043 JSON+MD 쌍 (43개 항목)                            │
│  · schema.json (Draft-07)                                            │
│  · crawl_targets.json v2.6.0 (382개 출처)                            │
└────────────────────────▲────────────────────────────────────────────┘
                         │ confirm_apply.py / 관리자 콘솔 apply (검토 후 반영)
                         │
┌────────────────────────┴────────────────────────────────────────────┐
│ 자동화 레이어 — policy_db/crawler/ (APScheduler 스케줄)               │
│  · detectors.py    (6종 변경 감지 — grounding 포함)                  │
│  · crawler.py      (메인 오케스트레이션)                             │
│  · llm_backends.py (LLM 추상화 — gemini[기본]/claude/gemma)          │
│  · llm_updater.py  (기존+변경출처 → LLM → 갱신 JSON, staging 저장)   │
│  · confirm_apply.py(검토 → 백업 → 반영 → ingest_sync 호출)           │
│  · review_core.py / policy_core.py (검토·CRUD 공용 로직)             │
└─────────────────────────────────────────────────────────────────────┘
```

> 배포: ServerA Docker(`deploy/serverA/`) — 호스트 12321 → 컨테이너 18000, DB는 compose 내부 `postgres:5432`. LLM/임베딩은 Gemini 클라우드 유지.

---

## 2. 데이터 흐름 시나리오

### 시나리오 ① — 정상 음성 대화

```
1. 사용자: 마이크로 "장애인 지하철 무료인가요?" 발화
2. AudioWorklet (mic-processor.js):
   - 브라우저 SR(44100) → 16kHz 다운샘플
   - RMS 임계값 노이즈 게이트 — 무음 청크 폐기 (VAD 제3안: 항상 전송)
   - Int16 PCM Base64 인코딩
3. WebSocket → FastAPI /ws/live-chat
4. live_bridge.py:
   - Gemini Live 세션에 session.send_realtime_input(audio=...)
   - AutomaticActivityDetection 으로 발화 종료 자동 감지
5. Gemini: System Instruction 따라 search_by_keyword 도구 호출 결정
6. tool_handlers.tool_search_by_keyword(query="지하철 무료"):
   - _embed("지하철 무료") → 768차원 벡터 (gemini-embedding-001)
   - PostgreSQL: SELECT ... ORDER BY embedding <=> :qvec LIMIT 5
   - 정책 매칭 결과 + Fat Response 반환
7. Gemini: tool response 받고 한국어 음성 답변 생성
8. AI 응답 (24kHz PCM 청크) → live_bridge → WebSocket → 브라우저
9. 브라우저: 청크 단위 재생 + transcript 채팅창 표시
10. TurnTracker: DB 검색이 성공했으므로 미해결 적재 없음
```

### 시나리오 ② — 외부 검색 폴백 + 미해결 적재 (데이터 플라이휠)

```
1~5. 시나리오 ①과 동일 — 단, 사용자가 "고양이 키우는 장애인 보조금?" 질문
6. tool_search_by_keyword: 결과 없음
7. Gemini: System Instruction 의 3단계 라우팅 따라
   → "저희 정책 DB에서는 정확한 정보를 찾지 못했어요. 외부 검색으로 확인해 드리겠습니다." 안내
   → google_search 자동 호출
8. 외부 검색 결과로 답변 + "외부 웹 검색 결과 기준, 공식 기관 재확인 권장" 명시
9. TurnTracker hook: 폴백 3종 분류 자동 감지 → unresolved_logger
   - PII 스크러빙(전화·주민·카드) 후 unresolved_queries 에 비동기 INSERT
   - 이후 주간 리포트·신규 정책 발굴(discovery)의 입력으로 사용
```

### 시나리오 ③ — Barge-in (인터럽션)

```
1. AI 답변 도중 사용자 발화
2. AudioWorklet 의 노이즈 게이트 통과 → 마이크 PCM 전송
3. Gemini Live 가 자동 감지 → server_content.interrupted=True
4. live_bridge.py: _safe_send_json({"type": "interrupted"})
5. 브라우저: stopAllPlayback() 실행 (BufferSource.stop / playbackCtx.close / 큐 비우기)
6. AI 음성 즉시 끊김 → 사용자 새 발화 처리
```

### 시나리오 ④ — 정기 크롤링 (APScheduler 인앱 스케줄)

```
1. scheduler.py 의 crawl_cron(매월 2·16일 09:00) → crawler.crawler 실행
2. crawl_targets.json 382개 타겟 순회
   - detectors.DETECTORS[change_detection_method] 호출
   - page_hash / last_modified_field / grounding / pdf_hash / css_selector_text / manual_review
   - snapshots/ 비교
3. 변경 감지된 출처 → used_by_items 식별 (예: B015)
4. llm_updater.update_item(B015):
   - get_backend() → 기본 GeminiBackend(gemini-3.1-pro-preview) (또는 claude/gemma)
   - 기존 B015 JSON + 변경 출처 본문 → LLM 호출 (temperature=0)
   - schema 재검증 → staging/B015_*.staged.json 저장
5. reports/2026-06-02.md 자동 생성 (변경 출처·영향 항목·diff)
6. 관리자 검토(택1):
   - CLI: python -m crawler.confirm_apply --list / --policy-id B015 --diff / --reingest
   - 관리자 콘솔: /admin 검토 큐에서 diff 확인 → apply / reject / triage
7. 반영 시: items/.backups/ 백업 → items/ 덮어쓰기 → ingest_sync 로 해당 정책만 재청크·재임베딩
8. revalidate_cron(매월 25일) 전체 재검증, discovery_cron(매월 1·15일) 신규 발굴, backup_cron(매일 04:00)
```

---

## 3. 컴포넌트별 역할

### 3.1 데이터 자산 — `welfare_backend/policy_db/`

| 자산 | 위치 | 규모 |
|---|---|---|
| 항목 JSON | `items/B001~B043.json` | 43개 |
| 항목 Markdown | `items/B001~B043.md` | 43개, 사람용 |
| 표준 스키마 | `schema.json` | Draft-07 |
| 출처 인덱스 | `crawl_targets.json` v2.6.0 | 382개 (primary 183·secondary 131·supplementary 68) |

> 원본 PDF/DOCX(전단지·사업안내)는 레포에 미포함 — 별도 보관소(NAS 등)에서 동기화.

### 3.2 백엔드 — `welfare_backend/`

| 파일 | 역할 |
|---|---|
| `main.py` | FastAPI 앱 + 5종 도구 REST + WebSocket /ws/live-chat + /health + admin 라우터 mount |
| `live_bridge.py` | Gemini Live ↔ WebSocket 양방향 펌프, System Instruction, 도구 라우팅, TurnTracker hook |
| `tool_handlers.py` | 5종 DB 도구의 async 구현 (Gemini Function Calling용) |
| `admin_router.py` | `/admin` REST — 검토 큐(staging list/review/apply/reject/triage), 정책 CRUD, deactivate/reactivate, 단건 crawl, init-baseline |
| `scheduler.py` | APScheduler(BackgroundScheduler) 인앱 스케줄 + run-now, 설정은 `/data/admin_schedule.json` |
| `unresolved_logger.py` | TurnTracker, PII 스크러빙, 폴백 3종 분류, 비동기 INSERT |
| `discovery_core.py` | 신규 정책 발굴(Track B) — 미답변→군집화→분류→외부검색+초안→후보 저장(검토 전용) |
| `models.py` | WelfarePolicy + PolicyChunk + UnresolvedQuery ORM, FallbackReason Enum |
| `database.py` | 비동기 SQLAlchemy 엔진 + AsyncSessionLocal |
| `schemas.py` | Pydantic 스키마 |
| `static/test_live.html` | 채팅 UI + WebSocket 클라이언트 + 인터럽션 |
| `static/mic-processor.js` | AudioWorklet (PCM 16kHz + 노이즈 게이트) |
| `static/admin.html` | 관리자 콘솔 UI |

### 3.3 자동화 — `welfare_backend/policy_db/crawler/`

| 파일 | 역할 |
|---|---|
| `detectors.py` | 6종 변경 감지: page_hash, pdf_hash, last_modified_field, css_selector_text, grounding, manual_review |
| `crawler.py` | 메인 CLI — 감지 → 다운로드 → llm_updater 호출 → 리포트 |
| `llm_backends.py` | LLM 추상화 — `GeminiBackend`(기본) / `AnthropicBackend` / `GemmaBackend`(온프레미스) |
| `llm_updater.py` | LLM 호출(백엔드 무관) → 갱신 JSON 생성, PDF 본문 추출 (구 `claude_updater.py`) |
| `confirm_apply.py` | 검토 후 items/ 반영 + ingest_sync 자동 호출 (반영 성공 시 baseline 전진) |
| `review_core.py` / `policy_core.py` | 검토·CRUD 공용 로직 (admin_router 와 공유) |
| `ingest_sync.py` | (policy_db/ 산하) MD5 해시 기반 스마트 부분 재적재, `--rebuild` 초기 구축 |

### 3.4 배치 스크립트 — `welfare_backend/scripts/`

| 스크립트 | 역할 |
|---|---|
| `backfill_embeddings.py` | embedding NULL 행 채움 |
| `purge_old_queries.py` | 오래된 unresolved 정리 (보존기간 경과분 파기) |
| `show_recent_unresolved.py` | 최근 미해결 질의 조회 |
| `weekly_report.py` | 주간 리포트 (`--use-llm` 시 의도 클러스터링) |

---

## 4. 외부 의존성·API

### 4.1 Google AI (기본)
- **gemini-2.0-flash-live-001** — 음성 대화 (Live API, WebSocket) · 환경변수 `GEMINI_LIVE_MODEL`
- **gemini-embedding-001** — 768차원 임베딩 (검색·청크 적재) · `GEMINI_EMBED_MODEL`
- **gemini-3.1-pro-preview** — 크롤러 갱신 LLM + grounding 감지 + discovery · `GEMINI_LLM_MODEL`
- **google_search** — Gemini 내장 외부 검색 (폴백)

### 4.2 크롤러 LLM 백엔드 (교체 가능, `LLM_BACKEND`)
- **gemini** (기본) — 임베딩·Live 와 키 단일화
- **claude** — `claude-sonnet-4-5` (`ANTHROPIC_MODEL`)
- **gemma** — 온프레미스 Ollama/vLLM (`GEMMA_MODEL`, 기본 `gemma-3n`)

### 4.3 인프라
- **PostgreSQL + pgvector** — 내부망 VPN 직결 테스트서버 (<DB_HOST>:5432)
- **HNSW partial 인덱스** — embedding IS NOT NULL 대상, cosine 거리
- **GIN 인덱스** — severity_levels(ARRAY), full_data(JSONB)
- **ServerA Docker 배포** — `deploy/serverA/`, 호스트 12321→컨테이너 18000, DB는 compose 내부 `postgres:5432`(외부 미공개)

---

## 5. 보안·안전 장치 요약

| 영역 | 안전 장치 |
|---|---|
| **사실 검증** | System Instruction "도구 결과 없이 답변 금지", 출처 멘트는 legal_basis + contact 만 사용 |
| **시크릿 관리** | `.env` 단일 진입점, 코드 하드코딩 금지. 서버는 `.env.server.example` 템플릿 |
| **LLM Hallucination** | temperature=0, schema 재검증, "추측·추가 정보 생성 금지" SI |
| **자동 재적재 금지** | LLM 갱신 결과는 staging 만, 사용자/관리자 confirm 필수 |
| **신규 발굴 안전** | discovery 는 후보 초안만 저장, 자동 정책 생성 금지 (관리자 승인 시에만 items 반영) |
| **자동 백업** | items/ 덮어쓰기 전 .backups/ 타임스탬프 백업 + backup_cron 일일 백업 |
| **PII 보호** | unresolved 적재 전 전화·주민·카드 번호 스크러빙, 보존기간 경과분 자동 파기 |
| **인터럽션 안전성** | AudioWorklet 노이즈 게이트, server_content.interrupted 감지 |
| **음성 응답 위험 차단** | URL·내부 ID 음성 노출 금지, 금액·날짜는 한국어 발화체로 |

---

## 6. 단계별 완성 현황

| Phase | 내용 | 상태 |
|---|---|---|
| **1** | PostgreSQL + pgvector 인프라, 테스트서버 구축 | ✅ |
| **2** | 정책 항목 JSON 적재 + 청크 임베딩 (현재 43종) | ✅ |
| **3** | FastAPI 5종 도구 + Gemini Live 음성 챗봇 | ✅ |
| **4-A** | AudioWorklet + 인터럽션 안정화 | ✅ |
| **4-B** | 정기 크롤러 + LLM 갱신 + confirm 워크플로우 | ✅ |
| **5-A** | 데이터 플라이휠(미해결 적재) + 관리자 콘솔(HITL) + ServerA 배포 | ✅ |
| **5-B** | 신규 정책 발굴(discovery, Track B) — 후보 초안 생성 | ✅ (운영 데이터 축적 중) |
| **6** | 온프레미스 Gemma 전환, 관계 기반 추론 | 🔜 |

---

## 7. 다음 진행 후보

### 7.1 운영 데이터 축적·발굴 고도화
1. unresolved_queries 1~2개월 축적 → 보강 우선순위 도출
2. discovery 후보 초안 품질 평가 → B044+ 등록 파이프라인 정착
3. 관리자 콘솔 v2: 스케줄 편집 UI, triage 워크플로 강화

### 7.2 온프레미스 Gemma 전환
1. 사내 Gemma 서버 구축 (Ollama 또는 vLLM)
2. `.env` 의 `LLM_BACKEND=gemma` + 관련 환경변수 설정
3. 한두 항목 테스트 갱신 → Gemini 대비 품질 비교 → 시스템 프롬프트 튜닝

### 7.3 운영 강화
1. Redis 캐시 (자주 묻는 질문 Top 50)
2. 응답 후처리 Hallucination 검증
3. 이메일/Slack 알림 (큰 변경 발견 시)

---

## 8. 운영 명령어 모음

```bash
# 백엔드 가동 (로컬)
cd welfare_backend
uvicorn main:app --reload --host 127.0.0.1 --port 18000
# 브라우저: http://127.0.0.1:18000/static/test_live.html
#           http://127.0.0.1:18000/static/admin.html  (관리자 콘솔)

# DB 초기 구축 / 부분 재적재
cd welfare_backend/policy_db
python ingest_sync.py --rebuild   # 빈 DB 전량 적재
python ingest_sync.py             # 변경된 항목만 증분 동기화

# 크롤러 — 감지만(무비용) / 풀 실행
python -m crawler.crawler --skip-claude
python -m crawler.crawler

# staging 검토 + 반영
python -m crawler.confirm_apply --list
python -m crawler.confirm_apply --policy-id B001 --diff
python -m crawler.confirm_apply --policy-id B001 --reingest

# 배치 스크립트
cd welfare_backend
python -m scripts.backfill_embeddings
python -m scripts.purge_old_queries
python -m scripts.show_recent_unresolved
python -m scripts.weekly_report --use-llm

# ServerA Docker 배포 (요약 — deploy/serverA/README.md 참조)
cd ~/stacks
docker compose build accessistant && docker compose up -d accessistant
curl -fsS http://127.0.0.1:12321/health
```

> 스케줄은 OS cron 이 아니라 백엔드 인앱 APScheduler(`scheduler.py`)로 구동됩니다. 기본값:
> crawl 매월 2·16일 / revalidate 매월 25일 / discovery 매월 1·15일 / backup 매일 04:00.

---

*문서 버전: 2.0  ·  갱신: 2026-06-19  ·  기준 v0.16.1 (Phase 5-A) 스냅샷*
