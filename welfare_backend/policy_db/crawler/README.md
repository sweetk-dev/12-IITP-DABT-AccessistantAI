# 정책DB 정기 크롤러 (Phase 4 — 트랙 B)

`crawl_targets.json` 의 출처들을 주기적으로 점검해 정책 변경을 감지하고, **LLM 백엔드(현 Gemini API, Claude/온프레미스 Gemma 선택 가능)** 로 갱신 JSON 을 자동 생성한 뒤 staging 폴더에 저장합니다. 사용자가 검토 후 confirm 해야만 실제 DB(items/) 에 반영됩니다.

> **운영 원칙**: LLM 자동 재적재 금지. 사용자가 매번 검토·승인하는 안전한 워크플로우.

---

## 폴더 구조

```
policy_db/crawler/
├── __init__.py
├── detectors.py          # 5종 변경 감지
├── crawler.py            # 메인 오케스트레이션 (CLI)
├── llm_backends.py       # ⭐ LLM 백엔드 추상화 — Claude/Gemma 교체 가능
├── llm_updater.py     # 기존/변경 출처 → LLM → 갱신 JSON (백엔드 무관)
├── confirm_apply.py      # 사용자 검토 후 items/ 반영 + ingest_sync.py 자동 호출
├── README.md             # 이 문서
│
├── snapshots/{target_id}/    # 감지 본문(latest.*, pending) + 비교 baseline(해시·chunks)
├── staging/                  # LLM 갱신 JSON 대기 (+ .sources.json baseline 메타)
│   ├── .applied/             # 반영 완료 보관
│   └── .rejected/            # 폐기 보관
├── manual_review_state.json  # manual_review 타겟의 마지막 검토일
└── reports/YYYY-MM-DD.{md,json}    # 회차별 리포트 (수동 검토 대상 섹션 포함)
```

---

## LLM 백엔드 (현재 Gemini → Claude / 온프레미스 Gemma 선택 가능)

`llm_backends.py` 가 LLM 호출을 추상화. 환경변수 `LLM_BACKEND` 로 교체.

### 현재: Gemini API (외부, 권장 — 외부 API 벤더 단일화)
```env
LLM_BACKEND=gemini
GEMINI_API_KEY=AIza...                 # 임베딩과 동일 키 재사용
GEMINI_LLM_MODEL=gemini-3.1-pro-preview   # (선택, 기본값 동일)
```
> 임베딩·Live 음성과 동일한 Google 키 하나로 통합되어 키·결제·쿼터 관리가 단일화됩니다.

### 대안: Claude API (외부)
```env
LLM_BACKEND=claude
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5    # (선택, 기본값 동일)
```

### 향후(Phase 5+): 온프레미스 Gemma (Ollama / vLLM)
```env
LLM_BACKEND=gemma
GEMMA_API_URL=http://gemma-server.internal:11434
GEMMA_MODEL=gemma-3n
GEMMA_API_STYLE=ollama          # 또는 openai (vLLM·LiteLLM 호환)
GEMMA_API_KEY=...               # (선택, vLLM 등 Bearer 인증 필요 시)
```

→ **코드 변경 없이 환경변수만 바꾸면 백엔드 교체 완료**. `llm_updater.py` 내부에서 `get_backend()` 가 자동으로 적절한 구현체를 반환.

### 추가 백엔드 작성
`llm_backends.py` 의 `LLMBackend` 추상 클래스를 상속 → `generate_json_update()` 구현 → `get_backend()` 팩토리에 등록.

---

## 사용법

### 1. 사전 준비

`welfare_backend/.env` 에 다음 키가 있는지 확인 (이미 등록 완료):
```env
LLM_BACKEND=gemini               # 현재 기본 백엔드
GEMINI_API_KEY=...               # 갱신 LLM + 임베딩 공용 (Google 키 단일화)
GEMINI_LLM_MODEL=gemini-3.1-pro-preview   # (선택)
ANTHROPIC_API_KEY=sk-ant-...     # (선택) LLM_BACKEND=claude 로 전환 시에만 필요
DB_HOST=<DB_HOST>           # 테스트 서버
```

추가 패키지 (이미 requirements.txt 에 포함):
```bash
pip install google-genai anthropic httpx jsonschema beautifulsoup4 trafilatura readability-lxml pypdf
```

### 2. 크롤링 실행

```bash
cd policy_db

# (a) 전체 점검 + 변경 감지 + LLM 갱신 (정기 실행)
python -m crawler.crawler

# (b) 감지만 (다운로드·LLM 호출 없이)
python -m crawler.crawler --dry-run

# (c) 변경 감지 + 다운로드만 (LLM 호출 생략 — 비용 절약)
python -m crawler.crawler --skip-claude

# (d) 특정 출처만
python -m crawler.crawler --only law

# (e) 수동 검토(manual_review) 타겟의 검토일 기록 (크롤링 안 함)
python -m crawler.crawler --mark-reviewed all          # 전체
python -m crawler.crawler --mark-reviewed <target_id>  # 특정 타겟
```

### 3. 변경 사항 검토

```bash
python -m crawler.confirm_apply --list                       # staging 목록
python -m crawler.confirm_apply --policy-id B001 --diff      # diff 만
python -m crawler.confirm_apply --policy-id B001             # y/N 확인 후 반영
python -m crawler.confirm_apply --policy-id B001 --reingest  # 반영 + DB 자동 재적재
python -m crawler.confirm_apply --policy-id B001 --reject    # 폐기
python -m crawler.confirm_apply --all --reingest             # 일괄 반영 + 재적재
```

### 4. DB 재적재 (스마트 동기화)

`ingest_sync.py` 가 파일 MD5 해시 기반 스마트 동기화를 수행 — **변경된 파일만 자동 감지해 부분 재처리**:

```bash
cd policy_db
python ingest_sync.py
```

`confirm_apply.py --reingest` 옵션은 위 스크립트를 자동 호출하므로, 일반적으로 별도 실행 불필요.

---

## 스케줄 등록 (매월 2일·16일)

운영 환경은 **Linux cron** 기준 (Windows Task Scheduler 도 가능).

### Linux cron (서버 권장)
```cron
# 매월 2일·16일 09:00 KST 실행
0 9 2,16 * * cd /opt/welfare_backend/policy_db && /usr/bin/python3 -m crawler.crawler >> /var/log/welfare_crawler.log 2>&1
```

`/etc/cron.d/welfare_crawler` 파일로 저장. 시스템 메일이 설정돼 있으면 stderr 가 자동 메일로 옴.

### systemd timer (대안)
```ini
# /etc/systemd/system/welfare-crawler.service
[Unit]
Description=Welfare Policy Crawler
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/welfare_backend/policy_db
ExecStart=/usr/bin/python3 -m crawler.crawler
StandardOutput=append:/var/log/welfare_crawler.log
StandardError=append:/var/log/welfare_crawler.log
EnvironmentFile=/opt/welfare_backend/.env

# /etc/systemd/system/welfare-crawler.timer
[Unit]
Description=Welfare Policy Crawler — monthly 2 & 16

[Timer]
OnCalendar=*-*-02 09:00:00
OnCalendar=*-*-16 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

활성화:
```bash
sudo systemctl enable --now welfare-crawler.timer
sudo systemctl list-timers | grep welfare
```

### Windows Task Scheduler (로컬 테스트용)
작업 스케줄러 → 작업 만들기 → 트리거: 매월 2일·16일 09:00 → 프로그램: `python.exe`, 인수: `-m crawler.crawler`, 시작 위치: `policy_db` 폴더.

---

## 정적 출처 → 동적 확장 (Phase 5 설계 초안)

현재 `crawl_targets.json` 은 정적 출처 인덱스. 두 가지 경로로 확장:

### A. 분기별 수동 출처 추가 (관리자 주도)
- 분기 1회 (1월·4월·7월·10월) 사용자 발견 출처를 crawl_targets 에 추가
- 새 출처는 `used_by_items` 가 비어 있어도 OK (모니터링만)

### B. 사용자 질의 기반 동적 발굴 (Phase 5 신규 — 별도 트랙)

```
[1] 음성 챗봇에서 외부 검색 폴백 발생
    → 사용자 질문·AI 답변을 unresolved_queries 테이블에 누적
    → live_bridge.py 에서 google_search 사용 시 자동 기록

[2] 주기적 분석 (월 1회 cron)
    → 누적된 질의를 LLM 으로 분류:
      a) 정책 관련 vs 잡담
      b) 기존 항목으로 답할 수 있는지 vs 진짜 새 정책 필요
    → "새 정책" 으로 분류된 질의 클러스터링 (유사 질문 묶기)

[3] 새 정책 후보 자동 발굴
    → 키워드 기반 외부 웹 검색
    → 발견한 공식 출처 → crawl_targets 추가 후보 staging
    → 새 정책 항목 (B040, B041…) 자동 초안 생성 → items/.staging_new/

[4] 사용자 검토
    → 관리자 화면(또는 CLI)에서 staging 항목 검토
    → 승인 시 → crawl_targets·items 반영 → ingest_sync.py 자동 호출
```

→ 코드는 Phase 5 에서 구현. 현재 트랙 B 는 (1)·(2) 인프라(질의 누적 테이블 + 분류 스크립트)만 사전 설계.

#### 사전 설계 — `unresolved_queries` 테이블 (DDL)
```sql
CREATE TABLE IF NOT EXISTS unresolved_queries (
  id BIGSERIAL PRIMARY KEY,
  asked_at TIMESTAMPTZ DEFAULT NOW(),
  user_question TEXT NOT NULL,
  ai_answer TEXT,
  used_external_search BOOLEAN DEFAULT FALSE,
  estimated_category VARCHAR(20),
  classified_as VARCHAR(20),       -- 'policy' / 'casual' / 'duplicate' / 'needs_research'
  classified_at TIMESTAMPTZ,
  clustered_with INT REFERENCES unresolved_queries(id),
  resolved_in_policy_id VARCHAR(10),  -- 새로 만들어진 정책에 흡수되면 기록
  notes TEXT
);
CREATE INDEX idx_uq_classified ON unresolved_queries(classified_as);
```

#### 사전 설계 — `live_bridge.py` 에 추가할 훅
```python
# 외부 검색 사용 시 자동 기록
if sc and sc.grounding_metadata:
    await record_unresolved_query(
        user_question=last_user_transcript,
        ai_answer=last_ai_transcript,
        used_external_search=True,
    )
```

---

## 안전 장치

| 안전 장치 | 효과 |
|---|---|
| **자동 재적재 금지** | LLM 이 만든 JSON 은 staging 에만. items/ 변경은 confirm_apply 필수 |
| **자동 백업** | items/ 덮어쓰기 전 `items/.backups/` 로 타임스탬프 백업 |
| **schema 재검증** | LLM 출력도 Draft-07 통과해야 staging·반영 모두 가능 |
| **Hallucination 방지 SI** | "schema 보존·추측 금지·문장 스타일 유지" 강제, temperature=0 |
| **staging 히스토리** | .applied/ + .rejected/ 보존 — 사후 추적 |
| **dry-run / skip-claude** | 비용·영향 0 으로 테스트 가능 |
| **ingest_sync 부분 재적재** | 변경된 파일만 재임베딩 — 전체 재적재 비용 절감 |

---

## 트러블슈팅

- **첫 실행은 변경 0건이 정상** — 모든 출처가 "최초 스냅샷" 으로 기록되어 비교 기준이 됨. 다음 회차부터 실제 감지 시작.
- **LLM 응답이 schema 위배** — staging/ 에 `_FAILED_*.txt` 디버그 파일이 저장됨. 시스템 프롬프트 보강 필요.
- **변경 감지 false positive** — 페이지에 동적 타임스탬프가 있으면 매번 변경됨. detectors.py 의 `_mask_dynamic_noise()` 마스킹 패턴을 보강(조회수·세션·날짜 등). page_hash 는 `_normalize_html_text()` 로 정규화한 본문만 비교하고, last_modified_field 는 날짜를 보존(`mask_dates=False`)한다.
- **변경 감지 false negative** — JS 렌더링 페이지는 httpx 만으로는 못 잡음. 향후 Playwright 도입 검토.
- **LLM_BACKEND 전환 시 응답 품질 차이** — Claude → Gemma 교체 후 첫 회차는 `--skip-claude` 로 감지만 한 뒤 일부 항목만 수동 테스트하며 SI 튜닝 권장.

### 환경변수 누락 (드물지만 발생 시)
- `ANTHROPIC_API_KEY 환경변수가 비어 있습니다` 에러 → `.env` 의 키가 실제로 로드되는지 확인 (`echo $ANTHROPIC_API_KEY` 또는 Python 측 `os.environ.get`). 본 프로젝트는 이미 등록 완료된 상태입니다.

---

*문서 버전: 1.2  ·  작성: 2026-05-21 → 2026-06-05 갱신(v0.6 baseline 분리·수동 검토)  ·  사용자 confirm 워크플로우 안전 모드*

---

## 파이프라인 보강 (v0.2 ~ v0.5)

초기 "해시 비교 → LLM 전체 재생성" 흐름을 다음과 같이 단계적으로 강화했습니다.

1. **본문 정규화 (v0.2, `detectors._normalize_html_text`)**
   BeautifulSoup 기반으로 nav/footer/script 등 비콘텐츠를 제거하고, 조회수·세션·다형식 날짜·토큰 같은 동적 노이즈를 마스킹합니다. 본문이 동일하면 노이즈가 달라도 같은 해시가 나와 거짓 변경이 줄어듭니다. (bs4 미설치 시 정규식 폴백)

2. **청크 기반 변경 감지 (v0.3, `_chunk_html` / `_chunk_diff`)**
   본문을 의미 단위(문단·리스트·표 행·제목)로 청킹해 이전 스냅샷과 비교하고, 추가/삭제/수정을 산출합니다. 유사도(difflib) 기반으로 "수정"을 분류해 add/remove 과대계상을 줄이며, 결과는 크롤러 리포트에 요약 출력됩니다.

3. **필드 단위 패치 갱신 (v0.4, `llm_updater._apply_patch`)**
   LLM 이 항목 전체 JSON 을 다시 쓰지 않고 변경된 필드만 패치(op/path/old/new/evidence/confidence)로 반환합니다. add/update 만 자동 적용하고 패치에 없는 필드는 불변으로 보존합니다. **delete 는 자동 적용하지 않고** 검토 항목(`.review.json`)으로 분리하며, 출처에 명시적 종료 문구(폐지·종료·미시행 등)가 있을 때만 `delete_candidate` 로 승격합니다.

4. **반영 전 회귀 가드 (v0.5, `confirm_apply._regression_check`)**
   items/ 반영 직전에 최상위 필수 키 누락, 문서 크기 급감(<50%), 주요 배열 길이 급감(<50%)을 검사해 조용한 손실을 차단합니다. 스키마도 핵심 필드(`title`·`short_summary`·`version` minLength, `sources` minItems)에 최소 제약을 추가했습니다. 검토가 필요한 패치 항목은 반영 단계에서 함께 표시됩니다.

5. **감지/확정 baseline 분리 (v0.6, `detectors.save_content_snapshot` / `save_baseline_snapshot`)**
   변경 감지 시점에는 본문(`latest.*`)·pending 청크만 저장하고, 비교 baseline(해시·`chunks.json`)은 `confirm_apply` 반영이 성공한 시점에만 전진합니다. 따라서 리포트를 놓치거나 LLM·staging 이 실패해도 다음 회차에 변경이 다시 노출됩니다. 미확정 변경의 매 회차 LLM 재호출은 "이미 staging 대기 시 생략" 가드로 막고, 반영 시 전진할 출처는 staging 의 `.sources.json` 사이드카에 기록합니다.

6. **수동 검토 표면화 (v0.6, `manual_review`)**
   자동 감지가 불가한 `manual_review` 타겟을 리포트의 "수동 검토 대상" 섹션에 노출하고, `manual_review_state.json` 으로 마지막 검토일을 추적합니다. 관리자는 `python -m crawler.crawler --mark-reviewed <target_id|all>` 로 검토일을 기록합니다.

> 전체 흐름: 출처 fetch → 정규화 → (해시 + 청크) 변경 감지 → 변경 본문 저장 → 변경 시 LLM 필드 패치 → staging 저장(+검토 리포트 + 출처 메타) → `confirm_apply`(스키마 검증 + 회귀 가드 + 사람 승인) → items/ 반영(백업 후 덮어쓰기) + 출처 baseline 전진.
