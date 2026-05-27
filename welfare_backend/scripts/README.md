# Track A 운영 cron 가이드

`unresolved_queries` 데이터 플라이휠 운영용 3종 cron 스크립트.
모두 standalone 호출 가능하며, Linux cron / Windows 작업 스케줄러 어느 쪽에서도 동작합니다.

## 권장 실행 순서·주기

| 시점 | 스크립트 | 역할 |
|---|---|---|
| **매일 23:00** | `backfill_embeddings.py` | 그날 적재된 `embedding IS NULL` 행을 Gemini Embedding API 로 채움 |
| **매일 03:30** | `purge_old_queries.py` | 90일 이전 행 자동 파기 (PII 리스크 분산) |
| **매주 월요일 06:00** | `weekly_report.py` | 최근 7일 통계 리포트(마크다운) 생성 |

이 순서로 두면 *매일 데이터가 임베딩 채워진 상태로 유지*되고, *주간 리포트는 100% 임베딩 채움률로 분석*하게 됩니다.

> ⚠️ 초기 운영 권장. 사용량이 늘면 `backfill` 빈도(시간당)·`purge` 보관 기간(60~120일)을 조정하세요. 스크립트 자체는 빈도와 무관 — cron 등록만 바꾸면 됩니다.

## 각 스크립트 동작 요약

### 1. `backfill_embeddings.py`

```bash
python -m scripts.backfill_embeddings --batch-size 50 --max-rows 500
# 옵션:
#   --batch-size : 한 번에 SELECT 행 수 (기본 50)
#   --max-rows   : 한 실행에서 최대 처리 행 수 (기본 500)
#   --dry-run    : 실제 임베딩/UPDATE 없이 대상 행만 출력
```

`embedding IS NULL` 행을 batch 로 가져와 Gemini Embedding API 로 768차원 벡터 채움.
실패 행은 2회 재시도 후 건너뛰고 계속 진행. 마지막 모든 행 처리되면 종료.

### 2. `weekly_report.py`

```bash
python -m scripts.weekly_report                  # 통계만 (무료)
python -m scripts.weekly_report --use-llm        # Claude 클러스터링 추가 (비용)
python -m scripts.weekly_report --days 14        # 분석 기간 변경
```

산출물: `welfare_backend/reports/unresolved/weekly_YYYY-MM-DD.md`

내용:
- 총 미해결 질의 건수
- 폴백 사유 분포 (`google_search` / `empty_result` / `tool_error`)
- 일별 추이
- 자주 나타난 질의 Top 20
- 재발화 의도 그룹 Top 10 (`intent_group_id` 별 turn 수)
- 임베딩 채움률 (백필 cron 헬스 지표)
- (옵션) Claude 의도 클러스터링 + 신규 정책 후보 도출

### 3. `purge_old_queries.py`

```bash
python -m scripts.purge_old_queries                # 기본 90일
python -m scripts.purge_old_queries --days 60      # 60일로 조정
python -m scripts.purge_old_queries --dry-run      # 영향 행수 확인만
```

`created_at < NOW() - INTERVAL 'N days'` 행을 DELETE.
PII 텍스트(user_query)가 무기한 누적되지 않도록 분산.

## Linux 운영 서버 cron 등록 예

`/etc/cron.d/welfare_track_a` 파일에 다음을 등록:

```cron
# /etc/cron.d/welfare_track_a
# 환경변수 PATH 명시 (cron 기본 PATH 좁음)
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
SHELL=/bin/bash

# 매일 03:30 — 90일 이전 행 파기
30 3 * * *  welfare  cd /opt/welfare_backend && /usr/bin/python3 -m scripts.purge_old_queries >> /var/log/welfare/purge.log 2>&1

# 매일 23:00 — 임베딩 백필
0 23 * * *  welfare  cd /opt/welfare_backend && /usr/bin/python3 -m scripts.backfill_embeddings >> /var/log/welfare/backfill.log 2>&1

# 매주 월요일 06:00 — 주간 리포트
0 6 * * 1   welfare  cd /opt/welfare_backend && /usr/bin/python3 -m scripts.weekly_report >> /var/log/welfare/weekly.log 2>&1
```

로그 디렉토리 사전 생성:

```bash
sudo mkdir -p /var/log/welfare && sudo chown welfare /var/log/welfare
```

`welfare` 는 실행 계정 — `.env` 파일 읽기 권한이 있어야 합니다.

## Windows 작업 스케줄러 등록 예

PowerShell 관리자 권한 실행:

```powershell
# 매일 23:00 — 임베딩 백필
schtasks /Create /TN "Welfare_Backfill_Embeddings" `
    /TR "python -X utf8 -m scripts.backfill_embeddings" `
    /SC DAILY /ST 23:00 `
    /SD 2026/05/22 `
    /RU SYSTEM /F

# 매일 03:30 — 90일 파기
schtasks /Create /TN "Welfare_Purge_Old" `
    /TR "python -X utf8 -m scripts.purge_old_queries" `
    /SC DAILY /ST 03:30 `
    /RU SYSTEM /F

# 매주 월요일 06:00 — 주간 리포트
schtasks /Create /TN "Welfare_Weekly_Report" `
    /TR "python -X utf8 -m scripts.weekly_report" `
    /SC WEEKLY /D MON /ST 06:00 `
    /RU SYSTEM /F
```

> 작업 디렉토리(시작 위치)를 `<welfare_backend 경로>` 로 설정해야 `.env` 파일을 찾을 수 있습니다.
> `schtasks /Create` 만으로 시작 위치는 변경되지 않으므로, GUI(작업 스케줄러)에서 등록 후 "동작 → 편집 → 시작 위치"에 경로를 추가하거나, 배치 파일 래퍼를 사용하는 것이 안전합니다.

배치 래퍼 예 (`<welfare_backend 경로>\scripts\run_backfill.bat`):

```batch
@echo off
cd /d <welfare_backend 경로>
python -X utf8 -m scripts.backfill_embeddings >> logs\backfill.log 2>&1
```

## 운영 모니터링 체크리스트

매주 리포트 받을 때 같이 확인:

- 임베딩 채움률이 90% 이상인가? (낮으면 backfill cron 실패 의심)
- `purge` 로그에 정상 삭제 메시지가 매일 찍히는가?
- 적재 건수가 *급증/급감* 했는가? (음성 챗봇 사용량·도구 응답 품질 신호)
- `tool_error` 비율이 갑자기 늘었는가? (도구 로직·DB 인덱스 회귀 의심)

## 트러블슈팅

| 증상 | 원인 | 조치 |
|---|---|---|
| `backfill_embeddings` 가 매번 0행 처리 | hook 적재가 안 됨 | `live_bridge.py` 의 TurnTracker 로그 확인 |
| `embedding` 채움률이 낮음 | Gemini API 일시 장애 또는 `GEMINI_API_KEY` 만료 | 수동 실행 후 stderr 확인, API 키 갱신 |
| `weekly_report` 가 빈 리포트만 출력 | 7일 내 폴백 0건 | 정상 — 음성 챗봇이 잘 답한다는 의미 |
| `purge` 가 실수로 너무 많이 삭제 | `--days` 인자 오타 | 백업에서 복원. 운영 전 반드시 `--dry-run` 으로 영향 행수 확인 |
