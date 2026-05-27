# 장애인 AI 비서 서비스 — 전체 플로우 개요

> 한국 장애인 복지 정책을 음성으로 안내하는 실시간 AI 상담 시스템.
> 사용자 음성 → DB 검색 → 음성 답변까지 전 과정을 정리.
> 작성: 2026-05-22 · 기준 Phase 4 (운영 안정성 + 자동 데이터 갱신) 완료 시점

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
│  └─ mic-processor.js (AudioWorklet) — PCM 16kHz + 노이즈 게이트     │
└────────────────────────┬────────────────────────────────────────────┘
                         │ WebSocket (PCM Base64)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 백엔드 — FastAPI (welfare_backend/)                                  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ live_bridge.py  — Gemini Live API ↔ WebSocket 양방향 브릿지    │  │
│  │  · System Instruction (DB 우선 → 외부 검색 폴백 라우팅)         │  │
│  │  · Barge-in (인터럽션) 자동 처리                                │  │
│  │  · 음성 → 텍스트 transcription                                  │  │
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
│  │                                                                │  │
│  │  + google_search (외부 검색 폴백, Gemini 내장)                  │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────┬────────────────────────────────────────────┘
                         │ SQLAlchemy (asyncpg)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 데이터베이스 — PostgreSQL 13 + pgvector (테스트서버 <DB_HOST>)   │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ welfare_policies (마스터, 39 행)                                │  │
│  │  · id, title, category, severity_levels[], age_min/max,        │  │
│  │    has_companion_benefit, full_data(JSONB), last_verified ...  │  │
│  │  · GIN 인덱스 (severity_levels, full_data)                     │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ policy_chunks (벡터, ~500 행)                                   │  │
│  │  · embedding VECTOR(768) — gemini-embedding-001                │  │
│  │  · chunk_type: summary/eligibility/how_to_use/application/      │  │
│  │                faq/exceptions/legal_basis/agency_specific/      │  │
│  │                validity/penalties/contact                       │  │
│  │  · HNSW 인덱스 (cosine_distance)                                │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────┬────────────────────────────────────────────┘
                         ▲
                         │ ingest_sync.py (스마트 동기화)
                         │
┌────────────────────────┴────────────────────────────────────────────┐
│ 데이터 레이어 — policy_db/                                            │
│  · items/B0xx JSON+MD 쌍 (정책 항목 — 누적 확장 중)                            │
│  · schema.json (Draft-07)                                            │
│  · crawl_targets.json (출처 인덱스)                                   │
└────────────────────────▲────────────────────────────────────────────┘
                         │ confirm_apply.py (사용자 검토 후 반영)
                         │
┌────────────────────────┴────────────────────────────────────────────┐
│ 자동화 레이어 — crawler/ (매월 2일·16일 cron 실행)                     │
│  · detectors.py        (5종 변경 감지)                               │
│  · crawler.py          (메인 오케스트레이션)                          │
│  · llm_backends.py     (LLM 추상화 — Claude ↔ Gemma 교체 가능)        │
│  · claude_updater.py   (기존+변경출처 → LLM → 갱신 JSON)              │
│  · confirm_apply.py    (사용자 확인 → 백업 → 반영 → ingest_sync 호출)  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 데이터 흐름 시나리오

### 시나리오 ① — 정상 음성 대화

```
1. 사용자: 마이크로 "장애인 지하철 무료인가요?" 발화
2. AudioWorklet (mic-processor.js):
   - 브라우저 SR(44100) → 16kHz 다운샘플
   - RMS 임계값 0.012 — 무음 청크 폐기
   - Int16 PCM Base64 인코딩
3. WebSocket → FastAPI /ws/live-chat
4. live_bridge.py:
   - Gemini Live 세션에 session.send_realtime_input(audio=...)
   - Gemini 의 VAD가 자동으로 발화 종료 감지
5. Gemini: System Instruction 따라 search_by_keyword 도구 호출 결정
6. tool_handlers.tool_search_by_keyword(query="지하철 무료"):
   - _embed("지하철 무료") → 768차원 벡터 (gemini-embedding-001)
   - PostgreSQL: SELECT ... ORDER BY embedding <=> :qvec LIMIT 5
   - B001 정책 매칭 결과 + Fat Response 반환
7. Gemini: tool response 받고 한국어 음성 답변 생성
8. AI 응답 (24kHz PCM 청크) → live_bridge → WebSocket → 브라우저
9. 브라우저: 청크 단위 재생 + transcript 채팅창 표시
10. 답변 끝에 "장애인복지법에 따른 정책이며, 보건복지부 129로 문의하세요"
```

### 시나리오 ② — 외부 검색 폴백

```
1~5. 시나리오 ①과 동일 — 단, 사용자가 "고양이 키우는 장애인 보조금?" 질문
6. tool_search_by_keyword: 결과 없음
7. Gemini: System Instruction 의 3단계 라우팅 따라
   → "저희 정책 DB에서는 정확한 정보를 찾지 못했어요. 외부 검색으로 확인해 드리겠습니다." 음성 안내
   → google_search 자동 호출
8. 외부 검색 결과로 답변 + "외부 웹 검색 결과 기준, 공식 기관 재확인 권장" 명시
```

### 시나리오 ③ — Barge-in (인터럽션)

```
1. AI 답변 도중 사용자 발화
2. AudioWorklet 의 노이즈 게이트 통과 → 마이크 PCM 전송
3. Gemini Live VAD가 자동 감지 → server_content.interrupted=True
4. live_bridge.py: websocket.send_json({"type": "interrupted"})
5. 브라우저: stopAllPlayback() 실행
   - 모든 BufferSource.stop()
   - playbackCtx.close()
   - pendingPlayback 큐 비우기
6. AI 음성 즉시 끊김 → 사용자 새 발화 처리
```

### 시나리오 ④ — 정기 크롤링 (매월 2일 09:00)

```
1. cron → python -m crawler.crawler
2. crawl_targets.json 의 모든 타겟 순회
   - detectors.DETECTORS[change_detection_method] 호출
   - snapshots/ 비교
3. 변경 감지된 출처 → used_by_items 식별 (예: B015)
4. claude_updater.update_item_via_claude(B015):
   - get_backend() → AnthropicBackend (또는 GemmaBackend)
   - 기존 B015 JSON + 변경 출처 본문 → LLM 호출 (temperature=0)
   - schema 재검증 → staging/B015_*.staged.json 저장
5. reports/2026-06-02.md 자동 생성 (변경 출처·영향 항목·diff)
6. 관리자가 다음날 검토:
   - python -m crawler.confirm_apply --list
   - python -m crawler.confirm_apply --policy-id B015 --diff
   - python -m crawler.confirm_apply --policy-id B015 --reingest
7. confirm_apply:
   - items/.backups/ 백업 → items/ 덮어쓰기
   - ingest_sync.py 자동 호출 → 변경된 정책만 청크 재생성 + 재임베딩
```

---

## 3. 컴포넌트별 역할

### 3.1 데이터 자산 — `policy_db/`

| 자산 | 위치 | 규모 |
|---|---|---|
| 항목 JSON | `items/B0xx.json` | 정책 항목별 (평균 25KB) |
| 항목 Markdown | `items/B0xx.md` | 정책 항목별 (사람용) |
| 표준 스키마 | `schema.json` | Draft-07, 22개 properties |
| 출처 인덱스 | `crawl_targets.json` v2.3.0 | primary / secondary / supplementary 우선순위로 분류된 출처 인덱스 |
| 정책 PDF | `장애인등록혜택전단지_20260309.pdf` | 원본 전단지 |

### 3.2 백엔드 — `welfare_backend/`

| 파일 | 역할 |
|---|---|
| `main.py` | FastAPI 앱 + 5종 도구 REST 엔드포인트 + WebSocket /ws/live-chat |
| `live_bridge.py` | Gemini Live ↔ WebSocket 양방향 펌프, System Instruction, 도구 라우팅 |
| `tool_handlers.py` | 5종 DB 도구의 일반 async 함수 구현 (Gemini 직접 호출용) |
| `models.py` | WelfarePolicy + PolicyChunk SQLAlchemy ORM |
| `database.py` | 비동기 SQLAlchemy 엔진 + AsyncSessionLocal |
| `static/test_live.html` | 채팅 UI + WebSocket 클라이언트 + 인터럽션 |
| `static/mic-processor.js` | AudioWorklet (PCM 16kHz + 노이즈 게이트) |

### 3.3 자동화 — `policy_db/crawler/`

| 파일 | 역할 |
|---|---|
| `detectors.py` | 5종 변경 감지: page_hash, pdf_hash, last_modified_field, css_selector_text, manual_review |
| `crawler.py` | 메인 CLI — 감지 → 다운로드 → claude_updater 호출 → 리포트 |
| `llm_backends.py` | LLM 추상화 — `AnthropicBackend`(현) / `GemmaBackend`(향후 온프레미스) |
| `claude_updater.py` | LLM 호출 (백엔드 무관) → 갱신 JSON 생성 |
| `confirm_apply.py` | 사용자 검토 후 items/ 반영 + ingest_sync.py 자동 호출 |
| `ingest_sync.py` | (policy_db/ 산하) MD5 해시 기반 스마트 부분 재적재 |

---

## 4. 외부 의존성·API

### 4.1 Google AI
- **gemini-3.1-flash-live-preview** — 음성 대화 (Live API, WebSocket)
- **gemini-embedding-001** — 768차원 임베딩 (검색·청크 적재)
- **google_search** — Gemini 내장 외부 검색 (폴백)

### 4.2 Anthropic
- **claude-sonnet-4-5** — 정책 JSON 갱신 (정기 크롤러 시)

### 4.3 향후 교체 가능 (Phase 5+)
- **Gemma 3n** (또는 다른 온프레미스 오픈 LLM) — claude-sonnet-4-5 자리
  - Ollama API (`/api/chat`) 또는 OpenAI-compatible (`/v1/chat/completions`)
  - 환경변수 `LLM_BACKEND=gemma` 한 줄로 교체

### 4.4 인프라
- **PostgreSQL 13 + pgvector** — Rocky Linux 9 테스트서버 (<DB_HOST>:5432)
- **HNSW 인덱스** (m=16, ef_construction=64) — 벡터 검색
- **GIN 인덱스** — severity_levels(ARRAY), full_data(JSONB)

---

## 5. 보안·안전 장치 요약

| 영역 | 안전 장치 |
|---|---|
| **사실 검증** | System Instruction "도구 결과 없이 답변 금지", 출처 멘트는 legal_basis + contact 만 사용 |
| **시크릿 관리** | `.env` 단일 진입점, 코드 하드코딩 금지 |
| **LLM Hallucination** | temperature=0, schema 재검증, "추측·추가 정보 생성 금지" SI |
| **자동 재적재 금지** | LLM 갱신 결과는 staging 만, 사용자 confirm 필수 |
| **자동 백업** | items/ 덮어쓰기 전 .backups/ 로 타임스탬프 백업 |
| **인터럽션 안전성** | AudioWorklet 노이즈 게이트, server_content.interrupted 감지 |
| **음성 응답 위험 차단** | URL·내부 ID 음성 노출 금지, 금액·날짜는 한국어 발화체로 |

---

## 6. 단계별 완성 현황

| Phase | 내용 | 상태 |
|---|---|---|
| **1** | PostgreSQL + pgvector 인프라, 테스트서버 구축 | ✅ |
| **2** | 정책 항목 JSON 적재 (누적 확장) + 청크 임베딩 | ✅ |
| **3** | FastAPI 5종 도구 + Gemini Live 음성 챗봇 | ✅ |
| **4-A** | AudioWorklet + 인터럽션 안정화 | ✅ |
| **4-B** | 정기 크롤러 + LLM 갱신 + 사용자 confirm 워크플로우 | ✅ |
| **5** | 동적 정책 발굴 (외부 검색 폴백 → 신규 정책 학습) | 🔜 설계 |
| **5+** | 온프레미스 Gemma 전환, Playwright 통합, PDF 본문 처리 | 🔜 |

---

## 7. 다음 진행 후보 (Phase 5)

### 5.1 동적 정책 발굴 (사용자 명시 계획)
1. `unresolved_queries` 테이블 신설 (DDL은 crawler/README.md 에 초안)
2. live_bridge.py 의 google_search 사용 시점에 자동 기록 훅 추가
3. 월 1회 분석 cron — 정책 관련성 분류 → 클러스터링 → 새 정책 발굴
4. 발굴된 후보 → items/.staging_new/ → 사용자 검토 → B040+ 등록

### 5.2 온프레미스 Gemma 전환
1. 사내 Gemma 서버 구축 (Ollama 또는 vLLM)
2. `.env` 의 `LLM_BACKEND=gemma` + 관련 환경변수 4개 설정
3. 한두 항목 테스트 갱신 → Claude 대비 품질 비교 → 시스템 프롬프트 미세 튜닝

### 5.3 운영 강화
1. Playwright 통합 (JS 렌더링 페이지)
2. pypdf 기반 PDF 본문 추출 → LLM 입력
3. 이메일/Slack 알림 (큰 변경 발견 시)
4. Redis 캐시 (자주 묻는 질문 Top 50)
5. 응답 후처리 Hallucination 검증

---

## 8. 운영 명령어 모음

```bash
# 백엔드 가동
cd welfare_backend
uvicorn main:app --reload
# 브라우저: http://127.0.0.1:8000/static/test_live.html

# DB 부분 재적재 (스마트 동기화 — 변경된 파일만)
cd policy_db && python ingest_sync.py

# 크롤러 — dry-run (변경 감지만)
cd policy_db && python -m crawler.crawler --dry-run

# 크롤러 — 풀 실행 (감지 + 다운로드 + LLM 갱신)
cd policy_db && python -m crawler.crawler

# staging 검토 + 반영
cd policy_db
python -m crawler.confirm_apply --list
python -m crawler.confirm_apply --policy-id B001 --diff
python -m crawler.confirm_apply --policy-id B001 --reingest

# 스케줄 등록 (Linux cron)
echo "0 9 2,16 * * cd /opt/welfare_backend/policy_db && python3 -m crawler.crawler >> /var/log/welfare_crawler.log 2>&1" | sudo tee /etc/cron.d/welfare_crawler
```

---

*문서 버전: 1.0  ·  작성 시각: 2026-05-22  ·  Phase 4 완료 시점 스냅샷*
