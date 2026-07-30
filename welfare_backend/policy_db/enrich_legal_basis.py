#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_legal_basis.py — 정책 데이터 legal_basis 표준 매핑(법령ID) 파이프라인

목적: 12-IITP-DABT-AccessistantAI/welfare_backend/policy_db/items/B*.json 의
      legal_basis[] 각 항목을, 국가법령정보센터(law.go.kr) 표준 식별자에 매핑한다.
      오픈데이터 API 공개 시 "근거 법령 = 검증 가능한 참조"로 만들기 위한 1단계.

파이프라인:
  1) normalize   : 표기 정제(중점문자 통일, 주석 괄호 제거, 이름에 섞인 조문 분리)
  2) classify    : target 분류 -> law | admrul | ordin | none | review
  3) resolve     : (OC 키 있을 때) law.go.kr Open API 로 법령ID/일련번호/시행일 조회
  4) apply/report: 결과를 리뷰 표로 출력, --apply 시 item JSON 에 필드 추가(백업 후)

키 없이도 1~2단계(정제·분류)는 완전 동작. 3단계는 --oc 제공 시에만.

실행 예:
  python enrich_legal_basis.py --items ./items --report-only
  python enrich_legal_basis.py --items ./items --oc myemailid          # 실조회(드라이런)
  python enrich_legal_basis.py --items ./items --oc myemailid --apply  # 파일 반영

주의: --oc 의 값은 open.law.go.kr 에서 이메일로 신청해 받은 OC(이메일 ID)다.
      law.go.kr 접근이 가능한 환경에서 실행한다.

이력: 2026-07-30 — target=law 검색 XML 의 아이템 태그가 <법령> 이 아니라 <law> 임을
      실측으로 확인해 수정(admrul/ordin 은 기존 태그 유지). 미수정 시 법령 전건 not_found.
"""
import argparse, json, glob, os, re, sys, time, datetime
from collections import Counter, OrderedDict

# ── 1. 표기 정제 ────────────────────────────────────────────────
_MIDDOT = str.maketrans({"ㆍ": "·", "／": "/"})
_ARTICLE_RE = re.compile(r"(제\d+조(?:의\d+)?)")
# 주석성 괄호: 구 명칭/영문/연도별/출처 표기 등 → 이름에서 제거
_ANNOTATION_HINT = ("구 ", "영문", "KLRI", "연도별", "보건복지부 고시", "버스비 지원 근거")

def normalize_name(raw: str):
    """반환: (정제된 법령명, 이름에서 분리된 조문 or None, 정제 플래그 리스트)"""
    flags = []
    name = raw.translate(_MIDDOT).strip()
    name = re.sub(r"\s+", " ", name)
    split_article = None
    # 이름에 조문이 섞인 경우 분리 (예: "방송법 시행령 제43조(...)")
    m = _ARTICLE_RE.search(name)
    if m:
        split_article = m.group(1)
        name = name[:m.start()].strip()
        flags.append("article_split")
    # 주석성 괄호 제거
    def _strip_paren(s):
        out = re.sub(r"\s*[\(（][^)）]*[\)）]\s*$", "", s).strip()
        return out
    while True:
        m2 = re.search(r"[\(（]([^)）]*)[\)）]\s*$", name)
        if not m2:
            break
        inner = m2.group(1)
        if any(h in inner for h in _ANNOTATION_HINT):
            name = _strip_paren(name)
            flags.append("annotation_stripped")
        else:
            break
    return name, split_article, flags

# ── 2. target 분류 ──────────────────────────────────────────────
# none = law.go.kr 에 존재하지 않는 비법령(사업안내/약관/내규 등) → 텍스트(S) 유지
def classify_target(name: str, raw: str):
    """target 분류. 고시/조례 등 유형신호는 원표기(raw)에서, 법령판정은 정제명(name)에서."""
    r = raw
    if "조례" in r:
        return "ordin"                     # 자치법규
    if "약관" in r or "내규" in r:
        return "none"                      # 비법령
    if "사업안내" in r or "사업지침" in r or "KLRI" in r or "영문" in r:
        return "none"                      # 비법령(사업지침/영문 등)
    if "고시" in r or "훈령" in r or "예규" in r:
        return "admrul"                    # 행정규칙
    n = name
    if n.endswith("법") or n.endswith("법률") or "시행령" in n or n.endswith("규칙"):
        return "law"                       # 법령(법률·시행령·부령/시행규칙)
    if "지침" in r or "규정" in r or "기준" in r:
        return "review"                    # 대통령령 규정/고시 혼재 → 사람 확인
    return "review"


# ── review 버킷 수기 확정 (리서치 근거) ──────────────────────────
# 근거 "문서 유형" 기준. 혜택의 지역별 운영차이(요금 등)는 operating_agencies 층위에서 별도 처리.
MANUAL_OVERRIDE = {
    # 보건복지부 고시(국민건강보험법 제51조·시행규칙 제26조 근거) → 행정규칙
    "장애인보조기기 보험급여 기준 등 세부사항": "admrul",
    # 국가유산청 궁능유적본부 훈령(국가시설, 지역차 없음) → 행정규칙
    "궁·능 관람 등에 관한 규정": "admrul",
    # 산업통상자원부 고시 2025-24호(도시가스사업법 시행규칙 제34조의2 근거) → 행정규칙
    "도시가스요금 경감지원금액 한도 산정 등에 관한 지침": "admrul",
}

TARGET_LABEL = {
    "law": "법령", "admrul": "행정규칙", "ordin": "자치법규",
    "none": "비법령(텍스트유지)", "review": "사람확인필요",
}

# ── 3. law.go.kr Open API 조회 (OC 키 필요) ─────────────────────
LAW_SEARCH_URL = "http://www.law.go.kr/DRF/lawSearch.do"
# 응답 XML 의 태그명(공동활용 표준): target 별 상이
_FIELDS = {
    "law":    ("law", "법령ID", "법령일련번호", "법령명한글", "시행일자", "현행연혁코드"),
    "admrul": ("admrul", "행정규칙ID", "행정규칙일련번호", "행정규칙명", "시행일자", "현행연혁코드"),
    "ordin":  ("law", "자치법규ID", "자치법규일련번호", "자치법규명", "시행일자", "현행연혁코드"),
}

def resolve_law_id(name, target, oc, sleep=0.34):
    """law.go.kr 검색 -> best match dict 또는 None. 표준 라이브러리만 사용."""
    import urllib.parse, urllib.request
    from xml.etree import ElementTree as ET
    if target not in _FIELDS:
        return {"mapping_status": "not_applicable"}
    item_tag, id_tag, serial_tag, name_tag, eff_tag, cur_tag = _FIELDS[target]
    q = urllib.parse.urlencode({"OC": oc, "target": target, "type": "XML", "query": name})
    url = f"{LAW_SEARCH_URL}?{q}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            xml = r.read().decode("utf-8", "replace")
        time.sleep(sleep)
        root = ET.fromstring(xml)
    except Exception as e:  # noqa
        return {"mapping_status": "api_error", "error": str(e)[:120]}
    cands = []
    for el in root.iter(item_tag):
        get = lambda t: (el.findtext(t) or "").strip()
        nm = get(name_tag)
        if not nm:
            continue
        cands.append({
            "law_id": get(id_tag), "law_serial_no": get(serial_tag),
            "matched_name": nm, "enforcement_date": get(eff_tag),
            "current": get(cur_tag), "exact": (nm == name),
        })
    if not cands:
        return {"mapping_status": "not_found"}
    exact = [c for c in cands if c["exact"]]
    pool = exact or cands
    cur = [c for c in pool if c.get("current") in ("현행", "현행연혁")] or pool
    best = cur[0]
    best["mapping_status"] = "matched" if exact else "fuzzy"
    return best

# ── 4. 메인 ────────────────────────────────────────────────────
STD_FIELDS = ("law_id", "law_serial_no", "law_ref_type",
              "enforcement_date", "mapping_status", "mapping_confidence")

def enrich(items_dir, oc=None, apply=False, report_path=None):
    files = sorted(glob.glob(os.path.join(items_dir, "B*.json")))
    dist_names = OrderedDict()   # normalized_name -> {target, raws:set}
    rows = []
    tcount = Counter()
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        changed = False
        for lb in (data.get("legal_basis") or []):
            raw = (lb.get("name") or "").strip()
            if not raw:
                continue
            name, split_art, flags = normalize_name(raw)
            target = classify_target(name, raw)
            if name in MANUAL_OVERRIDE:
                target = MANUAL_OVERRIDE[name]; flags.append("manual_override")
            tcount[target] += 1
            key = (name, target)
            dist_names.setdefault(key, {"raws": set(), "flags": set()})
            dist_names[key]["raws"].add(raw)
            dist_names[key]["flags"].update(flags)
            # 조문 보강: 이름에서 분리된 조문이 있고 article 비었으면 채움
            if split_art and not (lb.get("article") or "").strip():
                if apply:
                    lb["article"] = split_art; changed = True
            # 표준 필드 채움
            res = {"law_ref_type": target, "mapping_status": "pending_api"}
            if target == "none":
                res["mapping_status"] = "not_applicable"
            elif oc and target in ("law", "admrul", "ordin"):
                res.update(resolve_law_id(name, target, oc))
                res["law_ref_type"] = target
            if apply:
                for k in STD_FIELDS:
                    if k in res:
                        lb[k] = res[k]
                lb["mapping_confidence"] = {"matched": "exact",
                                            "fuzzy": "fuzzy"}.get(res.get("mapping_status"), "")
                changed = True
        if apply and changed:
            bak = fp + "." + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".bak"
            with open(bak, "w", encoding="utf-8") as f:
                json.dump(json.load(open(fp, encoding="utf-8")), f, ensure_ascii=False, indent=2)
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    # 리뷰 표
    lines = ["# legal_basis 표준매핑 분류 리뷰 (dry-run)\n",
             f"- distinct (정제명, target): {len(dist_names)}종\n",
             f"- target 분포: " + ", ".join(f"{TARGET_LABEL[t]}={c}" for t, c in tcount.most_common()) + "\n",
             "\n| 정제 법령명 | target | 정제플래그 | 원표기 예 |",
             "|---|---|---|---|"]
    for (name, target), meta in sorted(dist_names.items(), key=lambda x: (x[0][1], x[0][0])):
        raws = "; ".join(sorted(meta["raws"]))[:60]
        flg = ",".join(sorted(meta["flags"])) or "-"
        lines.append(f"| {name} | {TARGET_LABEL[target]} | {flg} | {raws} |")
    report = "\n".join(lines) + "\n"
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
    print(report)
    print(f"[분포] " + " / ".join(f"{TARGET_LABEL[t]}:{c}" for t, c in tcount.most_common()))
    return tcount

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "items"))
    ap.add_argument("--oc", default=None, help="law.go.kr OC 인증키(이메일 ID)")
    ap.add_argument("--apply", action="store_true", help="item JSON 에 실제 반영")
    ap.add_argument("--report", default=None, help="리뷰 표 저장 경로(.md)")
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    enrich(a.items, oc=a.oc, apply=(a.apply and not a.report_only), report_path=a.report)
