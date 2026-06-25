"""scripts/weekly_report.py
Track A — 주간 미해결 질의 분석 리포트.

기본 모드 (무료, 결정적):
  최근 7일 unresolved_queries 의 통계 리포트 생성.
    - 총 건수, fallback_reason 분포
    - 일별 추이 (date, count)
    - Top user_query (출현 빈도 기준, embedding 있으면 군집 대표 텍스트)
    - Top intent_group_id (turn 수 많은 의도)
  결과: welfare_backend/reports/unresolved/weekly_YYYY-MM-DD.md

선택 모드 (--use-llm, LLM API 비용 발생):
  위 통계 + 누적 질문들을 LLM(기본 Gemini)에 던져 의도 클러스터링 + 신규 정책 후보 도출.

실행:
  python -m scripts.weekly_report                 # 통계만
  python -m scripts.weekly_report --use-llm       # LLM 클러스터링 포함
  python -m scripts.weekly_report --days 14       # 분석 기간 변경

cron 등록 예 (Linux):
  0 6 * * 1 cd /opt/welfare_backend && /usr/bin/python3 -m scripts.weekly_report >> /var/log/welfare/weekly.log 2>&1
  (매주 월요일 06:00)
"""
import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv(_ROOT / ".env")

from database import engine  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("weekly_report")

REPORT_DIR = _ROOT / "reports" / "unresolved"


# ─────────────────────────────────────────────────────────────
# 통계 수집
# ─────────────────────────────────────────────────────────────
async def collect_stats(days: int) -> dict:
    """최근 N 일 통계 수집."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with engine.connect() as conn:
        # 1) 총 건수
        total = (await conn.execute(text(
            "SELECT count(*) FROM unresolved_queries WHERE created_at >= :since"
        ), {"since": since})).scalar()

        # 2) fallback_reason 분포
        reasons = (await conn.execute(text(
            "SELECT fallback_reason::text, count(*) "
            "FROM unresolved_queries WHERE created_at >= :since "
            "GROUP BY fallback_reason ORDER BY count(*) DESC"
        ), {"since": since})).all()

        # 3) 일별 추이
        daily = (await conn.execute(text(
            "SELECT DATE(created_at AT TIME ZONE 'UTC') AS d, count(*) "
            "FROM unresolved_queries WHERE created_at >= :since "
            "GROUP BY d ORDER BY d"
        ), {"since": since})).all()

        # 4) user_query 빈도 (정확 일치 기준 — 임베딩 군집은 별도)
        top_queries = (await conn.execute(text(
            "SELECT user_query, count(*) FROM unresolved_queries "
            "WHERE created_at >= :since "
            "GROUP BY user_query ORDER BY count(*) DESC LIMIT 20"
        ), {"since": since})).all()

        # 5) intent_group_id 별 turn 수 (재발화 패턴)
        top_groups = (await conn.execute(text(
            "SELECT intent_group_id, count(*) FROM unresolved_queries "
            "WHERE created_at >= :since "
            "GROUP BY intent_group_id HAVING count(*) > 1 "
            "ORDER BY count(*) DESC LIMIT 10"
        ), {"since": since})).all()

        # 6) 임베딩 채움률 (백필 cron 헬스 확인)
        embed_status = (await conn.execute(text(
            "SELECT count(*) FILTER (WHERE embedding IS NOT NULL) AS filled, "
            "       count(*) AS total "
            "FROM unresolved_queries WHERE created_at >= :since"
        ), {"since": since})).first()

    return {
        "since": since,
        "total": total,
        "reasons": [(r[0], r[1]) for r in reasons],
        "daily": [(str(r[0]), r[1]) for r in daily],
        "top_queries": [(r[0], r[1]) for r in top_queries],
        "top_groups": [(str(r[0]), r[1]) for r in top_groups],
        "embed_filled": embed_status[0] if embed_status else 0,
        "embed_total": embed_status[1] if embed_status else 0,
    }


# ─────────────────────────────────────────────────────────────
# 리포트 작성
# ─────────────────────────────────────────────────────────────
def render_markdown(stats: dict, days: int, llm_section: str = "") -> str:
    lines = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines.append(f"# Track A 주간 리포트 ({today})\n")
    lines.append(f"분석 기간: 최근 **{days}일** (since {stats['since'].strftime('%Y-%m-%d %H:%M UTC')})\n")
    lines.append(f"총 미해결 질의: **{stats['total']}건**\n")

    if stats["total"] == 0:
        lines.append("\n> 이 기간에 적재된 미해결 질의가 없습니다.\n")
        return "\n".join(lines)

    # 임베딩 채움률
    if stats["embed_total"]:
        pct = 100 * stats["embed_filled"] / stats["embed_total"]
        lines.append(f"\n임베딩 채움률: **{stats['embed_filled']}/{stats['embed_total']}** "
                     f"({pct:.1f}%) — 백필 cron 헬스 지표\n")

    # 폴백 사유 분포
    lines.append("\n## 폴백 사유 분포\n")
    lines.append("| 사유 | 건수 | 비율 |")
    lines.append("|---|---:|---:|")
    for reason, cnt in stats["reasons"]:
        pct = 100 * cnt / stats["total"]
        lines.append(f"| `{reason}` | {cnt} | {pct:.1f}% |")

    # 일별 추이
    lines.append("\n## 일별 추이\n")
    lines.append("| 날짜 | 건수 |")
    lines.append("|---|---:|")
    for d, c in stats["daily"]:
        lines.append(f"| {d} | {c} |")

    # Top queries
    lines.append("\n## 자주 나타난 질의 (정확 일치 기준 Top 20)\n")
    lines.append("| # | 건수 | 질의 |")
    lines.append("|---:|---:|---|")
    for i, (q, c) in enumerate(stats["top_queries"], 1):
        q_short = q.replace("|", "\\|")[:120]
        lines.append(f"| {i} | {c} | {q_short} |")

    # Top intent groups (재발화)
    if stats["top_groups"]:
        lines.append("\n## 재발화 의도 그룹 (Top 10)\n")
        lines.append("같은 의도(intent_group_id)에 turn 이 여러 번 묶인 케이스 — 사용자가 같은 질문을 반복하거나 AI 가 적절히 답하지 못한 신호.\n")
        lines.append("| intent_group_id | turn 수 |")
        lines.append("|---|---:|")
        for gid, c in stats["top_groups"]:
            lines.append(f"| `{gid[:8]}…` | {c} |")

    if llm_section:
        lines.append("\n## LLM 클러스터링\n")
        lines.append(llm_section)

    lines.append("\n---\n")
    lines.append("*자동 생성 — `python -m scripts.weekly_report`*\n")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# LLM 클러스터링 (옵션)
# ─────────────────────────────────────────────────────────────
async def llm_cluster(stats: dict) -> str:
    """LLM(기본 Gemini)으로 의도 클러스터링 + 신규 정책 후보 도출. 백엔드는 LLM_BACKEND 로 선택."""
    if stats["total"] == 0:
        return "_데이터 없음 — LLM 분석 생략_"

    queries_text = "\n".join(f"- ({c}회) {q}" for q, c in stats["top_queries"])
    prompt = f"""아래는 한국 장애인 복지 정책 AI 상담봇에서 최근 답변하지 못한 질문 목록입니다.
질문들을 의미적으로 묶어 *클러스터*를 만들고, 각 클러스터에 대해:
  - 대표 의도 한 줄 요약
  - 묶인 질문 개수
  - 이 의도를 다루는 신규 정책 항목(B040+) 발굴 후보인지 여부 (yes/no + 이유)

질문 목록:
{queries_text}

마크다운 표 형태로 답해주세요."""

    try:
        try:
            from policy_db.crawler.llm_backends import get_backend
        except ImportError:
            sys.path.insert(0, str(_ROOT / "policy_db" / "crawler"))
            from llm_backends import get_backend  # type: ignore
        backend = get_backend()
        return await backend.generate_text(prompt=prompt, max_tokens=4000)
    except Exception as e:
        logger.warning("LLM 클러스터링 호출 실패: %s", e)
        return f"_LLM 분석 실패: {e}_"


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────
async def main(days: int, use_llm: bool) -> int:
    logger.info("=== 주간 리포트 생성 시작 (days=%d, use_llm=%s) ===", days, use_llm)
    try:
        stats = await collect_stats(days)
        logger.info("통계 수집 완료: 총 %d건", stats["total"])

        llm_section = ""
        if use_llm and stats["total"] > 0:
            logger.info("LLM 클러스터링 호출 중...")
            llm_section = await llm_cluster(stats)

        md = render_markdown(stats, days, llm_section)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORT_DIR / f"weekly_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
        out.write_text(md, encoding="utf-8")
        logger.info("✅ 리포트 저장: %s", out)
        print(f"\n=== Report saved: {out} ===\n")
    except Exception as e:
        logger.exception("리포트 생성 실패: %s", e)
        return 1
    finally:
        await engine.dispose()
    return 0


def _parse_args():
    p = argparse.ArgumentParser(description="UnresolvedQuery 주간 분석 리포트")
    p.add_argument("--days", type=int, default=7, help="분석 기간 (일, 기본 7)")
    p.add_argument("--use-llm", action="store_true",
                   help="LLM(기본 Gemini)으로 의도 클러스터링 추가 (비용 발생)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(asyncio.run(main(args.days, args.use_llm)))
