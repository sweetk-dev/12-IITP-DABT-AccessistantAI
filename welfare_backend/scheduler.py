# scheduler.py
# 관리자 콘솔용 운영 스케줄러 — 크롤/백업의 정기 실행 + "지금 실행"(백그라운드) + 상태.
#   - APScheduler(BackgroundScheduler) 단일 인스턴스(단일 uvicorn 워커 전제).
#   - 잡은 블로킹(subprocess/tar)이라 스레드풀에서 실행. run-now 는 즉시 add_job.
#   - 스케줄 기본값은 코드 + /data 설정파일(admin_schedule.json). 편집 UI 는 v2.
import glob
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:  # tzdata 미설치 컨테이너 대비 — 한국은 DST 없음(고정 +9)
    KST = timezone(timedelta(hours=9))
from pathlib import Path

logger = logging.getLogger("scheduler")

_APP = Path(__file__).resolve().parent                 # /app
_POLICYDB = _APP / "policy_db"
_DATA = Path(os.environ.get("POLICY_DATA_DIR") or str(_POLICYDB))
_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))
_SCHED_CFG = _DATA / "admin_schedule.json"

DEFAULT_CFG = {
    "crawl_cron": {"day": "2,16", "hour": 9, "minute": 0},       # 해시 감지(저비용)
    "revalidate_cron": {"day": "25", "hour": 9, "minute": 0},    # 전체 재검증(전수)
    "discovery_cron": {"day": "1,15", "hour": 9, "minute": 0},   # 신규 발굴(B, 별도 구현)
    "backup_cron": {"hour": 4, "minute": 0},
    "embed_cron": {"minute": "*/15"},  # 미답변 질의 임베딩 백필(발굴 전처리)
    "backup_retention_days": 30,
}

_status = {
    "crawl":  {"running": False, "label": None, "last_run": None, "last_status": None, "last_output": None},
    "backup": {"running": False, "last_run": None, "last_status": None, "last_output": None},
    "discovery": {"running": False, "last_run": None, "last_status": None, "last_output": None},
    "embed": {"running": False, "last_run": None, "last_status": None, "last_output": None},
}
_lock = threading.Lock()
_sched = None


def _load_cfg():
    try:
        return {**DEFAULT_CFG, **json.loads(_SCHED_CFG.read_text(encoding="utf-8"))}
    except Exception:
        return dict(DEFAULT_CFG)


def _now():
    return datetime.now(KST).isoformat(timespec="seconds")


def _run_crawl(extra_args=None, label="full"):
    with _lock:
        if _status["crawl"]["running"]:
            return
        _status["crawl"]["running"] = True
        _status["crawl"]["label"] = label
    logger.info("크롤 시작(백그라운드) — %s", label)
    try:
        cmd = ["python", "-m", "crawler.crawler"] + list(extra_args or [])
        r = subprocess.run(cmd, cwd=str(_POLICYDB),
                           capture_output=True, text=True, timeout=3600)
        combined = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        out = "\n".join(combined.splitlines()[-15:])
        st = "ok" if r.returncode == 0 else f"exit {r.returncode}"
        with _lock:
            _status["crawl"].update(running=False, last_run=_now(), last_status=st, last_output=out)
        logger.info("크롤 종료: %s", st)
    except Exception as e:
        with _lock:
            _status["crawl"].update(running=False, last_run=_now(), last_status="error", last_output=str(e)[:500])
        logger.exception("크롤 실패: %s", e)


def _run_backup():
    with _lock:
        if _status["backup"]["running"]:
            return
        _status["backup"]["running"] = True
    logger.info("백업 시작")
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
        fn = _BACKUP_DIR / f"policy_data_{ts}.tar.gz"
        r = subprocess.run(["tar", "czf", str(fn), "-C", str(_DATA), "."],
                           capture_output=True, text=True, timeout=600)
        # 보존기간 정리
        days = _load_cfg().get("backup_retention_days", 30)
        cutoff = time.time() - days * 86400
        for f in glob.glob(str(_BACKUP_DIR / "policy_data_*.tar.gz")):
            try:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
            except OSError:
                pass
        if r.returncode == 0 and fn.exists():
            out = f"{fn.name} ({fn.stat().st_size // 1024}KB)"
            st = "ok"
        else:
            out = (r.stderr or "")[:300]
            st = f"exit {r.returncode}"
        with _lock:
            _status["backup"].update(running=False, last_run=_now(), last_status=st, last_output=out)
        logger.info("백업 종료: %s", st)
    except Exception as e:
        with _lock:
            _status["backup"].update(running=False, last_run=_now(), last_status="error", last_output=str(e)[:500])
        logger.exception("백업 실패: %s", e)


def _run_embed():
    with _lock:
        if _status["embed"]["running"]:
            return
        _status["embed"]["running"] = True
    logger.info("미답변 임베딩 백필 시작")
    try:
        cmd = ["python", "-m", "scripts.backfill_embeddings", "--batch-size", "50", "--max-rows", "500"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        combined = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        out = "\n".join(combined.splitlines()[-8:])
        st = "ok" if r.returncode == 0 else f"exit {r.returncode}"
        with _lock:
            _status["embed"].update(running=False, last_run=_now(), last_status=st, last_output=out)
        logger.info("임베딩 백필 종료: %s", st)
    except Exception as e:
        with _lock:
            _status["embed"].update(running=False, last_run=_now(), last_status="error", last_output=str(e)[:500])
        logger.exception("임베딩 백필 실패: %s", e)


def _start_crawl(extra_args, label):
    with _lock:
        if _status["crawl"]["running"]:
            return {"started": False, "reason": "이미 실행 중"}
    if not _sched:
        return {"started": False, "reason": "스케줄러 미기동"}
    _sched.add_job(_run_crawl, args=[extra_args, label], id="crawl_now", replace_existing=True)
    return {"started": True, "label": label}


def run_crawl_now():
    # 수동 '지금 크롤 실행' 기본 = 재검증(해시 무관 전수 재검증, 무변경은 staging 미생성)
    return _start_crawl(["--revalidate"], "재검증(전체)")


def run_crawl_hashcheck():
    # 해시 빠른검사(변경된 출처만)
    return _start_crawl([], "해시검사(전체)")


def run_crawl_policy(policy_id):
    return _start_crawl(["--policy", policy_id, "--revalidate"], f"재검증 {policy_id}")


def run_init_baseline(policy_id=None):
    extra = ["--init-baseline"] + (["--policy", policy_id] if policy_id else [])
    return _start_crawl(extra, ("기준확정 " + policy_id) if policy_id else "기준확정 전체")


def run_backup_now():
    with _lock:
        if _status["backup"]["running"]:
            return {"started": False, "reason": "이미 실행 중"}
    if not _sched:
        return {"started": False, "reason": "스케줄러 미기동"}
    _sched.add_job(_run_backup, id="backup_now", replace_existing=True)
    return {"started": True}


def _run_discovery():
    with _lock:
        if _status["discovery"]["running"]:
            return
        _status["discovery"]["running"] = True
    logger.info("신규 발굴 시작")
    try:
        import discovery_core as dc
        res = dc.run_discovery()
        with _lock:
            _status["discovery"].update(running=False, last_run=_now(), last_status="ok",
                                        last_output=json.dumps(res, ensure_ascii=False)[:400])
        logger.info("신규 발굴 완료: %s", res)
    except Exception as e:
        with _lock:
            _status["discovery"].update(running=False, last_run=_now(), last_status="error", last_output=str(e)[:400])
        logger.exception("신규 발굴 실패: %s", e)


def run_discovery_now():
    with _lock:
        if _status["discovery"]["running"]:
            return {"started": False, "reason": "이미 실행 중"}
    if not _sched:
        return {"started": False, "reason": "스케줄러 미기동"}
    _sched.add_job(_run_discovery, id="discovery_now", replace_existing=True)
    return {"started": True}


def get_status():
    with _lock:
        st = json.loads(json.dumps(_status))
    st["schedules"] = _load_cfg()
    st["next"] = {}
    if _sched:
        for j in _sched.get_jobs():
            st["next"][j.id] = j.next_run_time.isoformat() if j.next_run_time else None
    return st


def start():
    global _sched
    if _sched:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    cfg = _load_cfg()
    cc = cfg["crawl_cron"]; rc = cfg.get("revalidate_cron", DEFAULT_CFG["revalidate_cron"]); bc = cfg["backup_cron"]
    _sched = BackgroundScheduler(timezone=KST)
    # 해시 감지(2·16) — args 없음(=변경된 출처만)
    _sched.add_job(_run_crawl, CronTrigger(day=str(cc.get("day", "2,16")),
                   hour=cc.get("hour", 9), minute=cc.get("minute", 0), timezone=KST),
                   args=[[], "해시검사(정기)"], id="crawl_scheduled", replace_existing=True)
    # 전체 재검증(25) — --revalidate
    _sched.add_job(_run_crawl, CronTrigger(day=str(rc.get("day", "25")),
                   hour=rc.get("hour", 9), minute=rc.get("minute", 0), timezone=KST),
                   args=[["--revalidate"], "재검증(정기)"], id="revalidate_scheduled", replace_existing=True)
    dc_cron = cfg.get("discovery_cron", DEFAULT_CFG["discovery_cron"])
    _sched.add_job(_run_discovery, CronTrigger(day=str(dc_cron.get("day", "1,15")),
                   hour=dc_cron.get("hour", 9), minute=dc_cron.get("minute", 0), timezone=KST),
                   id="discovery_scheduled", replace_existing=True)
    _sched.add_job(_run_backup, CronTrigger(hour=bc.get("hour", 4), minute=bc.get("minute", 0), timezone=KST),
                   id="backup_scheduled", replace_existing=True)
    ec = cfg.get("embed_cron", DEFAULT_CFG["embed_cron"])
    _sched.add_job(_run_embed, CronTrigger(minute=str(ec.get("minute", "*/15")), timezone=KST),
                   id="embed_scheduled", replace_existing=True)
    _sched.add_job(_run_embed, id="embed_startup", replace_existing=True)  # 기동 직후 1회 catch-up
    _sched.start()
    logger.info("스케줄러 기동 — 해시=%s 재검증=%s 백업=%s", cc, rc, bc)
