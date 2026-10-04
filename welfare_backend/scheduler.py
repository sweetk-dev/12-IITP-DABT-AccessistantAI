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
import sys
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
    "discovery_cron": {"day": "1,15", "hour": 4, "minute": 0},   # 신규 발굴(B) — 04:00(09시가 늦어 조정)
    "backup_cron": {"hour": 4, "minute": 0},
    "embed_cron": {"minute": "*/15"},  # 미답변 질의 임베딩 백필(발굴 전처리)
    # 미답변 질의 보존기간 파기 — 매일 03:40.
    # 시각 근거: 이용이 거의 없는 새벽이면서 다른 잡과 겹치지 않는 분.
    #   임베딩 백필은 매시 0·15·30·45분, 백업·발굴은 04:00, 크롤·재검증은 09:00 에 돈다.
    #   04:00 백업보다 앞이라 그날 백업에는 파기 후 상태가 담긴다.
    "purge_cron": {"hour": 3, "minute": 40},
    # 미답변 질의 보존 일수 — scripts/purge_old_queries 의 기본값(90일)과 같은 값.
    "unresolved_retention_days": 90,
    "backup_retention_days": 30,
}

_status = {
    "crawl":  {"running": False, "label": None, "last_run": None, "last_status": None, "last_output": None},
    "backup": {"running": False, "last_run": None, "last_status": None, "last_output": None},
    "discovery": {"running": False, "last_run": None, "last_status": None, "last_output": None},
    "embed": {"running": False, "last_run": None, "last_status": None, "last_output": None},
    "purge": {"running": False, "last_run": None, "last_status": None, "last_output": None},
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
        # sys.executable + cwd 지정: "python" 이름과 현재 작업 디렉터리에 기대면, 서버를
        # 가상환경의 인터프리터로 띄웠거나 다른 디렉터리에서 기동한 경우 다른 파이썬이
        # 실행되거나 scripts 패키지를 찾지 못한다(-m 은 cwd 기준으로 모듈을 찾는다).
        cmd = [sys.executable, "-m", "scripts.backfill_embeddings", "--batch-size", "50", "--max-rows", "500"]
        r = subprocess.run(cmd, cwd=str(_APP), capture_output=True, text=True, timeout=1800)
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


def _run_purge():
    """미답변 질의 보존기간 파기 잡 — scripts.purge_old_queries 를 하위 프로세스로 실행한다.

    스크립트는 보존기간이 지난 unresolved_queries 행을 지운다(발굴 후보가 근거로 참조
    중인 행은 남긴다). 스케줄에 이 잡이 없으면 스크립트를 따로 cron 에 걸지 않은 배포에서는
    파기가 한 번도 실행되지 않아 사용자 발화 텍스트가 기한 없이 쌓인다.

    결과는 다른 잡과 같이 _status["purge"] 에 남는다(콘솔 운영 상태 API 로 확인 가능).
    실패(종료 코드 0 아님·시간 초과·실행 불가)해도 예외를 밖으로 내지 않는다.
    """
    with _lock:
        if _status["purge"]["running"]:
            return
        _status["purge"]["running"] = True
    logger.info("미답변 질의 보존기간 파기 시작")
    try:
        try:
            days = int(_load_cfg().get("unresolved_retention_days", 90))
        except (TypeError, ValueError):
            days = 90
        if days < 1:
            # 0 이하이면 전 행이 대상이 된다. 설정 파일 오기로 보고 기본값을 쓴다.
            days = 90
        cmd = [sys.executable, "-m", "scripts.purge_old_queries", "--days", str(days)]
        # 제한 600초: 조건이 있는 DELETE 한 번이라 보통 수 초 안에 끝난다.
        # 10분을 넘기면 DB 잠금 대기 등 비정상 상태로 보고 중단한다.
        r = subprocess.run(cmd, cwd=str(_APP), capture_output=True, text=True, timeout=600)
        combined = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        out = "\n".join(combined.splitlines()[-8:])
        st = "ok" if r.returncode == 0 else f"exit {r.returncode}"
        with _lock:
            _status["purge"].update(running=False, last_run=_now(), last_status=st, last_output=out)
        logger.info("미답변 질의 파기 종료: %s", st)
    except Exception as e:
        with _lock:
            _status["purge"].update(running=False, last_run=_now(), last_status="error", last_output=str(e)[:500])
        logger.exception("미답변 질의 파기 실패: %s", e)


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
    # 미답변 질의 보존기간 파기 — 매일 1회(기본 03:40, 스케줄러 시간대 = KST)
    pc = cfg.get("purge_cron", DEFAULT_CFG["purge_cron"])
    _sched.add_job(_run_purge, CronTrigger(hour=pc.get("hour", 3), minute=pc.get("minute", 40), timezone=KST),
                   id="purge_scheduled", replace_existing=True)
    _sched.start()
    logger.info("스케줄러 기동 — 해시=%s 재검증=%s 백업=%s", cc, rc, bc)
