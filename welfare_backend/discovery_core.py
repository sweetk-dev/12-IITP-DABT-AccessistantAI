# discovery_core.py — Phase 5 신규 정책 발굴(Track B)
# 미답변 질의 → 군집화 → 분류(정책관련/신규) → 외부검색+초안 → 후보 저장(검토 전용).
# 동기 실행(스케줄러 백그라운드 잡). psycopg2 + requests(Gemini generateContent).
# 안전: 새 정책을 자동 생성하지 않음. "후보 초안"만 저장하고 관리자 승인 시에만 items 반영.
import glob
import json
import logging
import math
import os
from datetime import date, datetime
from pathlib import Path

import psycopg2
import requests

logger = logging.getLogger("discovery")

_APP = Path(__file__).resolve().parent
_DATA = Path(os.environ.get("POLICY_DATA_DIR") or str(_APP / "policy_db"))
_ITEMS = _DATA / "items"
_CAND_DIR = _DATA / "discovery" / "candidates"
_STAGING_DIR = _DATA / "crawler" / "staging"  # 기존 검토 큐(staging) 재사용 — 보강(gap) 업데이트 적재
_REPORT_DIR = _DATA / "discovery" / "reports"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_LLM_MODEL", "gemini-3.1-pro-preview")
_GEN_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_GEN_URL = f"{_GEN_BASE}/{GEMINI_MODEL}:generateContent"  # 기본(main) 모델 — 하위호환


def _gen_url(model):
    return f"{_GEN_BASE}/{(model or GEMINI_MODEL).strip()}:generateContent"


def _model_for(role):
    """호출 항목별 모델 선택(비용 분리). 환경변수 GEMINI_MODEL_<ROLE> 미설정 시 기본 모델.
    예) 분류(classify)는 저렴·빠른 모델로: GEMINI_MODEL_CLASSIFY=gemini-3.1-flash-preview
    draft/enrich/gap 은 품질이 필요하면 기본(pro) 유지."""
    return (os.environ.get(f"GEMINI_MODEL_{role.upper()}") or GEMINI_MODEL).strip()
SIM_THRESHOLD = 0.82

# 직전 _gemini 호출 실패 사유(429/한도초과 등) — 호출부가 사용자에게 원인 표시용
_LAST_GEMINI_ERR = ""


def _db():
    return psycopg2.connect(dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                            password=os.environ["DB_PASS"], host=os.environ["DB_HOST"],
                            port=os.environ["DB_PORT"])


def _cosine(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


def _load_unresolved(limit=500):
    con = _db(); cur = con.cursor()
    cur.execute("SELECT id, user_query, embedding FROM unresolved_queries "
                "WHERE embedding IS NOT NULL AND discovery_processed_at IS NULL "
                "ORDER BY created_at DESC LIMIT %s", (limit,))
    rows = cur.fetchall(); con.close()
    out = []
    for rid, q, emb in rows:
        if isinstance(emb, str):
            try:
                emb = [float(x) for x in emb.strip("[]").split(",") if x.strip()]
            except Exception:
                emb = None
        if emb:
            out.append({"id": rid, "q": q, "emb": list(emb)})
    return out


def _cluster(rows):
    clusters = []
    for r in rows:
        for c in clusters:
            if _cosine(r["emb"], c["centroid"]) >= SIM_THRESHOLD:
                c["members"].append(r)
                break
        else:
            clusters.append({"centroid": r["emb"], "members": [r]})
    return clusters


def _gemini(prompt, grounding=False, max_tokens=24000, retries=3, model=None):
    """Gemini generateContent. thinking 모델의 간헐적 빈 응답에 대비해 재시도."""
    import time as _t
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}}
    if grounding:
        payload["tools"] = [{"google_search": {}}]
    else:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    global _LAST_GEMINI_ERR
    url = _gen_url(model)
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_API_KEY,
                              "Content-Type": "application/json"}, json=payload, timeout=(10, 120))
            # 사용량/과금 한도(429)는 재시도 무의미 — 사유를 명확히 잡아 즉시 종료
            if r.status_code == 429:
                detail = ""
                try:
                    detail = ((r.json() or {}).get("error") or {}).get("message", "") or ""
                except Exception:
                    pass
                last_err = "API 사용량 한도 초과(429)"
                if "spend" in detail.lower() or "지출" in detail or "spending cap" in detail.lower():
                    last_err = "API 월 지출 상한 초과(429) — Gemini spend cap 확인 필요"
                break
            r.raise_for_status()
            cands = r.json().get("candidates") or []
            if cands:
                parts = (cands[0].get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought"))
                if txt.strip():
                    _LAST_GEMINI_ERR = ""
                    return txt
                fr = (cands[0].get("finishReason") or "")
                last_err = f"빈 응답(finishReason={fr})" if fr else "빈 응답"
            else:
                last_err = "빈 응답(candidates 없음)"
        except Exception as e:
            last_err = str(e)
        if attempt < retries - 1:
            _t.sleep(2 * (attempt + 1))
    _LAST_GEMINI_ERR = last_err or "알 수 없는 오류"
    logger.warning("Gemini 응답 실패(%d회): %s", retries, last_err)
    # 차선책 가드: Gemini 실패(429 한도/장애/타임아웃) 시 온프레미스 Gemma 로 폴백.
    # LLM_FALLBACK=gemma 일 때만 동작(미설정 시 기존과 동일). grounding 은 폴백에서 불가(지식기반 답).
    if (os.environ.get("LLM_FALLBACK") or "").lower() == "gemma":
        logger.info("→ Gemma(온프레미스) 차선책 폴백 시도 (사유: %s)", last_err)
        fb = _gemma_generate(prompt, max_tokens=min(max_tokens, 8000))
        if fb.strip():
            _LAST_GEMINI_ERR = f"{last_err} (Gemma 폴백 사용)"
            return fb
    return ""


def _gemma_generate(prompt, max_tokens=8000):
    """온프레미스 Gemma(Ollama 기본 / OpenAI 호환) 동기 호출 — Gemini 차선책.
    환경변수(llm_backends.py 와 동일 체계):
      GEMMA_API_URL  (기본 http://ollama:11434 — compose 내부 서비스명)
      GEMMA_MODEL    (예: gemma4)
      GEMMA_API_STYLE ollama|openai (기본 ollama)
      GEMMA_API_KEY  (선택)
    grounding 미지원 → 모델 지식 기반 답변(검토 필수)."""
    base = (os.environ.get("GEMMA_API_URL") or "http://ollama:11434").rstrip("/")
    model = os.environ.get("GEMMA_MODEL", "gemma4")
    style = (os.environ.get("GEMMA_API_STYLE") or "ollama").lower()
    key = os.environ.get("GEMMA_API_KEY")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        if style == "openai":
            url = f"{base}/v1/chat/completions"
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "max_tokens": max_tokens}
            r = requests.post(url, headers=headers, json=payload, timeout=(10, 180))
            r.raise_for_status()
            return (r.json()["choices"][0]["message"].get("content") or "")
        url = f"{base}/api/chat"
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "options": {"temperature": 0, "num_predict": max_tokens}, "stream": False}
        r = requests.post(url, headers=headers, json=payload, timeout=(10, 180))
        r.raise_for_status()
        return ((r.json().get("message") or {}).get("content") or "")
    except Exception as e:
        logger.warning("Gemma 폴백 실패: %s", e)
        return ""


def _parse_json(raw):
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:].strip() if raw[:4].lower() == "json" else raw
    # grounding 응답은 앞뒤 텍스트가 섞일 수 있어 첫 { ~ 마지막 } 추출 폴백
    try:
        return json.loads(raw)
    except Exception:
        i, j = raw.find("{"), raw.rfind("}")
        k, l = raw.find("["), raw.rfind("]")
        if k != -1 and (i == -1 or k < i):
            return json.loads(raw[k:l + 1])
        return json.loads(raw[i:j + 1])


def _existing_titles():
    out = []
    for f in glob.glob(str(_ITEMS / "B0*.json")):
        try:
            d = json.loads(open(f, encoding="utf-8").read())
            out.append((d.get("id"), d.get("title"), d.get("category")))
        except Exception:
            pass
    return out


def _schema_enums():
    """schema.json 에서 enum 제약을 읽어 초안 프롬프트에 주입(스키마와 동기화)."""
    try:
        sc = json.loads((_APP / "policy_db" / "schema.json").read_text(encoding="utf-8"))
        P = sc.get("properties", {})
        def en(k):
            return P.get(k, {}).get("enum") or []
        return {"leaflet_section": en("leaflet_section"), "category": en("category"),
                "benefit_type": en("benefit_type")}
    except Exception:
        return {"leaflet_section": [], "category": [], "benefit_type": []}


def _ensure_processed_col():
    con = _db(); cur = con.cursor()
    cur.execute("ALTER TABLE unresolved_queries ADD COLUMN IF NOT EXISTS discovery_processed_at TIMESTAMPTZ")
    con.commit(); con.close()


def _mark_processed(ids):
    if not ids:
        return
    con = _db(); cur = con.cursor()
    cur.execute("UPDATE unresolved_queries SET discovery_processed_at = NOW() WHERE id = ANY(%s)", (list(ids),))
    con.commit(); con.close()


def run_discovery():
    """미답변 질의 → 군집 → 분류 → 신규 후보 초안 저장. 반환: 요약."""
    if not GEMINI_API_KEY:
        return {"error": "GEMINI_API_KEY 없음"}
    try:
        _ensure_processed_col()
    except Exception as e:
        return {"error": f"마이그레이션 실패: {e}"}
    rows = _load_unresolved()
    if not rows:
        return {"clusters": 0, "candidates": 0, "note": "임베딩된 미답변 질의 없음"}
    clusters = _cluster(rows)
    titles = _existing_titles()
    titles_txt = "\n".join(f"- {i}: {t} ({c})" for i, t, c in titles)
    reps = [{"idx": k, "query": c["members"][0]["q"], "count": len(c["members"])}
            for k, c in enumerate(clusters)]

    clf_prompt = (
        "다음은 장애인 정책 음성 Q&A가 답하지 못한 사용자 질문 군집의 대표 문장입니다.\n"
        "각 군집을 분류하세요. 기존 정책 목록을 참고해 이미 다루는 주제인지 판단하세요.\n\n"
        f"[기존 정책]\n{titles_txt}\n\n"
        "[질문 군집]\n" + "\n".join(f'{r["idx"]}: "{r["query"]}" (유사 {r["count"]}건)' for r in reps) + "\n\n"
        "JSON 배열로만 답: "
        '[{"idx":0,"policy_related":true,"klass":"new|gap|covered","covered_by":"B0xx 또는 null","gap_detail":"klass=gap일 때 빠진 세부","topic":"주제 한 줄"}]\n'
        "policy_related=장애인 지원정책/제도 관련(아이돌·영화·잡담은 false). "
        "klass: 기존 정책에 전혀 없는 새 주제=new / 기존 정책 B0xx가 주제는 다루나 질문이 요구하는 세부가 빠짐=gap(covered_by 필수, gap_detail 기재) / 기존 정책이 이미 충분히 답함=covered. "
        "보수적으로: 확신 없으면 gap 으로 분류하지 말고 new 또는 covered 로(신뢰된 기존 정책을 잘못 건드리지 않도록)."
    )
    try:
        clf = _parse_json(_gemini(clf_prompt, model=_model_for("classify")))
    except Exception as e:
        return {"clusters": len(clusters), "candidates": 0, "error": f"분류 실패: {e}"}

    enums = _schema_enums()
    new_cl = [c for c in clf if c.get("policy_related") and c.get("klass") == "new"]
    gap_cl = [c for c in clf if c.get("policy_related") and c.get("klass") == "gap" and c.get("covered_by")]
    _CAND_DIR.mkdir(parents=True, exist_ok=True)
    created = []
    for c in new_cl:
        try:
            cl = clusters[c["idx"]]
        except (KeyError, IndexError):
            continue
        topic = c.get("topic", "")
        member_qs = [m["q"] for m in cl["members"]]
        member_ids = [m["id"] for m in cl["members"]]
        draft_prompt = (
            "대한민국 장애인 지원 정책 중 다음 주제의 신규 정책 항목 초안을 작성하세요. "
            "공식 출처(law.go.kr·보건복지부·복지로 등)를 웹에서 찾아 근거로 쓰고, 추측 금지.\n"
            f"주제: {topic}\n관련 질문: {member_qs}\n\n"
            "JSON 하나만 출력(설명·코드블록 없이). 스키마 enum 을 반드시 지키세요.\n"
            f"- leaflet_section 은 반드시 다음 중 하나: {enums['leaflet_section']} (지역·기타 정책은 \"기타\")\n"
            f"- category 는 반드시 다음 중 하나: {enums['category']}\n"
            f"- benefit_type 은 반드시 다음 중 하나: {enums['benefit_type']} (현금 지급은 \"현금지급\")\n"
            "키: id(빈 문자열), leaflet_section, leaflet_number(0), title, short_summary, category, "
            "benefit_type, supported_amount{rate,amount,scope}, eligibility{target}, legal_basis(배열), "
            'legal_basis(배열, 각 항목은 객체 {"name":법령명(필수), "article":조항(선택), "url":(선택)} — 확실치 않으면 빈 배열 []), ' "how_to_use{default}, application{}, last_verified, version(\"1.0.0\"), "
            'sources(배열, 각 항목 필수키 title·publisher·url + priority 는 ["primary","secondary","supplementary"] 중 하나, 최소 1개 실제 URL). '
            "확인 안 되는 필드는 보수적으로 비우되(배열은 [], 객체는 {}) sources 는 실제 URL 과 publisher 를 포함. 모든 배열·객체는 위 키 구조를 지킬 것."
        )
        draft = None
        try:
            draft = _parse_json(_gemini(draft_prompt, grounding=True, model=_model_for("draft")))
        except Exception as e:
            logger.warning("초안 실패(topic=%s): %s", topic, e)
        cid = "C" + datetime.now().strftime("%Y%m%d%H%M%S%f")[:18]
        cand = {"candidate_id": cid, "topic": topic, "cluster_queries": member_qs,
                "query_ids": member_ids, "classification": c, "draft_item": draft, "status": "pending",
                "created_at": datetime.now().isoformat(timespec="seconds")}
        (_CAND_DIR / f"{cid}.json").write_text(json.dumps(cand, ensure_ascii=False, indent=2), encoding="utf-8")
        created.append(cid)

    # 보강(gap): 기존 정책 B0xx 누락 세부를 채워 기존 검토 큐(staging)로 적재(사람 승인 필요)
    gaps = []
    for c in gap_cl:
        try:
            cl = clusters[c["idx"]]
        except (KeyError, IndexError):
            continue
        try:
            r = _make_gap_staged(c.get("covered_by"), [m["q"] for m in cl["members"]],
                                 [m["id"] for m in cl["members"]],
                                 c.get("gap_detail") or c.get("topic") or "")
            if r.get("ok"):
                gaps.append({"policy_id": r["policy_id"], "changed": r.get("changed")})
        except Exception as e:
            logger.warning("보강 staged 실패(pid=%s): %s", c.get("covered_by"), e)

    # 처리한 질의는 '발굴됨'으로 표시 → 다음 발굴에서 제외(중복 후보 방지)
    processed_ids = [m["id"] for cl in clusters for m in cl["members"]]
    try:
        _mark_processed(processed_ids)
    except Exception as e:
        logger.warning("processed 표시 실패: %s", e)

    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {"date": date.today().isoformat(), "clusters": len(clusters),
               "classified": clf, "new": len(new_cl), "gap": len(gap_cl), "candidates": created, "gaps": gaps}
    (_REPORT_DIR / f"{date.today().isoformat()}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"clusters": len(clusters), "new_candidates": len(created), "gap_staged": len(gaps), "processed": len(processed_ids)}


def list_candidates():
    _CAND_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(_CAND_DIR.glob("C*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            di = d.get("draft_item") or {}
            out.append({"candidate_id": d["candidate_id"], "topic": d.get("topic"),
                        "title": di.get("title"), "category": di.get("category"),
                        "status": d.get("status"), "n_queries": len(d.get("cluster_queries") or []),
                        "has_draft": bool(d.get("draft_item")), "created_at": d.get("created_at"),
                        "status_at": d.get("status_at")})
        except Exception:
            pass
    return out


def candidate_query_index():
    """후보 파일의 cluster_queries 역색인 → {질의문: {candidate_id,status,topic}}.
    미답변질의 '반영'(=신규 후보로 분류된 질의) 판정에 사용. approved 후보를 우선."""
    _CAND_DIR.mkdir(parents=True, exist_ok=True)
    idx = {}
    for f in _CAND_DIR.glob("C*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        info = {"candidate_id": d.get("candidate_id"), "status": d.get("status"), "topic": d.get("topic")}
        for q in (d.get("cluster_queries") or []):
            if q not in idx or info.get("status") == "approved":
                idx[q] = info
    return idx


def get_candidate(cid):
    f = _CAND_DIR / f"{cid}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"error": "후보 없음"}


def set_status(cid, status, policy_id=None):
    f = _CAND_DIR / f"{cid}.json"
    if not f.exists():
        return {"ok": False, "error": "후보 없음"}
    d = json.loads(f.read_text(encoding="utf-8"))
    d["status"] = status
    d["status_at"] = datetime.now().isoformat(timespec="seconds")
    if policy_id:
        d["approved_policy_id"] = policy_id
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    reopened = 0
    if status == "rejected" and d.get("query_ids"):
        try:
            reopened = _reopen_queries(d["query_ids"])
        except Exception as e:
            logger.warning("질의 재오픈 실패(%s): %s", cid, e)
    return {"ok": True, "status": status, "reopened": reopened}


# ── 후보 보강(재보강) — 승인 전 핵심 운영 정보 채우기 (이슈 #143) ──
# 비어 있는 운영 필드만 외부검색(grounding)으로 채우고, 기존 값은 보존. status 변경/등록 없음.
ENRICH_FIELDS = ["operating_agencies", "supported_amount", "how_to_use", "application",
                 "validity", "exceptions_and_caveats", "contact", "faq",
                 "eligibility", "legal_basis", "sources", "last_verified"]


def _is_empty(v):
    return v is None or v == "" or v == [] or v == {}


def _schema_keys():
    try:
        sc = json.loads((_APP / "policy_db" / "schema.json").read_text(encoding="utf-8"))
        return set((sc.get("properties") or {}).keys())
    except Exception:
        return set()


def _deep_fill(base, add):
    """add 의 값으로 base 의 빈 필드만 채움(재귀). 채워진 최상위 키 목록 반환."""
    filled = []
    if not isinstance(add, dict):
        return filled
    for k, v in add.items():
        if _is_empty(v):
            continue
        if k not in base or _is_empty(base.get(k)):
            base[k] = v
            filled.append(k)
        elif isinstance(base.get(k), dict) and isinstance(v, dict):
            if _deep_fill(base[k], v):
                filled.append(k)
    return filled


def enrich_candidate(cid, draft_override=None):
    """신규 후보 초안의 비어 있는 핵심 운영 정보를 외부검색으로 보강.
    - status 변경/items 등록 없음(검토 전용). 후보 파일에 보강 결과 저장.
    - draft_override(관리자 편집본)가 오면 그 위에 보강."""
    if not GEMINI_API_KEY:
        return {"ok": False, "error": "GEMINI_API_KEY 없음"}
    f = _CAND_DIR / f"{cid}.json"
    if not f.exists():
        return {"ok": False, "error": "후보 없음"}
    cand = json.loads(f.read_text(encoding="utf-8"))
    if cand.get("status") == "approved":
        return {"ok": False, "error": "이미 승인된 후보(보강 불가)"}
    draft = draft_override if (isinstance(draft_override, dict) and draft_override) else (cand.get("draft_item") or {})
    queries = cand.get("cluster_queries") or []
    topic = cand.get("topic") or draft.get("title") or ""
    enums = _schema_enums()

    prompt = (
        "대한민국 장애인 지원 정책 항목의 '핵심 운영 정보'를 보강하세요. "
        "아래 기존 초안에서 비어 있는 운영 필드를, 공식·신뢰 출처(law.go.kr·보건복지부·복지로·지자체·해당 기관/은행 공식 안내)를 "
        "웹에서 찾아 사실에 근거해 채웁니다. 추측·창작 금지, 확인 안 되면 비웁니다.\n"
        f"주제: {topic}\n"
        f"사용자가 실제로 궁금해한 질문(이 질문들에 답할 수 있도록 보강): {queries}\n\n"
        "[기존 초안 JSON]\n" + json.dumps(draft, ensure_ascii=False) + "\n\n"
        "다음 키를 가능한 한 구체적으로 채운 JSON 하나만 출력(설명·코드블록 없이):\n"
        "- operating_agencies: 실제 운영/취급 기관. 각 항목은 객체 {agency:기관·업체명, region, apply_channel, url, notes}(상품이면 agency 에 취급 은행명)\n"
        "- supported_amount{rate,amount,scope}: 실제 수치(금리·금액). 변동되는 값은 기준 시점을 scope 에 명시\n"
        "- how_to_use{default, ...}: 실제 이용·가입 절차\n"
        "- application{where[{channel,method,url}], required_documents[], processing_period, fee, online_available, proxy_allowed}: 신청·가입 방법\n"
        "- validity: 적용/유효 기간·갱신 주기\n"
        "- exceptions_and_caveats: 예외·유의사항(은행·지역·시점별로 달라지면 그 점을 명시)\n"
        "- contact: 문의처(기관명·전화·URL)\n"
        "- faq: 기존 초안의 faq 와 겹치지 않는 새로운 질문·답변을 3~5개 생성(재보강을 반복하면 누적되어 10개 이상까지 쌓이도록). 사용자가 실제 물어본 질문 + 관련해 자주 묻는 질문. 각 답변은 위에서 보강한 운영정보에 근거해 구체적으로, 확인 안 된 내용은 지어내지 말 것\n"
        "- sources: 근거 출처(각 항목 title·publisher·url + priority 는 primary/secondary/supplementary 중 하나, 실제 URL)\n"
        f"category enum: {enums['category']} / benefit_type enum: {enums['benefit_type']} (해당 키를 새로 채울 때만 enum 준수). "
        "확인 안 되는 필드는 넣지 말 것(빈 값으로 출력 금지)."
    )
    raw = _gemini(prompt, grounding=True, max_tokens=8000, model=_model_for("enrich"))
    if not raw.strip():
        reason = _LAST_GEMINI_ERR or "외부검색 실패 또는 빈 응답"
        return {"ok": False, "error": f"보강 응답 없음 — {reason}"}
    try:
        add = _parse_json(raw)
    except Exception as e:
        return {"ok": False, "error": f"보강 응답 파싱 실패: {e}"}
    if not isinstance(add, dict):
        return {"ok": False, "error": "보강 응답 형식 오류"}

    allowed = _schema_keys() or set(ENRICH_FIELDS)
    add = {k: v for k, v in add.items() if k in allowed}
    # faq 는 '빈칸 채우기'가 아니라 '누적' — 기존과 겹치지 않는 새 Q만 덧붙임(재보강 반복 시 10+ 축적)
    faq_added = 0
    if isinstance(add.get("faq"), list):
        def _qn(x):
            q = (x.get("q") if isinstance(x, dict) else "") or ""
            return "".join(ch for ch in q.lower() if ch.isalnum())
        existing = draft.get("faq") if isinstance(draft.get("faq"), list) else []
        seen = {_qn(it) for it in existing if isinstance(it, dict)}
        merged = list(existing)
        for it in add["faq"]:
            qa = _as_qa(it)
            if qa:
                k = _qn(qa)
                if k and k not in seen:
                    seen.add(k)
                    merged.append(qa)
                    faq_added += 1
        draft["faq"] = merged
        add.pop("faq", None)  # _deep_fill 이 덮어쓰지 않도록 제거
    filled = _deep_fill(draft, add)
    if faq_added:
        filled = list(dict.fromkeys(filled + ["faq"]))
    draft["last_verified"] = date.today().isoformat()
    cand["draft_item"] = draft
    cand["enriched_at"] = datetime.now().isoformat(timespec="seconds")
    cand["enrich_count"] = int(cand.get("enrich_count") or 0) + 1
    f.write_text(json.dumps(cand, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "draft_item": draft, "added_fields": filled, "faq_added": faq_added, "faq_total": len(draft.get("faq") or []), "grounded": True}


# ── 보강(gap) 경로: 기존 정책 누락 세부를 기존 검토 큐(staging)로 적재 (이슈 후속) ──
def _reopen_queries(ids):
    """반려된 제안의 원 미답변 질의를 재분류 대기로 되돌림(discovery_processed_at=NULL)."""
    ids = [i for i in (ids or []) if i is not None]
    if not ids:
        return 0
    con = _db(); cur = con.cursor()
    cur.execute("UPDATE unresolved_queries SET discovery_processed_at = NULL WHERE id = ANY(%s)", (ids,))
    n = cur.rowcount
    con.commit(); con.close()
    return n


def reopen_for_staging(staged_name):
    """staging 보강 제안 반려 시 — 동반 .disc.json 의 query_ids 를 재오픈(best-effort)."""
    try:
        side = _STAGING_DIR / staged_name.replace(".staged.json", ".disc.json")
        if not side.exists():
            return 0
        info = json.loads(side.read_text(encoding="utf-8"))
        return _reopen_queries(info.get("query_ids"))
    except Exception as e:
        logger.warning("staging 재오픈 실패(%s): %s", staged_name, e)
        return 0


def _load_existing_policy(pid):
    files = list(_ITEMS.glob(f"{pid}_*.json"))
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def _norm_txt(x):
    if isinstance(x, dict):
        x = x.get("q") or x.get("url") or json.dumps(x, ensure_ascii=False)
    return "".join(ch for ch in str(x or "").lower() if ch.isalnum())


def _as_qa(it):
    """faq 항목을 {q,a}로 정규화 — q/question/Q, a/answer/A 키 변형 허용."""
    if not isinstance(it, dict):
        return None
    q = it.get("q") or it.get("question") or it.get("Q")
    a = it.get("a") or it.get("answer") or it.get("A")
    return {"q": q, "a": a} if (q and a) else None


def _merge_additive(base, add):
    """기존 정책(base)에 보강(add)을 추가 병합(기존 보존). 변경된 최상위 키 목록 반환.
    faq/operating_agencies/sources 는 비중복 append, 그 외는 빈 칸만 채움."""
    changed = []
    if not isinstance(add, dict):
        return changed
    add = dict(add)
    if isinstance(add.get("faq"), list):
        ex = base.get("faq") if isinstance(base.get("faq"), list) else []
        seen = {_norm_txt(it) for it in ex if isinstance(it, dict)}
        merged = list(ex); n = 0
        for it in add["faq"]:
            qa = _as_qa(it)
            if qa and _norm_txt(qa) not in seen:
                seen.add(_norm_txt(qa)); merged.append(qa); n += 1
        if n:
            base["faq"] = merged; changed.append("faq")
        add.pop("faq", None)
    for k, keyfn in (("operating_agencies", lambda it: _norm_txt(it.get("agency") if isinstance(it, dict) else it)),
                     ("sources", lambda it: (it.get("url") if isinstance(it, dict) else _norm_txt(it)))):
        if isinstance(add.get(k), list):
            ex = base.get(k) if isinstance(base.get(k), list) else []
            seen = {keyfn(it) for it in ex}
            merged = list(ex); n = 0
            for it in add[k]:
                kk = keyfn(it)
                if kk and kk not in seen:
                    seen.add(kk); merged.append(it); n += 1
            if n:
                base[k] = merged; changed.append(k)
            add.pop(k, None)
    changed += _deep_fill(base, add)
    return list(dict.fromkeys(changed))


def _compact_policy(p):
    """gap 보강 프롬프트용 — 정책에서 판단·중복확인에 필요한 핵심 필드만 추려 입력을 경량화.
    (전체 JSON 임베드 시 프롬프트가 커져 grounding 응답이 느려지는 문제 회피)"""
    if not isinstance(p, dict):
        return {}
    keys = ["id", "title", "category", "benefit_type", "supported_amount", "eligibility",
            "operating_agencies", "how_to_use", "application", "validity",
            "exceptions_and_caveats", "contact"]
    out = {k: p.get(k) for k in keys if p.get(k) not in (None, "", [], {})}
    # faq 는 질문만(중복 회피 컨텍스트) — 답변 본문은 제외해 크기 절감
    faqs = p.get("faq")
    if isinstance(faqs, list) and faqs:
        out["faq_questions"] = [(_as_qa(it) or {}).get("q") for it in faqs if _as_qa(it)]
    return out


def _make_gap_staged(pid, member_qs, member_ids, gap_detail):
    """기존 정책 pid 의 누락 세부를 외부검색으로 보강해, 변경분이 있으면 staging 적재.
    직접 적용/등록 없음 — 기존 검토 큐에서 사람이 승인해야 반영."""
    if not GEMINI_API_KEY:
        return {"ok": False, "error": "GEMINI_API_KEY 없음"}
    existing = _load_existing_policy(pid)
    if not existing:
        return {"ok": False, "error": f"기존 정책 {pid} 없음"}
    prompt = (
        f"대한민국 장애인 지원 정책 '{existing.get('title')}'({pid})에 빠진 세부 정보를 보강합니다.\n"
        f"빠진 것으로 의심되는 세부: {gap_detail}\n"
        f"사용자가 답을 못 받은 질문: {member_qs}\n\n"
        "[기존 정책 요약]\n" + json.dumps(_compact_policy(existing), ensure_ascii=False) + "\n\n"
        "공식·신뢰 출처를 웹에서 찾아, 위 정책에 '추가되어야 할' 부분만 JSON 하나로 출력(설명·코드블록 없이). "
        "기존에 이미 있는 내용은 반복하지 말 것. 추측·창작 금지(확인 안 되면 비움).\n"
        "채울 수 있는 키: operating_agencies(추가 기관·업체. 각 항목은 객체 {agency:기관·업체명(필수), region, apply_channel, url, notes}), supported_amount{rate,amount,scope}, "
        "how_to_use{...}, application{where[{channel,method,url}],required_documents[],processing_period,fee}, "
        "validity, exceptions_and_caveats, contact, faq([{q,a}]), "
        "sources(각 항목 title·publisher·url + priority 는 primary/secondary/supplementary 중 하나, 실제 URL). "
        "확인 안 되는 키는 넣지 말 것."
    )
    raw = _gemini(prompt, grounding=True, max_tokens=8000, model=_model_for("gap"))
    if not raw.strip():
        return {"ok": False, "error": f"보강 응답 없음 — {_LAST_GEMINI_ERR or '외부검색 실패'}"}
    try:
        add = _parse_json(raw)
    except Exception as e:
        return {"ok": False, "error": f"보강 응답 파싱 실패: {e}"}
    if not isinstance(add, dict):
        return {"ok": False, "error": "보강 응답 형식 오류"}
    allowed = _schema_keys() or set(ENRICH_FIELDS)
    add = {k: v for k, v in add.items() if k in allowed}
    import copy as _copy
    updated = _copy.deepcopy(existing)
    changed = _merge_additive(updated, add)
    if not changed:
        return {"ok": False, "skipped": True, "error": "실제 추가될 내용 없음(오탐 가능)"}
    updated["last_verified"] = date.today().isoformat()
    try:
        import jsonschema
        schema = json.loads((_APP / "policy_db" / "schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(updated, schema)
    except ImportError:
        pass
    except Exception as e:
        logger.warning("보강 스키마 검증 실패(%s): %s", pid, e)
        return {"ok": False, "error": f"스키마 검증 실패: {e}"}
    _STAGING_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    staged = _STAGING_DIR / f"{pid}_disc{ts}.staged.json"
    staged.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    (_STAGING_DIR / f"{pid}_disc{ts}.disc.json").write_text(json.dumps({
        "source": "discovery_gap", "policy_id": pid, "gap_detail": gap_detail,
        "query_ids": member_ids, "cluster_queries": member_qs, "changed_fields": changed,
        "created_at": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("보강 staged 적재: %s (변경 %s)", staged.name, changed)
    return {"ok": True, "policy_id": pid, "changed": changed, "staged": staged.name}


# ── 재보강 비동기 실행(상태를 후보 파일에 기록) — nginx 동기 타임아웃 우회 ──
def _set_enrich_status(cid, status):
    f = _CAND_DIR / f"{cid}.json"
    if not f.exists():
        return
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        d["enrich_status"] = status
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("enrich_status 기록 실패(%s): %s", cid, e)


def enrich_candidate_run(cid, draft_override=None):
    """백그라운드 러너 — enrich_candidate 를 돌리고 진행/결과 상태를 후보 파일에 남김.
    프론트는 후보 GET 으로 enrich_status 를 폴링(긴 동기요청/프록시 타임아웃 회피)."""
    now = datetime.now().isoformat(timespec="seconds")
    _set_enrich_status(cid, {"state": "running", "started_at": now})
    try:
        r = enrich_candidate(cid, draft_override)
        if r.get("ok"):
            _set_enrich_status(cid, {"state": "done", "at": datetime.now().isoformat(timespec="seconds"),
                                     "added_fields": r.get("added_fields"), "faq_total": r.get("faq_total")})
        else:
            _set_enrich_status(cid, {"state": "error", "at": datetime.now().isoformat(timespec="seconds"),
                                     "error": r.get("error")})
    except Exception as e:
        logger.exception("enrich 러너 실패(%s)", cid)
        _set_enrich_status(cid, {"state": "error", "error": str(e)})
