# welfare_backend

장애인 복지 정책 AI 음성 챗봇용 **FastAPI 백엔드** — Gemini Multimodal Live API의 Function Calling 도구를 제공.

## 폴더 구조
```
welfare_backend/
├── database.py        # PostgreSQL(welfare_db) 비동기 연결
├── models.py          # welfare_policies 테이블 ORM (v1.2 스키마)
├── main.py            # FastAPI 앱 + 도구 엔드포인트
├── .env.example       # 환경변수 템플릿
├── requirements.txt   # Python 의존성
└── README.md          # 이 문서
```

## 실행 절차

### 1. 가상환경 + 의존성 설치
```bash
cd welfare_backend
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 환경변수 설정
```bash
cp .env.example .env        # Windows: copy .env.example .env
# 에디터로 .env 열어 DB_PASS 등 본인 값 입력
```

### 3. 서버 실행
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 4. 테스트
- Swagger UI: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health
- 도구 호출 예시:
  ```
  GET /api/v1/tools/search_policies_by_metadata?category=교통&severity=심한%20장애(중증)&limit=5
  ```

## 사전 준비
1. PostgreSQL + pgvector 확장 활성화 (welfare_db 생성)
2. `ingest_v1.5.py` 로 정책 데이터 적재 (`items/` 디렉터리의 모든 B0xx 항목)
3. `.env` 작성

## 제공 도구 (Function Calling)

| # | 도구 | 경로 | 설명 |
|---|---|---|---|
| 1 | **search_policies_by_metadata** | `GET /api/v1/tools/search_policies_by_metadata` | 카테고리·중증도 메타데이터 필터링 (1차 좁히기) |
| 2 | **search_by_keyword** | `GET /api/v1/tools/search_by_keyword` | 자연어 벡터 검색 (모든 청크 대상, pgvector cosine) |
| 3 | **get_policy_details** | `GET /api/v1/tools/get_policy_details` | 특정 정책 전체 + 출처 + 핵심 요약 (Fat 응답) |
| 4 | **check_eligibility_criteria** | `GET /api/v1/tools/check_eligibility_criteria` | 자격 요건 청크 + 구조화 메타 (severity·age·income) |
| 5 | **find_operating_agencies** | `GET /api/v1/tools/find_operating_agencies` | 지역·기관 벡터 검색 (agency_specific + contact 청크) |

모든 도구는 **Fat Tool Response 패턴** 준수 — 호출 1회로 음성 답변 작성에 필요한 모든 컨텍스트 한 번에 반환.

## 다음 단계

| 도구 | 설명 |
|---|---|
| search_similar_faq(question, top_k) | FAQ 청크만 타겟팅 벡터 검색 (정밀도 향상) |
| search_external_web(query) | 내부 DB 미스 시 외부 웹 폴백 |

모든 도구는 **Fat Tool Response 패턴**(§7.2)을 따릅니다 — 호출 1회로 음성 답변에 필요한 모든 컨텍스트(요약·신청처·핵심 출처)를 한 번에 반환해 음성 Latency 최소화.

## 보안 / 운영 지침 준수
- DB 비밀번호·API 키 모두 `.env` 분리 (코드에 하드코딩 금지)
- `.env` 는 반드시 `.gitignore` 에 추가
- CORS는 운영 환경에서 도메인 화이트리스트로 제한 권장 (현재는 개발 편의상 `*`)
