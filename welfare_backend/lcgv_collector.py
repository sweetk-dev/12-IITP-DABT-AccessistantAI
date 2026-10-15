# lcgv_collector.py — 지자체 복지서비스(한국사회보장정보원 「지자체복지서비스」 공공 API) → 신규 정책 후보 (v2.7.0)
#
# 시 자체 사업(안양시·경기도 단위)은 전국 정책 DB 에 없고, 매년 바뀐다. 공공 API 가 지자체별 복지서비스
# 목록·상세를 주므로 정기적으로 읽어 **장애인 대상이면서 정책 DB 에 없는 사업**을 신규 후보로 만든다.
# 후보는 '발굴' 후보와 같은 검토 큐(discovery/candidates)에 들어가 운영 콘솔에서 사람이 승인해야만
# 정책 항목이 된다(자동 등록하지 않는다).
#
#   목록   LcgvWelfarelist     ctpvNm(시도)·sggNm(시군구) 단위, 500건씩
#   상세   LcgvWelfaredetailed servId — 대상·선정기준·지원내용·신청방법·근거법령·문의처
#
# 대상: LCGV_CTPV(기본 경기도)의 도 단위 사업(시군구명 빈칸) + LCGV_SGG(기본 안양시) 사업 중
#       장애인 관련(서비스명·요약·대상 구분에 '장애')
# 상태: $POLICY_DATA_DIR/discovery/lcgv_state.json {servId: {last_mod, candidate_id, status, title}}
#   - 처음 본 사업 → 후보 생성(status=candidate)
#   - 정책 DB 에 같은 이름이 이미 있으면 후보를 만들지 않고 status=exists
#   - 이미 후보를 만든 사업의 수정일(lastModYmd)이 바뀌면 changed 로 표시(요약에 건수) — 후보를 새로
#     만들지는 않는다(승인된 정책의 변경 감지는 정책 크롤러가 출처 페이지로 한다)
# 인증키: LCGV_API_KEY, 없으면 DATA_GO_KR_API_KEY (공공데이터포털 일반 인증키, 활용신청 필요)
import json
import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import requests

logger = logging.getLogger("lcgv")

_APP = Path(__file__).resolve().parent
_DATA = Path(os.environ.get("POLICY_DATA_DIR") or str(_APP / "policy_db"))
_ITEMS = _DATA / "items"
_CAND_DIR = _DATA / "discovery" / "candidates"
_STATE = _DATA / "discovery" / "lcgv_state.json"

BASE = "https://apis.data.go.kr/B554287/LocalGovernmentWelfareInformations"
TIMEOUT = float(os.environ.get("LCGV_TIMEOUT", "30"))
PAGE_SIZE = 500
MAX_PAGES = 10

# 카테고리 추정(스키마 enum). 앞에 있는 규칙이 먼저 걸린다. 운영자가 승인 전에 고칠 수 있다.
_CATEGORY_RULES = (
    ("활동지원", ("활동지원", "돌봄", "활동보조")),
    ("교통", ("차량", "콜", "교통", "이동", "택시", "버스", "주차")),
    ("의료", ("치료", "의료", "수술", "진료", "재활", "검사", "보청기", "달팽이")),
    ("보육·교육", ("학생", "통학", "교육", "학교", "보육", "아동")),
    ("주거", ("주거", "주택", "임대", "냉난방", "집수리")),
    ("소득지원", ("수당", "지원금", "장려금", "통장", "소득", "출산", "연금", "생계")),
    ("통신", ("통신", "전화", "인터넷")),
    ("문화·체육", ("문화", "체육", "여가", "관광")),
)


def _api_key():
    k = (os.environ.get("LCGV_API_KEY") or os.environ.get("DATA_GO_KR_API_KEY") or "").strip().strip('"').strip("'")
    return k


def _enc(k):
    return k if "%" in k else urllib.parse.quote(k, safe="")


def _get(op, params):
    """XML 응답의 루트. 결과 코드가 정상이 아니면 RuntimeError."""
    url = "%s/%s?serviceKey=%s&%s" % (BASE, op, _enc(_api_key()), urllib.parse.urlencode(params))
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)      # XML 선언의 인코딩을 따른다(헤더에 charset 이 없어도 한글이 깨지지 않게)
    code = (root.findtext(".//resultCode") or "").strip()
    if code not in ("0", "00", ""):
        raise RuntimeError("지자체복지서비스 %s 오류 %s %s" % (op, code, root.findtext(".//resultMessage")))
    return root


def _txt(el, tag):
    v = el.findtext(tag)
    return " ".join(v.split()) if v else ""


def fetch_list(ctpv, sgg=""):
    """시도(+시군구) 목록 전체 → [{servId, servNm, sggNm, servDgst, trgterIndvdlNmArray, lastModYmd, servDtlLink, ...}]."""
    out = []
    for page in range(1, MAX_PAGES + 1):
        params = {"pageNo": page, "numOfRows": PAGE_SIZE, "ctpvNm": ctpv}
        if sgg:
            params["sggNm"] = sgg
        root = _get("LcgvWelfarelist", params)
        rows = [{c.tag: " ".join((c.text or "").split()) for c in s} for s in root.iter("servList")]
        out.extend(rows)
        total = int(root.findtext(".//totalCount") or 0)
        if len(rows) < PAGE_SIZE or len(out) >= total:
            break
    return out


def fetch_detail(serv_id):
    """상세 → {필드: 값, 'laws': [...], 'contacts': [(이름, 연락처)], 'homepages': [...]}"""
    root = _get("LcgvWelfaredetailed", {"servId": serv_id})
    d = {}
    for tag in ("servNm", "sggNm", "ctpvNm", "bizChrDeptNm", "servDgst", "sprtCycNm", "srvPvsnNm", "sprtTrgtCn",
                "slctCritCn", "alwServCn", "aplyMtdCn", "lastModYmd", "enfcBgngYmd", "enfcEndYmd", "servDtlLink"):
        d[tag] = _txt(root, ".//" + tag)
    laws, contacts, homes = [], [], []
    for parent in root.iter():
        name = parent.tag
        if not name.endswith("List") or name in ("servList",):
            continue
        nm = _txt(parent, "wlfareInfoReldNm")
        cn = _txt(parent, "wlfareInfoReldCn")
        if not nm:
            continue
        if name.startswith("baslaw"):
            laws.append(nm)
        elif name.startswith("inqpl") and "Hmpg" not in name:
            contacts.append((nm, cn))
        elif "Hmpg" in name or name.startswith("hmpg"):
            homes.append(cn or nm)
    d["laws"], d["contacts"], d["homepages"] = laws, contacts, homes
    return d


def is_disability(row):
    text = " ".join([row.get("servNm", ""), row.get("servDgst", ""), row.get("trgterIndvdlNmArray", "")])
    return "장애" in text


def _norm(s):
    return re.sub(r"[\s·\-()（）\[\]]+", "", s or "")


def existing_titles():
    out = set()
    for f in _ITEMS.glob("*.json"):
        try:
            out.add(_norm(json.loads(f.read_text(encoding="utf-8")).get("title")))
        except Exception:  # noqa: BLE001 — 깨진 파일 하나 때문에 수집을 멈추지 않는다
            continue
    out.discard("")
    return out


def guess_category(text):
    for cat, words in _CATEGORY_RULES:
        if any(w in text for w in words):
            return cat
    return "기타"


def guess_benefit(provision):
    p = provision or ""
    if "현금" in p:
        return "현금지급"
    if "현물" in p:
        return "현물지급"
    if "감면" in p:
        return "감면"
    return "서비스제공"


def build_draft(row, det, today=None):
    """목록 행 + 상세 → 정책 항목 초안(스키마 형태). 비어 있는 칸은 비워 둔다(추측해 채우지 않는다)."""
    today = today or date.today().isoformat()
    title = det.get("servNm") or row.get("servNm")
    region = det.get("sggNm") or row.get("sggNm") or det.get("ctpvNm") or row.get("ctpvNm") or ""
    owner = det.get("ctpvNm") or row.get("ctpvNm") or ""
    link = det.get("servDtlLink") or row.get("servDtlLink") or ""
    target = det.get("sprtTrgtCn") or ""
    if det.get("slctCritCn") and det["slctCritCn"] != target:
        target = (target + " / 선정기준: " + det["slctCritCn"]).strip(" /")
    contacts = "; ".join("%s %s" % (n, c) for n, c in det.get("contacts") or [] if n).strip()
    draft = {
        "id": "",
        "leaflet_section": "기타",
        "leaflet_number": 0,
        "title": title,
        "short_summary": det.get("servDgst") or row.get("servDgst") or title,
        "category": guess_category(" ".join([title or "", det.get("servDgst", ""), det.get("alwServCn", "")])),
        "benefit_type": guess_benefit(det.get("srvPvsnNm") or row.get("srvPvsnNm")),
        # 금액(숫자+원)이 적힌 경우만 amount 로 옮기고, 지원 내용 전체는 scope 에 둔다
        "supported_amount": {"rate": None,
                             "amount": det.get("alwServCn") if re.search(r"\d[\d,.]*\s*(만\s*)?원", det.get("alwServCn") or "") else None,
                             "scope": det.get("alwServCn") or ""},
        "eligibility": {"target": target or "%s 거주 장애인(상세는 출처 확인)" % (region or owner),
                        "residency_criteria": ("%s 거주" % region) if region else None},
        "legal_basis": [{"name": n} for n in (det.get("laws") or []) if not n.lower().endswith(".pdf")][:5],
        "how_to_use": {"default": det.get("aplyMtdCn") or "담당 부서·주민센터 문의(상세는 출처 확인)"},
        "application": {},
        "operating_agencies": [{
            "region": region or owner, "agency": det.get("bizChrDeptNm") or row.get("bizChrDeptNm") or "",
            "lines_or_routes": [], "apply_channel": det.get("aplyMtdCn") or None, "card_or_proof_type": None,
            "url": link or None, "notes": ("문의: " + contacts) if contacts else None,
        }],
        "sources": [{
            "title": "복지로 지자체 복지서비스 — %s" % title,
            "publisher": "한국사회보장정보원(복지로)",
            "publisher_type": "공기업",
            "url": link,
            "priority": "primary",
            "official_api": {"available": True, "endpoint": BASE + "/LcgvWelfaredetailed", "auth_required": True,
                             "docs_url": None},
            "crawl": {"frequency": "monthly", "change_detection_method": "page_hash", "css_selector_hint": None,
                      "notes": "지자체 복지서비스 상세(servId=%s, 원천 수정일 %s)" % (
                          row.get("servId"), det.get("lastModYmd") or row.get("lastModYmd"))},
            "accessed": today,
        }],
        "last_verified": today,
        "version": "1.0.0",
    }
    if det.get("sprtCycNm"):
        draft["how_to_use"]["exceptions"] = "지원 주기: %s" % det["sprtCycNm"]
    return draft


def _load_state():
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state):
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STATE)


def run(ctpv=None, sggs=None, max_new=30):
    """수집 1회. 반환: 요약 dict. 키가 없으면 error."""
    if not _api_key():
        return {"error": "지자체복지서비스 인증키 없음(LCGV_API_KEY 또는 DATA_GO_KR_API_KEY)"}
    ctpv = ctpv or os.environ.get("LCGV_CTPV", "경기도")
    sggs = sggs if sggs is not None else [s.strip() for s in os.environ.get("LCGV_SGG", "안양시").split(",") if s.strip()]
    try:
        rows = [r for r in fetch_list(ctpv) if not r.get("sggNm")]          # 도 단위 사업
        for sgg in sggs:
            rows += fetch_list(ctpv, sgg)
    except Exception as e:  # noqa: BLE001
        logger.warning("지자체복지서비스 목록 실패: %s", e)
        return {"error": "목록 조회 실패: %s" % e}
    targets = [r for r in rows if r.get("servId") and is_disability(r)]
    state = _load_state()
    titles = existing_titles()
    _CAND_DIR.mkdir(parents=True, exist_ok=True)
    summary = {"fetched": len(rows), "disability": len(targets), "new_candidates": [], "exists": 0,
               "changed": [], "errors": 0}
    for row in targets:
        sid = row["servId"]
        st = state.get(sid)
        if st:
            if row.get("lastModYmd") and st.get("last_mod") and row["lastModYmd"] != st["last_mod"] \
                    and st.get("status") == "candidate":
                st["changed_mod"] = row["lastModYmd"]
                summary["changed"].append(sid)
            continue
        if _norm(row.get("servNm")) in titles:
            state[sid] = {"last_mod": row.get("lastModYmd"), "status": "exists", "title": row.get("servNm")}
            summary["exists"] += 1
            continue
        if len(summary["new_candidates"]) >= max_new:
            continue        # 다음 회차에 이어서(한 번에 검토 큐를 넘치게 하지 않는다)
        try:
            det = fetch_detail(sid)
        except Exception as e:  # noqa: BLE001
            logger.warning("지자체복지서비스 상세 실패(%s): %s", sid, e)
            summary["errors"] += 1
            continue
        draft = build_draft(row, det)
        cid = "C" + datetime.now().strftime("%Y%m%d%H%M%S%f")[:18]
        cand = {"candidate_id": cid, "topic": "지자체 복지서비스: %s (%s)" % (row.get("servNm"), row.get("sggNm") or ctpv),
                "cluster_queries": [], "query_ids": [],
                "classification": {"source": "lcgv", "serv_id": sid, "sgg": row.get("sggNm") or "",
                                   "ctpv": ctpv, "last_mod": row.get("lastModYmd")},
                "draft_item": draft, "status": "pending",
                "created_at": datetime.now().isoformat(timespec="seconds")}
        try:
            (_CAND_DIR / ("%s.json" % cid)).write_text(json.dumps(cand, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("후보 저장 실패(%s): %s", sid, e)
            summary["errors"] += 1
            continue
        state[sid] = {"last_mod": row.get("lastModYmd"), "status": "candidate", "candidate_id": cid,
                      "title": row.get("servNm")}
        summary["new_candidates"].append(cid)
    _save_state(state)
    summary["new"] = len(summary["new_candidates"])
    logger.info("지자체복지서비스 수집: %s", {k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()})
    return summary
