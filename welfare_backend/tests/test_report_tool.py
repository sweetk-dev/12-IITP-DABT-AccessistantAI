# -*- coding: utf-8 -*-
"""음성 접근성 제보 도구 회귀 테스트 (의존성 없이 단독 실행).

    python3 tests/test_report_tool.py

지키는 계약 (v1.35.0):
  1) 좌표는 모델이 아니라 세션이 주입한다 — inject_nav_defaults 가
     user_location 으로 lat/lng 를 덮어쓰고, 안내 중이면 route_id 도 채운다
  2) 위치를 모르면 API 를 호출하지 않고 need_location 으로 안내한다
  3) reason 화이트리스트 밖 값은 etc 로 강등된다
  4) 경로 서비스 오류는 route_client 의 안내문(ai_instruction) 그대로 전달한다
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")

# tool_handlers 는 DB 계층을 import 한다 — 제보 로직만 검증하므로 최소 스텁으로 대체
import types as _t
for name, attrs in (
    ("sqlalchemy", {"select": lambda *a, **k: None, "or_": lambda *a, **k: None,
                    "text": lambda *a, **k: None, "func": _t.SimpleNamespace()}),
    ("sqlalchemy.ext", {}),
    ("sqlalchemy.ext.asyncio", {"AsyncSession": object}),
    ("database", {"AsyncSessionLocal": None, "get_db": None, "engine": None}),
    ("models", {}),
):
    if name not in sys.modules:
        m = _t.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

import route_client                       # noqa: E402
import tool_handlers                      # noqa: E402
from nav_context import inject_nav_defaults  # noqa: E402

CALLS = []


async def _fake_report(payload):
    CALLS.append(payload)
    return {"report_id": 42, "override_id": 7, "message": "접수"}


async def _fake_report_error(payload):
    return {"status": "error", "message": "경로 서비스에 연결하지 못했습니다",
            "ai_instruction": "잠시 후 다시"}


results = []


def check(name, cond, extra=""):
    results.append(("PASS" if cond else "FAIL", name + (" — " + extra if extra and not cond else "")))


async def main():
    route_client.report_accessibility = _fake_report
    tool_handlers.route_client = route_client

    # 1) 정상 접수 + reason 화이트리스트
    r = await tool_handlers.tool_report_accessibility(reason="curb", detail="턱이 높음",
                                                      lat=37.39, lng=126.95, route_id="r_x")
    check("정상 접수 -> success + 감사 안내문", r["status"] == "success" and "감사" in r["ai_instruction"], str(r))
    check("페이로드에 좌표·사유·route_id 전달", CALLS[-1]["lat"] == 37.39 and CALLS[-1]["reason"] == "curb"
          and CALLS[-1]["route_id"] == "r_x", str(CALLS[-1]))

    r = await tool_handlers.tool_report_accessibility(reason="이상한값", lat=37.39, lng=126.95)
    check("화이트리스트 밖 reason -> etc 강등", CALLS[-1]["reason"] == "etc", str(CALLS[-1]))

    # 2) 위치 없음 -> API 미호출
    n0 = len(CALLS)
    r = await tool_handlers.tool_report_accessibility(reason="curb")
    check("위치 없음 -> need_location + API 미호출",
          r["status"] == "need_location" and len(CALLS) == n0, str(r))

    # 3) inject_nav_defaults — 좌표·route_id 는 세션이 채운다 (모델 값 무시)
    fargs = inject_nav_defaults("report_accessibility_issue",
                                {"reason": "steep", "lat": 0.0, "lng": 0.0},
                                {"route_id": "r_nav", "guiding": True},
                                {"lat": 37.4, "lng": 126.92})
    check("좌표는 세션 현재 위치로 덮어씀", fargs["lat"] == 37.4 and fargs["lng"] == 126.92, str(fargs))
    check("안내 중이면 route_id 자동 주입", fargs["route_id"] == "r_nav", str(fargs))

    fargs = inject_nav_defaults("report_accessibility_issue",
                                {"reason": "steep", "lat": 99.9, "lng": 99.9},
                                {}, {"lat": None, "lng": None})
    check("세션 위치 없으면 모델 좌표 제거", "lat" not in fargs and "lng" not in fargs, str(fargs))

    # 4) 경로 서비스 오류 전달
    route_client.report_accessibility = _fake_report_error
    r = await tool_handlers.tool_report_accessibility(reason="curb", lat=37.39, lng=126.95)
    check("서비스 오류 -> route_client 안내문 그대로", r["status"] == "error" and r.get("ai_instruction"), str(r))

    # 5) 디스패처 등록
    disp = tool_handlers.get_tool_dispatcher(lambda x: [0.0])
    check("디스패처에 report_accessibility_issue 등재", "report_accessibility_issue" in disp)


asyncio.run(main())

failed = 0
for st, name in results:
    print(f"  {st}  {name}")
    if st == "FAIL":
        failed += 1
print("\nALL PASSED" if not failed else f"\n{failed} FAILED")
sys.exit(1 if failed else 0)
