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
_REPORT_DIR = _DATA / "discovery" / "reports"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_LLM_MODEL", "gemini-3.1-pro-preview")
_GEN_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
SIM_THRESHOLD = 0.82


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


def _gemini(prompt, grounding=False, max_tokens=24000, retries=3):
    """Gemini generateContent. thinking 모델의 간헐적 빈 응답에 대비해 재시도."""
    import time as _t
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}}
    if grounding:
        payload["tools"] = [{"google_search": {}}]
    else:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post(_GEN_URL, headers={"x-goog-api-key": GEMINI_API_KEY,
                              "Content-Type": "application/json"}, json=payload, timeout=(10, 120))
            r.raise_for_status()
            cands = r.json().get("candidates") or []
            if cands:
                parts = (cands[0].get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought"))
                if txt.strip():
                    return txt
            last_err = "빈 응답"
        except Exception as e:
            last_err = str(e)
        if attempt < retries - 1:
            _t.sleep(2 * (attempt + 1))
    logger.warning("Gemini 응답 실패(%d회): %s", retries, last_err)
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
        '[{"idx":0,"policy_related":true,"covered_by":"B0xx 또는 null","novel":true,"topic":"주제 한 줄"}]\n'
        "policy_related=장애인 지원정책/제도 관련(아이돌·영화·잡담은 false). "
        "novel=정책관련이며 기존 정책에 없는 새 정책이면 true."
    )
    try:
        clf = _parse_json(_gemini(clf_prompt))
    except Exception as e:
        return {"clusters": len(clusters), "candidates": 0, "error": f"분류 실패: {e}"}

    enums = _schema_enums()
    novel = [c for c in clf if c.get("policy_related") and c.get("novel")]
    _CAND_DIR.mkdir(parents=True, exist_ok=True)
    created = []
    for c in novel:
        try:
            cl = clusters[c["idx"]]
        except (KeyError, IndexError):
            continue
        topic = c.get("topic", "")
        member_qs = [m["q"] for m in cl["members"]]
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
            draft = _parse_json(_gemini(draft_prompt, grounding=True))
        except Exception as e:
            logger.warning("초안 실패(topic=%s): %s", topic, e)
        cid = "C" + datetime.now().strftime("%Y%m%d%H%M%S%f")[:18]
        cand = {"candidate_id": cid, "topic": topic, "cluster_queries": member_qs,
                "classification": c, "draft_item": draft, "status": "pending",
                "created_at": datetime.now().isoformat(timespec="seconds")}
        (_CAND_DIR / f"{cid}.json").write_text(json.dumps(cand, ensure_ascii=False, indent=2), encoding="utf-8")
        created.append(cid)

    # 처리한 질의는 '발굴됨'으로 표시 → 다음 발굴에서 제외(중복 후보 방지)
    processed_ids = [m["id"] for cl in clusters for m in cl["members"]]
    try:
        _mark_processed(processed_ids)
    except Exception as e:
        logger.warning("processed 표시 실패: %s", e)

    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {"date": date.today().isoformat(), "clusters": len(clusters),
               "classified": clf, "novel": len(novel), "candidates": created}
    (_REPORT_DIR / f"{date.today().isoformat()}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"clusters": len(clusters), "policy_novel": len(novel), "candidates": len(created), "processed": len(processed_ids)}


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
                        "has_draft": bool(d.get("draft_item")), "created_at": d.get("created_at")})
        except Exception:
            pass
    return out


def get_candidate(cid):
    f = _CAND_DIR / f"{cid}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"error": "후보 없음"}


def set_status(cid, status):
    f = _CAND_DIR / f"{cid}.json"
    if not f.exists():
        return {"ok": False, "error": "후보 없음"}
    d = json.loads(f.read_text(encoding="utf-8"))
    d["status"] = status
    d["status_at"] = datetime.now().isoformat(timespec="seconds")
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "status": status}
