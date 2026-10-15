# -*- coding: utf-8 -*-
"""데이터 수집 배치 상태 · 이상 알림 설정 (운영 콘솔 [운영] 탭, v2.2.0 · 분류 묶음·종류별 현황 v2.3.0
· 고용 파일데이터 표별 기준일 v2.5.0).

무엇을 보여 주나
  데이터 수집 배치(08-IITP-DABT-PreProcessing)는 KOSIS 통계와 이동편의 소스를 정해진 주기로
  수집해 iitp_db 에 적재하고, 실행 1회마다 sys_batch_run_log 에 결과를 남긴다(08 v1.17.0).
  실패하거나 실패 뒤 정상으로 돌아오면 sys_ops_setting 의 수신 주소로 알림 메일을 보낸다.
  이 모듈은 그 기록을 작업(ext_sys)별로 묶어 콘솔에 보여 주고, 수신 주소·사용 여부를 고친다.

iitp_db 쓰기 범위
  콘솔의 iitp_db 세션은 원래 조회 전용이다. 이 모듈만 예외로 sys_ops_setting 의 알림 설정 두 행
  (batch_alert_enabled, batch_alert_emails)을 UPDATE 한다. 다른 표·다른 키는 쓰지 않는다.

표가 없을 때
  01 v1.9.0 마이그레이션 전이면 두 표가 없다. 그때는 ready=False 를 돌려주고 화면은 안내만 한다.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

SETTING_ENABLED = "batch_alert_enabled"
SETTING_EMAILS = "batch_alert_emails"
MAX_RECIPIENTS = 5
# 08 ops_alert.EMAIL_RE 와 같은 규칙 — 콘솔에서 통과한 주소는 배치도 받아들인다.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# 작업 이름 · 정기 실행 주기(수집 서버 cron 기준) · 화면 분류.
#   주기는 화면 안내용이다 — cron 을 바꾸면 여기도 고친다.
#   분류는 카드에서 작업을 묶어 보여 주는 단위다(JOB_GROUPS 순서). 같은 분류는 이웃하게 적는다.
#   등록되지 않은 작업(실행 기록만 있는 것)은 '기타' 로 맨 뒤에 붙는다.
JOB_GROUPS = ("통계", "이동편의", "문화·체육", "기타")
JOBS = {
    "KOSIS": ("KOSIS 통계", "매월 3·18일 03:00", "통계"),
    "ODCLOUD_EMP": ("장애인 고용 파일데이터(14종)", "매월 3일 04:40", "통계"),
    "GBIS": ("경기 버스 노선(GBIS)", "매주 월 04:00", "이동편의"),
    "GBIS_LOWFLOOR": ("저상버스 운행 노선", "매일 04:30", "이동편의"),
    "KORAIL_CONV": ("철도역 편의시설", "매월 1일 04:30", "이동편의"),
    "KOWSI_FACL": ("건물 편의시설", "매월 22일 04:10", "이동편의"),
    "TOUR_BF_API": ("무장애 관광지", "매주 일 04:00", "이동편의"),
    "GG_TOILET": ("경기 공중화장실", "분기 1일 04:20", "이동편의"),
    "GG_CULTURE": ("경기 문화·체육 시설(4종)", "매월 2일 04:20", "문화·체육"),
}

# 한 작업이 여러 종류를 함께 수집하는 경우의 종류별 현재 적재 건수(활성 행).
#   실행 기록의 처리 건수는 합계뿐이라 한 종류가 통째로 빠져도(예: 5건짜리 종류 0건) 눈에 띄지
#   않는다. 표의 현재 상태를 종류별로 세어 함께 보여 준다. 표가 아직 없으면(DB 마이그레이션 전) 생략.
#   table 과 sql 은 코드 상수다(사용자 입력이 들어가지 않는다).
#   sql 은 (종류 키, 건수) 또는 (종류 키, 건수, 메모) 를 돌려준다. 메모는 화면에 괄호로 붙는다(예: 기준일).

# 장애인 고용 파일데이터(08 ODCLOUD_EMP, v1.19.0) — 표 14개. 표마다 현재 행 수와 적재된 판의 기준일
# (sys_data_summary_info.src_latest_chn_dt)을 보여 준다. 기준일이 오래 그대로면 원천 갱신이 없거나
# 그 표만 계속 실패하고 있다는 뜻이다(실패 목록과 함께 본다).
_EMP_TABLES = (
    ("emp_dis_jobseeker_status", "구직자"), ("emp_dis_job_posting", "구인"),
    ("emp_dis_center_usage", "훈련센터"), ("emp_dis_obligation_status", "고용의무"),
    ("emp_dis_consulting_his", "컨설팅"), ("emp_dis_burden_workplace", "연계고용"),
    ("emp_dis_regional_status", "지역별"), ("emp_dis_staff_train_crs", "전문인력교육"),
    ("emp_dis_dev_support_org", "발달지원기관"), ("emp_dis_startup_lecture", "창업강좌"),
    ("emp_dis_std_workplace", "표준사업장"), ("emp_dis_emp_incentive", "고용장려금"),
    ("emp_dis_obligation_by_indust", "산업별"), ("emp_dis_obligation_fulfillment", "의무이행"),
)
_EMP_SQL = (
    "SELECT c.k, c.n, d.src_latest_chn_dt FROM ("
    + " UNION ALL ".join("SELECT '%s' AS k, count(*) AS n FROM public.%s" % (t, t) for t, _ in _EMP_TABLES)
    + ") c LEFT JOIN public.sys_data_summary_info d"
      " ON d.sys_tbl_id = c.k AND d.del_yn = 'N' AND d.status = 'A'"
)

JOB_BREAKDOWN = {
    "GG_CULTURE": {
        "table": "public.poi_culture_sports_facility",     # 01 v1.10.0
        "sql": ("SELECT facility_type, count(*) FROM public.poi_culture_sports_facility"
                " WHERE del_yn = 'N' GROUP BY facility_type"),
        "labels": (("braille_lib", "점자도서관"), ("booknarae_lib", "책나래 제공도서관"),
                   ("sports_fac", "장애인체육시설"), ("sports_org", "장애인체육회")),
    },
    "ODCLOUD_EMP": {
        "table": "public.emp_dis_jobseeker_status",         # 01 emp init
        "sql": _EMP_SQL,
        "labels": _EMP_TABLES,
    },
}


class AlertConfigError(ValueError):
    """수신 설정 입력이 잘못됐을 때. 메시지는 화면에 그대로 보여 준다."""


def validate_emails(raw: Any) -> list[str]:
    """입력(문자열 또는 목록) → 형식이 맞는 주소 목록. 하나라도 틀리면 AlertConfigError.

    배치 쪽 해석(틀린 주소는 버리고 나머지로 발송)과 달리, 입력 단계에서는 틀린 주소를 알려
    고치게 한다. 중복은 하나로 합치고, 5개를 넘으면 거부한다.
    """
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = re.split(r"[,;\s]+", str(raw or ""))
    out: list[str] = []
    bad: list[str] = []
    for p in parts:
        addr = p.strip()
        if not addr:
            continue
        if not EMAIL_RE.match(addr):
            bad.append(addr)
            continue
        if addr.lower() not in [a.lower() for a in out]:
            out.append(addr)
    if bad:
        raise AlertConfigError("메일 주소 형식이 맞지 않습니다: " + ", ".join(bad[:5]))
    if len(out) > MAX_RECIPIENTS:
        raise AlertConfigError(f"수신 주소는 최대 {MAX_RECIPIENTS}개까지 입력할 수 있습니다.")
    return out


async def tables_ready(db) -> bool:
    r = (await db.execute(text(
        "SELECT to_regclass('public.sys_batch_run_log') IS NOT NULL"
        " AND to_regclass('public.sys_ops_setting') IS NOT NULL"))).scalar()
    return bool(r)


async def get_alert_config(db) -> dict:
    rows = (await db.execute(text(
        "SELECT setting_key, setting_value, to_char(updated_at AT TIME ZONE 'Asia/Seoul',"
        " 'YYYY-MM-DD HH24:MI') AS updated_at, updated_by FROM sys_ops_setting"
        " WHERE setting_key IN (:a, :b)"), {"a": SETTING_ENABLED, "b": SETTING_EMAILS})).mappings().all()
    by = {r["setting_key"]: r for r in rows}
    en = by.get(SETTING_ENABLED)
    em = by.get(SETTING_EMAILS)
    emails = [e for e in re.split(r"[,;\s]+", (em["setting_value"] if em else "") or "") if e]
    latest = max((r for r in rows), key=lambda r: r["updated_at"] or "", default=None)
    return {
        "enabled": (en["setting_value"] if en else "Y").strip().upper() != "N",
        "emails": emails,
        "updated_at": latest["updated_at"] if latest else None,
        "updated_by": latest["updated_by"] if latest else None,
    }


async def save_alert_config(db, enabled: Any, emails: Any, actor: str = "admin-console") -> dict:
    """알림 설정 저장. 두 행만 UPSERT 하고 커밋한다. 잘못된 입력은 AlertConfigError."""
    if not isinstance(enabled, bool):
        raise AlertConfigError("enabled 는 true/false 여야 합니다.")
    addrs = validate_emails(emails)
    if enabled and not addrs:
        raise AlertConfigError("알림을 켜려면 수신 주소를 하나 이상 입력해야 합니다.")
    sql = text(
        "INSERT INTO sys_ops_setting (setting_key, setting_value, updated_at, updated_by)"
        " VALUES (:k, :v, CURRENT_TIMESTAMP, :u)"
        " ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value,"
        " updated_at = CURRENT_TIMESTAMP, updated_by = EXCLUDED.updated_by")
    try:
        await db.execute(sql, {"k": SETTING_ENABLED, "v": "Y" if enabled else "N", "u": actor[:40]})
        await db.execute(sql, {"k": SETTING_EMAILS, "v": ", ".join(addrs), "u": actor[:40]})
        await db.commit()
    except Exception:
        # 두 행 중 하나만 바뀐 채 남지 않게 되돌리고, 실패한 트랜잭션이 세션에 남지 않게 한다.
        await db.rollback()
        raise
    return await get_alert_config(db)


def _fmt(ts) -> str | None:
    return ts.strftime("%Y-%m-%d %H:%M") if ts else None


async def job_breakdown(db, ext_sys: str):
    """JOB_BREAKDOWN 에 있는 작업의 종류별 현재 적재 건수. 대상이 아니거나 표가 없으면 None.

    표 존재를 먼저 확인한다 — 없는 표를 조회하면 오류로 세션 트랜잭션이 중단돼 같은 요청의 다른
    조회까지 실패하기 때문이다. 표는 있는데 칸이 다르거나 권한이 없는 과도기에도 카드 전체가
    실패하지 않도록, 집계는 세이브포인트 안에서 돌리고 실패하면 그 부분만 되돌린 뒤 None 을 준다.
    반환: [{"key", "label", "count", "note"}] (정의 순서, 없는 종류는 0, 메모가 없으면 note=None).
    """
    spec = JOB_BREAKDOWN.get(ext_sys)
    if not spec:
        return None
    exists = (await db.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": spec["table"]})).scalar()
    if not exists:
        return None
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(spec["sql"]))).all()
    except Exception as exc:  # noqa: BLE001 — 현황 한 줄 때문에 수집 상태 전체를 실패시키지 않는다
        logger.warning("수집 작업 %s 종류별 현황 조회 실패: %s", ext_sys, exc)
        return None
    counts = {r[0]: int(r[1]) for r in rows}
    notes = {r[0]: (str(r[2]) if r[2] is not None else None) for r in rows if len(r) > 2}
    return [{"key": k, "label": label, "count": counts.get(k, 0), "note": notes.get(k)}
            for k, label in spec["labels"]]


async def collect_status(db, history: int = 10) -> dict:
    """작업(ext_sys)별 최근 실행 상태. 정기 실행(db 모드·전체 범위) 기준으로 연속 실패를 센다.

    반환: {"ready": bool, "jobs": [...], "alert": {...}}
      jobs[i] = {ext_sys, label, schedule, group, last_sync, last_run: {...}|None, consecutive_fail,
                 last_success, recent: [최근 실행 요약 history 건], breakdown: [...]|None}
      jobs 는 분류(JOB_GROUPS) 순서로 정렬된다 — 같은 분류 안에서는 JOBS 에 적은 순서.
    """
    if not await tables_ready(db):
        return {"ready": False, "jobs": [], "alert": None}
    syncs = {r["ext_sys"]: r for r in (await db.execute(text(
        "SELECT ext_sys, if_name, latest_sync_time AT TIME ZONE 'Asia/Seoul' AS latest_sync"
        " FROM sys_ext_api_info WHERE del_yn = 'N'"))).mappings().all()}
    rows = (await db.execute(text(
        "SELECT run_id, ext_sys, run_mode, COALESCE(run_scope, 'ALL') AS run_scope,"
        " started_at AT TIME ZONE 'Asia/Seoul' AS started_at,"
        " finished_at AT TIME ZONE 'Asia/Seoul' AS finished_at, exit_code, status, targets, ok_cnt,"
        " fail_cnt, failures, error_summary, alert_status, alert_detail"
        " FROM (SELECT *, row_number() OVER (PARTITION BY ext_sys ORDER BY started_at DESC, run_id DESC) rn"
        "       FROM sys_batch_run_log) t WHERE rn <= 60 ORDER BY ext_sys, started_at DESC, run_id DESC"
    ))).mappings().all()
    by_sys: dict[str, list] = {}
    for r in rows:
        by_sys.setdefault(r["ext_sys"], []).append(r)
    order = list(JOBS) + sorted(set(by_sys) - set(JOBS))
    jobs = []
    for ext_sys in order:
        runs = by_sys.get(ext_sys, [])
        if ext_sys not in JOBS and not runs:
            continue
        if ext_sys not in syncs and not runs:
            continue      # 등록되지 않은 소스는 숨긴다
        regular = [r for r in runs if r["run_mode"] == "db" and r["run_scope"] == "ALL"]
        consecutive, last_ok = 0, None
        for r in regular:
            if r["status"] == "SUCCESS":
                last_ok = r["started_at"]
                break
            consecutive += 1
        label, schedule, group = JOBS.get(ext_sys, (ext_sys, "-", "기타"))
        sync = syncs.get(ext_sys)
        jobs.append({
            "ext_sys": ext_sys, "label": label, "schedule": schedule, "group": group,
            "breakdown": await job_breakdown(db, ext_sys),
            "last_sync": _fmt(sync["latest_sync"]) if sync else None,
            "last_run": _run_view(runs[0]) if runs else None,
            "last_regular": _run_view(regular[0]) if regular else None,
            "consecutive_fail": consecutive,
            "last_success": _fmt(last_ok),
            "recent": [_run_view(r, brief=True) for r in runs[:history]],
        })
    # 분류 순서로 안정 정렬 — 화면이 분류 머리행을 한 번씩만 그리게 한다.
    rank = {g: i for i, g in enumerate(JOB_GROUPS)}
    jobs.sort(key=lambda j: rank.get(j["group"], len(JOB_GROUPS)))
    return {"ready": True, "jobs": jobs, "alert": await get_alert_config(db)}


def _run_view(r, brief: bool = False) -> dict:
    v = {
        "run_id": r["run_id"], "started_at": _fmt(r["started_at"]), "finished_at": _fmt(r["finished_at"]),
        "mode": r["run_mode"], "scope": r["run_scope"], "status": r["status"],
        "targets": r["targets"], "ok_cnt": r["ok_cnt"], "fail_cnt": r["fail_cnt"],
        "alert_status": r["alert_status"], "alert_detail": r["alert_detail"],
    }
    if not brief:
        raw = r["failures"]
        # jsonb 는 드라이버 설정에 따라 문자열로 올 수도 있다 — 목록으로 맞춘다.
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = []
        v["failures"] = list(raw or [])[:50] if isinstance(raw, list) else []
        v["error_summary"] = r["error_summary"]
    return v
