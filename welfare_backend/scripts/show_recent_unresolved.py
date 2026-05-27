"""scripts/show_recent_unresolved.py

오늘(또는 최근 N시간) 적재된 미해결 질의를 사람이 읽기 좋은 표로 출력.
weekly_report 는 통계 위주라 "그 시간에 무슨 질문을 못 답했나" 가 안 보임.
이 스크립트는 raw row 를 그대로 표시 — 사용자 질문, AI 가 한 말, 어떤 도구를 어떻게 호출했는지.

사용:
  python -m scripts.show_recent_unresolved                # 최근 24시간 (기본)
  python -m scripts.show_recent_unresolved --hours 3      # 최근 3시간
  python -m scripts.show_recent_unresolved --hours 24 --limit 50
  python -m scripts.show_recent_unresolved --reason google_search   # 특정 폴백만
  python -m scripts.show_recent_unresolved --hours 1 --raw          # 표 대신 raw JSON

cron 과 무관 — 운영자가 즉시 확인용으로 돌리는 단방향 ad-hoc 스크립트.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv(_ROOT / ".env")

from database import engine  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("show_recent")


VALID_REASONS = {
    "low_similarity", "empty_result", "category_mismatch",
    "explicit_no_info", "google_search", "tool_error", "unknown",
}


async def fetch_rows(hours: int, limit: int, reason: str | None) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    params = {"since": since, "limit": limit}
    where_extra = ""
    if reason:
        where_extra = "AND fallback_reason = :reason"
        params["reason"] = reason
    sql = f"""
        SELECT
            id,
            created_at AT TIME ZONE 'Asia/Seoul' AS created_kst,
            session_id::text                   AS session_id,
            intent_group_id::text              AS intent_group_id,
            turn_in_group,
            fallback_reason::text              AS fallback_reason,
            user_query,
            ai_final_answer,
            tool_chain,
            grounding_info IS NOT NULL         AS has_grounding,
            embedding IS NOT NULL              AS has_embedding
        FROM unresolved_queries
        WHERE created_at >= :since
          {where_extra}
        ORDER BY created_at DESC
        LIMIT :limit
    """
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        rows = [dict(r._mapping) for r in result]
    return rows


def _shorten(s: str | None, width: int) -> str:
    if not s:
        return ""
    s = " ".join(s.split())  # collapse whitespace
    if len(s) <= width:
        return s
    return s[: width - 1] + "…"


def _format_tool_chain(chain) -> str:
    """tool_chain JSONB 를 한 줄 요약. 예: search_by_keyword(0건) → find_operating_agencies(2건)"""
    if not chain:
        return "-"
    try:
        if isinstance(chain, str):
            chain = json.loads(chain)
        steps = chain.get("steps") if isinstance(chain, dict) else None
        if not steps:
            return "-"
        out = []
        for s in steps:
            nm = s.get("name", "?")
            cnt = s.get("result_count")
            err = s.get("error")
            if err:
                out.append(f"{nm}(ERR)")
            else:
                out.append(f"{nm}({cnt}건)")
        return " → ".join(out)
    except Exception:
        return str(chain)[:80]


def render_table(rows: list[dict], hours: int, reason: str | None) -> str:
    out = []
    title = f"최근 {hours}시간 미해결 질의"
    if reason:
        title += f" (필터: fallback_reason={reason})"
    out.append("=" * 100)
    out.append(f"📥 {title} — 총 {len(rows)}건")
    out.append("=" * 100)

    if not rows:
        out.append("")
        out.append("  (적재된 데이터 없음)")
        out.append("")
        out.append("  ※ 참고: AI 가 도구는 정상 호출했지만 '말로만' 정보 없다고 답한 케이스는 적재 안 됩니다.")
        out.append("         google_search 폴백 / 도구 오류 / 결과 0건 인 경우만 적재됩니다.")
        return "\n".join(out)

    for i, r in enumerate(rows, 1):
        when = r["created_kst"].strftime("%m-%d %H:%M:%S") if r["created_kst"] else "-"
        out.append("")
        out.append(f"[{i:>3}] {when} KST  •  id={r['id']}  •  세션={r['session_id'][:8]}…  •  turn#{r['turn_in_group']}")
        out.append(f"      폴백사유: {r['fallback_reason']}   임베딩: {'✅' if r['has_embedding'] else '⏳ 백필 대기'}")
        out.append(f"      🙋 질문: {_shorten(r['user_query'], 200)}")
        if r["ai_final_answer"]:
            out.append(f"      답변: {_shorten(r['ai_final_answer'], 200)}")
        else:
            out.append(f"      답변: (텍스트 없음 — 음성만 출력했을 가능성)")
        out.append(f"      🛠 도구: {_format_tool_chain(r['tool_chain'])}")
        if r["has_grounding"]:
            out.append(f"      🔍 외부 검색 사용됨")

    out.append("")
    out.append("=" * 100)
    out.append(f"※ 임베딩이 비어있으면 매일 23:00 백필 cron (scripts/backfill_embeddings.py) 으로 채워집니다.")
    out.append(f"※ 90일 지나면 자동 파기됩니다 (scripts/purge_old_queries.py).")
    return "\n".join(out)


async def main(hours: int, limit: int, reason: str | None, raw: bool) -> int:
    try:
        rows = await fetch_rows(hours, limit, reason)
        if raw:
            # JSON 그대로 (디버깅용)
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        else:
            print(render_table(rows, hours, reason))
    except Exception as e:
        print(f"❌ 조회 실패: {e}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    return 0


def _parse_args():
    p = argparse.ArgumentParser(description="최근 적재된 미해결 질의 조회")
    p.add_argument("--hours", type=int, default=24, help="조회 기간 (시간, 기본 24)")
    p.add_argument("--limit", type=int, default=30, help="최대 출력 행수 (기본 30)")
    p.add_argument("--reason", choices=sorted(VALID_REASONS),
                   help="특정 폴백사유만 필터 (예: google_search)")
    p.add_argument("--raw", action="store_true",
                   help="표 대신 raw JSON 출력 (디버깅용)")
    return p.parse_args()


if __name__ == "__main__":
    a = _parse_args()
    sys.exit(asyncio.run(main(a.hours, a.limit, a.reason, a.raw)))
