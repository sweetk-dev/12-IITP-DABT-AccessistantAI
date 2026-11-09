# model_router.py
# 용도별 생성 모델 선택 · 차선 모델 전환 · 교차검증 설정.
#
# 쓰는 곳 (텍스트 일괄 처리만 — 음성 상담·답변 카드·외부검색이 필요한 호출은 대상이 아니다)
#   policy_update       정책 갱신 패치 (policy_db/crawler/llm_updater.py)
#   weekly_report       주간 리포트 질의 묶음 요약 (scripts/weekly_report.py)
#   discovery_classify  신규 발굴 질의 분류 (discovery_core.py)
#
# 모델 등록부(MODELS)
#   gemini  Google Gemini (generateContent)      — 기본
#   exaone  K-EXAONE (OpenAI 호환)               — 차선·교차검증
#   gemma   온프레미스 Gemma (Ollama / OpenAI 호환 서빙)
#
# 설정 파일: <POLICY_DATA_DIR 또는 policy_db>/model_routing.json
#   {"roles": {"policy_update": {"primary": "gemini", "fallback": ["exaone", "gemma"], "verifier": "exaone"}, ...},
#    "updated_at": "...", "updated_by": "..."}
#   - 운영 콘솔에서 저장한다. 파일이 없거나 용도 항목이 없으면 그 용도는 종전 환경변수 동작
#     (LLM_BACKEND · GEMINI_LLM_MODEL · LLM_FALLBACK)을 그대로 따른다 — explicit_route() 가 None.
#   - 키는 이 파일에 두지 않는다. 각 모델의 키는 환경변수(GEMINI_API_KEY · EXAONE_API_KEY · GEMMA_API_KEY).
#
# 호출 한도
#   EXAONE 은 초당 1회 · 분당 60회 · 하루 100회(응답 헤더 기준)다. 호출 간격을 두고, 하루 한도가
#   소진되면 그날은 건너뛰고 다음 모델로 넘어간다. 사용 기록은 같은 폴더의 model_usage.json
#   (프로세스 간 공유 — 크롤러는 별도 프로세스로 돈다).
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("model_router")

KST = timezone(timedelta(hours=9))
_APP = Path(__file__).resolve().parent


def _data_dir() -> Path:
    # 크롤러는 policy_db 를 작업 폴더로 따로 돈다 — 상대경로여도 프로세스마다 같은 곳을 보도록 절대경로로
    p = Path(os.environ.get("POLICY_DATA_DIR") or str(_APP / "policy_db"))
    return p if p.is_absolute() else (_APP / p).resolve()


def routing_path() -> Path:
    return _data_dir() / "model_routing.json"


def usage_path() -> Path:
    return _data_dir() / "model_usage.json"


# ─────────────────────────────────────────────────────────────
# 등록부
# ─────────────────────────────────────────────────────────────
MODELS = {
    "gemini": {
        "label": "Gemini",
        "provider": "gemini",
        "key_env": "GEMINI_API_KEY",
        "caps": ["json", "text"],
        "note": "기본 모델. 용도별 세부 모델은 GEMINI_LLM_MODEL · GEMINI_MODEL_CLASSIFY 를 따른다.",
    },
    "exaone": {
        "label": "K-EXAONE",
        "provider": "openai_compat",
        "key_env": "EXAONE_API_KEY",
        "base_url_env": "EXAONE_API_URL",
        "base_url_default": "https://api.exaone.ai/v1",
        "model_env": "EXAONE_MODEL",
        "model_default": "k-exaone_v2.5",
        "caps": ["json", "text"],
        # 항상 추론 후 답한다 — 추론 토큰이 max_tokens 에 포함되므로 상한을 넉넉히, 대기 시간도 길게.
        "reasoning_effort_env": "EXAONE_REASONING_EFFORT",
        "reasoning_effort_default": "low",
        "min_max_tokens": 32000,
        "timeout": 420.0,
        "min_interval_sec": 1.2,
        "day_limit_default": 100,
        "note": "국내 모델. 텍스트 전용 · 하루 호출 한도가 있다. 응답이 느려 일괄 처리용.",
    },
    "gemma": {
        "label": "Gemma (온프레미스)",
        "provider": "ollama",
        "key_env": None,
        "caps": ["json", "text"],
        "note": "서버 안에서 도는 모델. 외부 호출이 끊겨도 동작한다.",
    },
}

ROLES = {
    "policy_update": {
        "label": "정책 갱신 패치",
        "desc": "출처 변경을 감지한 정책의 변경 필드 패치를 만든다. 결과는 검토 큐로 간다.",
        "needs": ["json"],
        "verifier_supported": True,
    },
    "weekly_report": {
        "label": "주간 리포트 요약",
        "desc": "미답변 질의를 묶어 요약한다(주간 리포트 --use-llm).",
        "needs": ["text"],
        "verifier_supported": False,
    },
    "discovery_classify": {
        "label": "신규 발굴 질의 분류",
        "desc": "미답변 질의 군집을 새 주제 / 보강 / 기존 정책으로 분류한다. 초안·보강 작성은 외부검색이 필요해 Gemini 고정.",
        "needs": ["json"],
        "verifier_supported": False,
    },
}


class RouterError(RuntimeError):
    pass


# ─────────────────────────────────────────────────────────────
# 설정 읽기·저장
# ─────────────────────────────────────────────────────────────
def _atomic_write_json(p: Path, doc) -> None:
    """같은 폴더의 고유 임시 파일에 쓰고 교체한다(동시에 저장해도 서로의 임시 파일을 덮지 않는다)."""
    import tempfile
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _file_lock(p: Path):
    """프로세스 간 배타 잠금(fcntl). 잠금 기능이 없는 개발 PC(Windows)에서만 잠금 없이 진행."""
    p.parent.mkdir(parents=True, exist_ok=True)
    lockf = open(str(p) + ".lock", "a+")
    try:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl is not None:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
    finally:
        lockf.close()

def _env_default_route(role: str) -> dict:
    """설정 파일이 없을 때의 종전 동작을 등록부 표현으로 옮긴 것(화면 표시용)."""
    backend = (os.environ.get("LLM_BACKEND") or "gemini").lower()
    primary = "gemma" if backend in ("gemma", "ollama") else "gemini"
    fallback = []
    if role == "discovery_classify":
        primary = "gemini"
        if (os.environ.get("LLM_FALLBACK") or "").lower() == "gemma":
            fallback = ["gemma"]
    return {"primary": primary, "fallback": fallback, "verifier": None}


def load_routing() -> dict:
    p = routing_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8")) or {}
        return d if isinstance(d, dict) else {}
    except Exception as e:
        logger.warning("모델 설정 파일을 읽지 못함(종전 동작 사용): %s", e)
        return {}


def explicit_route(role: str) -> Optional[dict]:
    """콘솔에서 저장한 용도 설정. 없으면 None — 호출부는 종전 경로를 그대로 쓴다."""
    r = (load_routing().get("roles") or {}).get(role)
    if not isinstance(r, dict) or not r.get("primary"):
        return None
    return {"primary": r.get("primary"), "fallback": list(r.get("fallback") or []),
            "verifier": r.get("verifier") or None}


def effective_route(role: str) -> dict:
    return explicit_route(role) or _env_default_route(role)


def validate_route(role: str, route: dict) -> dict:
    """저장 전 검사. 정리된 route 를 돌려주고, 잘못되면 RouterError."""
    if role not in ROLES:
        raise RouterError(f"알 수 없는 용도: {role}")
    meta = ROLES[role]
    if not isinstance(route, dict):
        raise RouterError(f"{meta['label']}: 설정 형식이 잘못되었습니다.")
    primary = route.get("primary")
    fb_raw = route.get("fallback") or []
    if not isinstance(fb_raw, list):
        raise RouterError(f"{meta['label']}: 차선 모델은 목록이어야 합니다.")
    fallback = [m for m in fb_raw if m]
    verifier = route.get("verifier") or None

    def _check(mid, what):
        if not isinstance(mid, str) or mid not in MODELS:
            raise RouterError(f"{meta['label']}: {what} 모델 '{mid}' 은 등록되지 않았습니다.")
        missing = [c for c in meta["needs"] if c not in MODELS[mid]["caps"]]
        if missing:
            raise RouterError(f"{meta['label']}: {MODELS[mid]['label']} 은 이 용도에 필요한 기능({', '.join(missing)})이 없습니다.")
        if not configured(mid):
            raise RouterError(f"{meta['label']}: {MODELS[mid]['label']} 은 서버에 키·주소가 설정되지 않아 고를 수 없습니다.")

    if not primary:
        raise RouterError(f"{meta['label']}: 기본 모델을 고르세요.")
    _check(primary, "기본")
    seen = {primary}
    clean_fb = []
    for m in fallback:
        _check(m, "차선")
        if m in seen:
            raise RouterError(f"{meta['label']}: 같은 모델을 두 번 넣을 수 없습니다({MODELS[m]['label']}).")
        seen.add(m)
        clean_fb.append(m)
    if len(clean_fb) > 2:
        raise RouterError(f"{meta['label']}: 차선 모델은 두 개까지입니다.")
    if verifier:
        if not meta["verifier_supported"]:
            raise RouterError(f"{meta['label']}: 교차검증을 지원하지 않는 용도입니다.")
        _check(verifier, "교차검증")
        if verifier == primary:
            raise RouterError(f"{meta['label']}: 교차검증 모델은 기본 모델과 달라야 합니다.")
    return {"primary": primary, "fallback": clean_fb, "verifier": verifier}


def save_routing(roles: dict, actor: str = "admin-console") -> dict:
    if not isinstance(roles, dict):
        raise RouterError("roles 형식이 잘못되었습니다.")
    clean = {}
    for role, route in roles.items():
        if role not in ROLES:
            raise RouterError(f"알 수 없는 용도: {role}")
        clean[role] = None if route is None else validate_route(role, route)   # None = 종전 동작으로 되돌림
    with _file_lock(routing_path()):
        cur = load_routing()
        out_roles = dict(cur.get("roles") or {})
        for role, route in clean.items():
            if route is None:
                out_roles.pop(role, None)
            else:
                out_roles[role] = route
        doc = {"roles": out_roles,
               "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
               "updated_by": (actor or "")[:40]}
        _atomic_write_json(routing_path(), doc)
    return doc


# ─────────────────────────────────────────────────────────────
# 사용 기록 (프로세스 간 공유 · 간격 두기 · 하루 한도)
# ─────────────────────────────────────────────────────────────
def _usage_lock():
    return _file_lock(usage_path())


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _read_usage() -> dict:
    try:
        d = json.loads(usage_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_usage(d: dict) -> None:
    _atomic_write_json(usage_path(), d)


def _model_usage(d: dict, mid: str, day: Optional[str] = None) -> Optional[dict]:
    """그날의 사용 기록. day 를 주면 그 날짜 기록만 돌려준다(이미 날짜가 넘어갔으면 None)."""
    u = d.setdefault(mid, {})
    if day is not None and day != _today():
        return None          # 자정 전에 시작해 자정 뒤에 끝난 호출 — 새 날 기록을 건드리지 않는다
    if u.get("date") != _today():
        last_ts = u.get("last_call_ts", 0)       # 날짜가 바뀌어도 호출 간격 기준은 유지
        u.clear()
        u.update({"date": _today(), "calls": 0, "ok": 0, "fail": 0, "day_remaining": None,
                  "exhausted": False, "last_call_ts": last_ts})
    return u


def _before_call(mid: str) -> str:
    """간격 두기 + 하루 한도 소진 확인. 소진이면 RouterError. 호출을 센 날짜를 돌려준다."""
    spec = MODELS[mid]
    gap = float(spec.get("min_interval_sec") or 0)
    with _usage_lock():
        d = _read_usage()
        u = _model_usage(d, mid)
        if u.get("exhausted"):
            raise RouterError(f"{spec['label']} 오늘 호출 한도 소진")
        wait = (u.get("last_call_ts") or 0) + gap - time.time()
        if wait > 0:
            time.sleep(min(wait, gap))
        u["last_call_ts"] = time.time()
        u["calls"] = int(u.get("calls") or 0) + 1
        _write_usage(d)
        return u["date"]


def _after_call(mid: str, ok: bool, headers=None, exhausted: bool = False, error: str = "",
                day: Optional[str] = None) -> None:
    with _usage_lock():
        d = _read_usage()
        u = _model_usage(d, mid, day)
        if u is None:
            return
        u["ok" if ok else "fail"] = int(u.get("ok" if ok else "fail") or 0) + 1
        if headers is not None:
            rem = headers.get("x-day-rate-limit-remaining")
            lim = headers.get("x-day-rate-limit")
            if rem is not None:
                try:
                    u["day_remaining"] = int(str(rem).strip())
                    if u["day_remaining"] <= 0:
                        u["exhausted"] = True
                except ValueError:
                    pass
            if lim is not None:
                try:
                    u["day_limit"] = int(lim)
                except ValueError:
                    pass
        if exhausted:
            u["exhausted"] = True
        if error:
            u["last_error"] = error[:200]
        u["last_at"] = datetime.now(KST).isoformat(timespec="seconds")
        _write_usage(d)


def usage_today() -> dict:
    d = _read_usage()
    out = {}
    for mid in MODELS:
        u = d.get(mid) or {}
        if u.get("date") != _today():
            u = {}
        out[mid] = {k: u.get(k) for k in ("calls", "ok", "fail", "day_remaining", "day_limit",
                                          "exhausted", "last_error", "last_at")}
    return out


# ─────────────────────────────────────────────────────────────
# 모델 정보
# ─────────────────────────────────────────────────────────────
def model_name(mid: str, role: Optional[str] = None) -> str:
    if mid == "gemini":
        if role == "discovery_classify":
            v = os.environ.get("GEMINI_MODEL_CLASSIFY")
            if v:
                return v.strip()
        return (os.environ.get("GEMINI_LLM_MODEL") or "gemini-3.1-pro-preview").strip()
    if mid == "exaone":
        return (os.environ.get("EXAONE_MODEL") or MODELS["exaone"]["model_default"]).strip()
    if mid == "gemma":
        return (os.environ.get("GEMMA_MODEL") or "gemma4").strip()
    return mid


def configured(mid: str) -> bool:
    env = MODELS[mid].get("key_env")
    if env is None:
        # 온프레미스 서빙 주소가 있거나, 그 서빙을 쓰는 설정(차선·백엔드·음성 폴백)이 켜져 있으면 사용 가능으로 본다
        return bool(os.environ.get("GEMMA_API_URL") or os.environ.get("GEMMA_MODEL")
                    or (os.environ.get("LLM_FALLBACK") or "").lower() == "gemma"
                    or (os.environ.get("LLM_BACKEND") or "").lower() in ("gemma", "ollama")
                    or (os.environ.get("LIVE_LOCAL_FALLBACK") or "").lower() in ("1", "true", "yes"))
    return bool(os.environ.get(env))


def describe() -> dict:
    """콘솔 표시용 — 모델 목록·상태·용도별 설정."""
    use = usage_today()
    models = []
    for mid, spec in MODELS.items():
        models.append({
            "id": mid, "label": spec["label"], "provider": spec["provider"], "caps": spec["caps"],
            "model_name": model_name(mid), "configured": configured(mid), "note": spec.get("note", ""),
            "day_limit": spec.get("day_limit_default"), "usage": use.get(mid) or {},
        })
    cur = load_routing()
    roles = []
    for rid, meta in ROLES.items():
        exp = explicit_route(rid)
        roles.append({
            "id": rid, "label": meta["label"], "desc": meta["desc"], "needs": meta["needs"],
            "verifier_supported": meta["verifier_supported"],
            "explicit": exp is not None, "route": exp or _env_default_route(rid),
            "env_default": _env_default_route(rid),
        })
    return {"models": models, "roles": roles,
            "updated_at": cur.get("updated_at"), "updated_by": cur.get("updated_by")}


# ─────────────────────────────────────────────────────────────
# 공급자별 호출 (동기)
# ─────────────────────────────────────────────────────────────
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _strip_reply(txt: str) -> str:
    t = (txt or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _call_gemini(role, *, prompt, system, json_mode, max_tokens, temperature, timeout=None):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RouterError("GEMINI_API_KEY 미설정")
    base = (os.environ.get("GEMINI_API_URL") or "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{base}/v1beta/models/{model_name('gemini', role)}:generateContent"
    gc = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        gc["responseMimeType"] = "application/json"
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gc}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    last = ""
    for attempt in range(2):        # 일시 오류(5xx)·간헐적 빈 응답 대비 1회 재시도
        r = httpx.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                       json=payload, timeout=httpx.Timeout(timeout or 180.0, connect=10.0))
        if r.status_code == 429:    # 사용량·지출 상한 — 재시도해도 같다. 바로 차선 모델로
            raise RouterError("Gemini HTTP 429(사용량 한도)")
        if r.status_code in _RETRYABLE_STATUS:
            last = f"HTTP {r.status_code}"
            time.sleep(3)
            continue
        if r.status_code != 200:
            raise RouterError(f"Gemini HTTP {r.status_code}")
        try:
            cands = (r.json() or {}).get("candidates") or []
        except ValueError:
            raise RouterError("Gemini 응답이 JSON 이 아님")
        if cands:
            parts = (cands[0].get("content") or {}).get("parts") or []
            txt = "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought"))
            if txt.strip():
                return txt
            last = f"빈 응답(finishReason={cands[0].get('finishReason') or '-'})"
        else:
            last = "빈 응답(candidates 없음)"
        time.sleep(2)
    raise RouterError(f"Gemini {last}")


def _call_exaone(role, *, prompt, system, json_mode, max_tokens, temperature, timeout=None):
    spec = MODELS["exaone"]
    key = os.environ.get(spec["key_env"])
    if not key:
        raise RouterError("EXAONE_API_KEY 미설정")
    base = (os.environ.get(spec["base_url_env"]) or spec["base_url_default"]).rstrip("/")
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    payload = {
        "model": model_name("exaone"), "messages": msgs, "stream": False,
        "max_tokens": max(int(max_tokens or 0), int(spec["min_max_tokens"])),
        "reasoning_effort": (os.environ.get(spec["reasoning_effort_env"]) or spec["reasoning_effort_default"]),
        # 엑사원 권장 샘플링(temperature 1.0 · top_p 0.95). 0 에 가까운 값은 추론 모델에서 반복이 생길 수 있다.
        "temperature": 1.0, "top_p": 0.95,
    }
    if json_mode == "object":
        payload["response_format"] = {"type": "json_object"}
    for attempt in range(2):
        day = _before_call("exaone")
        try:
            r = httpx.post(f"{base}/chat/completions",
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                           json=payload, timeout=httpx.Timeout(timeout or spec["timeout"], connect=10.0))
        except httpx.HTTPError as e:
            _after_call("exaone", False, error=f"{type(e).__name__}", day=day)
            raise RouterError(f"K-EXAONE 연결 실패: {type(e).__name__}")
        hdr = {k.lower(): v for k, v in r.headers.items()}
        if r.status_code == 429:
            exhausted = False
            try:
                exhausted = int(str(hdr.get("x-day-rate-limit-remaining")).strip()) <= 0
            except (TypeError, ValueError):
                pass
            _after_call("exaone", False, headers=hdr, exhausted=exhausted, error="429", day=day)
            try:
                wait = float(hdr.get("retry-after") or 2)
            except ValueError:
                wait = 2.0
            if exhausted or attempt == 1 or wait > 65:
                raise RouterError("K-EXAONE 호출 한도 초과(429)" + (" — 오늘 한도 소진" if exhausted else ""))
            time.sleep(min(max(wait, 1.0), 65.0))      # 분당 한도까지 기다릴 수 있게
            continue
        if r.status_code in (500, 502, 503, 504) and attempt == 0:
            _after_call("exaone", False, headers=hdr, error=f"HTTP {r.status_code}", day=day)
            time.sleep(3)
            continue
        if r.status_code != 200:
            # 본문은 남기지 않는다(인증 오류 응답에 요청 정보가 섞여 나올 수 있다)
            _after_call("exaone", False, headers=hdr, error=f"HTTP {r.status_code}", day=day)
            raise RouterError(f"K-EXAONE HTTP {r.status_code}")
        try:
            body = r.json() or {}
            msg = ((body.get("choices") or [{}])[0] or {}).get("message") or {}
        except (ValueError, AttributeError, IndexError):
            _after_call("exaone", False, headers=hdr, error="응답 형식 오류", day=day)
            raise RouterError("K-EXAONE 응답 형식 오류")
        txt = (msg.get("content") or "").strip()
        if not txt:
            _after_call("exaone", False, headers=hdr, error="빈 응답", day=day)
            raise RouterError("K-EXAONE 빈 응답(추론 토큰이 상한을 다 썼을 수 있음)")
        _after_call("exaone", True, headers=hdr, day=day)
        return txt
    raise RouterError("K-EXAONE 일시 오류 반복")


def _call_gemma(role, *, prompt, system, json_mode, max_tokens, temperature, timeout=None):
    base = (os.environ.get("GEMMA_API_URL") or "http://ollama:11434").rstrip("/")
    model = model_name("gemma")
    style = (os.environ.get("GEMMA_API_STYLE") or "ollama").lower()
    key = os.environ.get("GEMMA_API_KEY")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    timeout = httpx.Timeout(timeout or 300.0, connect=10.0)
    if style == "openai":
        body = {"model": model, "messages": msgs, "temperature": temperature,
                "max_tokens": min(max_tokens, 16000)}
        if json_mode == "object":
            body["response_format"] = {"type": "json_object"}
        r = httpx.post(f"{base}/v1/chat/completions", headers=headers, timeout=timeout, json=body)
        r.raise_for_status()
        try:
            return ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except (ValueError, AttributeError, IndexError):
            raise RouterError("Gemma 응답 형식 오류")
    payload = {"model": model, "messages": msgs, "stream": False, "think": False, "keep_alive": -1,
               "options": {"temperature": temperature, "num_predict": min(max_tokens, 16000)}}
    if json_mode:
        payload["format"] = "json"
    r = httpx.post(f"{base}/api/chat", headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    m = r.json().get("message") or {}
    return (m.get("content") or m.get("thinking") or "")


_CALLERS = {"gemini": _call_gemini, "exaone": _call_exaone, "gemma": _call_gemma}


def call_model(mid: str, *, prompt: str, system: Optional[str] = None, json_mode=None,
               max_tokens: int = 16000, temperature: float = 0.0, role: Optional[str] = None,
               timeout: Optional[float] = None) -> str:
    """모델 하나를 호출해 본문 문자열을 돌려준다. 실패는 RouterError(또는 httpx 예외)."""
    if mid not in _CALLERS:
        raise RouterError(f"알 수 없는 모델: {mid}")
    kw = {"timeout": timeout} if timeout else {}
    return _CALLERS[mid](role, prompt=prompt, system=system, json_mode=json_mode,
                         max_tokens=max_tokens, temperature=temperature, **kw)


def generate(role: str, *, prompt: str, system: Optional[str] = None, json_mode=None,
             max_tokens: int = 16000, temperature: float = 0.0, route: Optional[dict] = None) -> dict:
    """용도 설정의 기본 → 차선 순으로 호출. 반환 {"text", "model", "attempts":[{model, ok, error}]}.
    json_mode: None | "object" | "array" — 모델별 JSON 강제 방식이 다르다(엑사원은 object 만 강제)."""
    route = route or effective_route(role)
    chain = [route["primary"]] + [m for m in route.get("fallback") or [] if m != route["primary"]]
    attempts = []
    for mid in chain:
        if not configured(mid):
            attempts.append({"model": mid, "ok": False, "error": "설정 없음(키·주소)"})
            continue
        t0 = time.time()
        try:
            txt = call_model(mid, prompt=prompt, system=system, json_mode=json_mode,
                             max_tokens=max_tokens, temperature=temperature, role=role)
            if not (txt or "").strip():
                raise RouterError("빈 응답")
            attempts.append({"model": mid, "ok": True, "sec": round(time.time() - t0, 1)})
            if len(attempts) > 1:
                logger.info("[%s] 차선 모델 %s 사용 (앞선 실패: %s)", role, mid,
                            "; ".join(f"{a['model']}={a.get('error')}" for a in attempts[:-1]))
            return {"text": txt, "model": mid, "attempts": attempts}
        except Exception as e:
            err = str(e) if isinstance(e, RouterError) else f"{type(e).__name__}: {str(e)[:120]}"
            logger.warning("[%s] %s 호출 실패: %s", role, mid, err)
            attempts.append({"model": mid, "ok": False, "error": err, "sec": round(time.time() - t0, 1)})
    raise RouterError("모든 모델 호출 실패 — " + "; ".join(f"{a['model']}: {a['error']}" for a in attempts))


async def agenerate(role: str, **kw) -> dict:
    import asyncio
    return await asyncio.to_thread(generate, role, **kw)


async def acall_model(mid: str, **kw) -> str:
    import asyncio
    return await asyncio.to_thread(call_model, mid, **kw)


# ─────────────────────────────────────────────────────────────
# 시험 호출 (콘솔)
# ─────────────────────────────────────────────────────────────
_TEST_PROMPT = ("다음 문장에서 정책명과 감면율을 JSON 객체로 뽑으세요. 키: name, rate_severe, rate_mild. "
                "문장: '철도요금은 중증 장애인 50%, 경증 장애인 30% 감면된다.'")


def test_model(mid: str) -> dict:
    if mid not in MODELS:
        return {"ok": False, "error": "알 수 없는 모델"}
    if not configured(mid):
        return {"ok": False, "error": "설정 없음(키·주소)"}
    t0 = time.time()
    try:
        # 콘솔 요청이 프록시 대기 시간 안에 끝나도록 짧게 기다린다. 추론 토큰을 감안해 출력 상한은 넉넉히.
        txt = call_model(mid, prompt=_TEST_PROMPT, json_mode="object", max_tokens=8192, role="test", timeout=50.0)
        sec = round(time.time() - t0, 1)
        try:
            parsed = json.loads(_strip_reply(txt))
            ok = isinstance(parsed, dict) and "rate_severe" in parsed
        except Exception:
            parsed, ok = None, False
        return {"ok": ok, "sec": sec, "model_name": model_name(mid), "sample": _strip_reply(txt)[:200],
                "error": None if ok else "JSON 형식이 아님"}
    except Exception as e:
        return {"ok": False, "sec": round(time.time() - t0, 1), "error": str(e)[:200]}


# ─────────────────────────────────────────────────────────────
# 교차검증 — 두 모델의 정책 패치 비교
# ─────────────────────────────────────────────────────────────
# 비교 단위는 "사실 토큰"이다. 문장 표현은 보지 않고 값 속의 날짜·숫자(단위 포함)를 뽑되,
#   - 숫자 앞에 붙은 중증/경증 구분을 함께 묶는다      → "중증 50%·경증 30%" 와 뒤바뀐 값을 구별
#   - 숫자 뒤의 이상/이하/미만/초과/까지/부터를 묶는다 → "65세 이상" 과 "65세 미만" 을 구별
#   - 천 단위 쉼표·만원/천원·범위(18~65세)를 맞춘다   → 같은 금액·범위를 다르게 쓴 것은 같게
# 숫자가 없는 값(신청 방법 문장 등)은 경로가 같으면 같은 것으로 본다 — 화면에 그 한계를 적는다.
_DATE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?")
_MONTH_RE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월(?!\s*\d)|(\d{4})\.(\d{1,2})(?![.\d])")
_UNIT = r"(?:%|퍼센트|만\s*원|천\s*원|원|명|인|세|개월|년|일|회|급|석|km|m)"
_RANGE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*[~∼\-]\s*(\d[\d,]*(?:\.\d+)?)\s*(" + _UNIT + r")")
_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(" + _UNIT + r")?")
_QUAL_RE = re.compile(r"^\s*(이상|이하|미만|초과|까지|부터|이내)")
_SEV_RE = re.compile(r"(중증|경증|심하지\s*않은|심한)")
_SEP = "/,;|·\n"


def _norm_num(num: str, unit: str) -> str:
    n = num.replace(",", "")
    u = re.sub(r"\s+", "", unit or "").replace("퍼센트", "%")
    try:
        if u == "만원":
            return f"{int(round(float(n) * 10000))}원"
        if u == "천원":
            return f"{int(round(float(n) * 1000))}원"
    except ValueError:
        pass
    if u in ("명", "인"):
        u = "명"
    return n + u


def _sev_label(s: str, start: int, end: int) -> str:
    """숫자 바로 앞(구분자·다른 숫자 이후 14자 안) 또는 바로 뒤 괄호 안의 중증/경증 표시."""
    head = s[max(0, start - 14):start]
    for ch in _SEP:
        if ch in head:
            head = head[head.rfind(ch) + 1:]
    digits = [i for i, c in enumerate(head) if c.isdigit()]
    if digits:                      # 사이에 다른 숫자가 있으면 그 숫자의 구분이다
        head = head[digits[-1] + 1:]
    found = _SEV_RE.findall(head)
    if not found:
        tail = s[end:end + 8]
        m = re.match(r"^\s*[(（]\s*(중증|경증)", tail)
        found = [m.group(1)] if m else []
    if not found:
        return ""
    w = re.sub(r"\s+", "", found[-1])
    return "중증:" if w in ("중증", "심한") else "경증:"


def _facts(v) -> set:
    """값에서 사실 토큰을 뽑는다. 문장 표현이 달라도 사실이 같으면 같은 집합."""
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = s or ""
    out = set()
    for y, mo, d in _DATE_RE.findall(s):
        out.add(f"{int(y)}-{int(mo):02d}-{int(d):02d}")
    s = _DATE_RE.sub(" ", s)
    for y1, m1, y2, m2 in _MONTH_RE.findall(s):
        y, mo = (y1, m1) if y1 else (y2, m2)
        out.add(f"{int(y)}-{int(mo):02d}")
    s = _MONTH_RE.sub(" ", s)

    def _range(m):
        unit = m.group(3)
        return f" {m.group(1)}{unit} 부터 {m.group(2)}{unit} 까지 "
    s = _RANGE_RE.sub(_range, s)
    for m in _NUM_RE.finditer(s):
        tok = _norm_num(m.group(1), m.group(2))
        q = _QUAL_RE.match(s[m.end():m.end() + 4])
        if q:
            tok += q.group(1)
        out.add(_sev_label(s, m.start(), m.end()) + tok)
    return out


def _flatten(path: str, val, out: dict, op: str) -> None:
    """dict 값은 잎 경로로 펼친다 — supported_amount={rate:..} 와 supported_amount.rate=.. 를 같게 본다."""
    if isinstance(val, dict) and val:
        for k, v in val.items():
            _flatten(f"{path}.{k}", v, out, op)
        return
    out.setdefault((op, path), []).append(val)


def _patch_map(patches) -> dict:
    m = {}
    for p in patches or []:
        if not isinstance(p, dict):
            continue
        path, op = p.get("path"), p.get("op") or "update"
        if not isinstance(path, str) or not path or not isinstance(op, str):
            continue
        val = p.get("new") if op == "update" else p.get("value")
        _flatten(path, val, m, op)
    return m


def compare_patches(primary, verifier) -> dict:
    """두 패치 목록을 비교. status: agree | disagree.
    - 바꾼 필드(잎 경로)가 한쪽에만 있으면 불일치
    - 같은 필드라도 값의 사실 토큰이 다르면 불일치 — 문장 표현 차이는 보지 않는다."""
    a, b = _patch_map(primary), _patch_map(verifier)
    only_a = sorted(f"{k[0]} {k[1]}" for k in a.keys() - b.keys())
    only_b = sorted(f"{k[0]} {k[1]}" for k in b.keys() - a.keys())
    fact_diff = []
    for k in sorted(a.keys() & b.keys()):
        fa = set().union(*(_facts(x) for x in a[k]))
        fb = set().union(*(_facts(x) for x in b[k]))
        if fa != fb:
            fact_diff.append({"path": k[1], "op": k[0],
                              "only_primary": sorted(fa - fb), "only_verifier": sorted(fb - fa)})
    status = "agree" if not (only_a or only_b or fact_diff) else "disagree"
    return {"status": status, "only_primary": only_a, "only_verifier": only_b, "fact_diff": fact_diff,
            "n_primary": len(primary or []), "n_verifier": len(verifier or [])}
