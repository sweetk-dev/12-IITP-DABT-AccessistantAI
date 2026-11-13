# -*- coding: utf-8 -*-
"""정책 항목 자격요건의 구조화 칸 — 소득기준 여부 · 나이 범위 · 중증도.

정책 DB 의 has_income_criteria · age_min · age_max · severity_levels 는 검색 조건과
상담 도구의 구조화 응답에 쓰인다. 값은 항목 파일 eligibility 의 명시 칸을 따른다.

    has_income_criteria  신청 자격에 소득·재산 조건이 있으면 true (일부 유형에만 있어도 true).
                         본인부담금 차등·지원 순서에만 소득을 쓰면 false.
    age_min / age_max    만 나이, 둘 다 포함 경계(이상·이하). "만 65세 미만" 은 age_max 64.
                         원칙 기준이며 예외는 age_criteria 문구에 둔다. 제한이 없으면 null.
    severity_levels      대상 중증도. 빈 배열은 중증도 제한 없음.

명시 칸이 없는 항목(예전 파일·초안)은 문구에서 보수적으로 추정한다. 문구가 있다는 사실만으로
소득기준이 있다고 보면 "소득기준 없음" 같은 문구가 '있음'으로 바뀌므로 부정 표현을 먼저 본다.
"""
import re

SEVERE = "심한 장애(중증)"
MILD = "심하지 않은 장애(경증)"
SEVERITY_ALL = [SEVERE, MILD]

# 소득 조건이 없다는 표현 — 첫 문장에서만 본다(뒤쪽 예외 설명의 "무관" 은 원칙이 아니다)
_NO_INCOME = re.compile(
    r"소득\s*(?:기준|요건|제한|조건)?\s*(?:이|은|과)?\s*(?:없|무관|불문|폐지|관계\s*없|상관\s*없)"
    r"|소득에\s*관계\s*없|해당\s*없음")
# 소득 조건이 있다는 표현
_HAS_INCOME = re.compile(
    r"중위\s*소득|수급자|수급권자|차상위|저소득|월평균\s*소득|소득\s*인정액|선정\s*기준액"
    r"|건강보험료|소득\s*\d|소득\s*금액|\d[\d,.]*\s*(?:만\s*)?원\s*이하")
# 나이 제한이 없다는 표현 · 원칙 문장이 끝나는 곳
_NO_AGE = re.compile(r"(?:연령|나이)\s*(?:요건|제한|기준)?\s*(?:이|은)?\s*(?:없|무관|불문)")
_VARIES = re.compile(r"차등|상이|별로\s*다름|지자체별|품목별|시설\s*종류별")
_PRINCIPLE_END = re.compile(r"단[,\s]|다만|예외|①|\.\s|。")
_AGE_RANGE = re.compile(
    r"(?:만\s*)?(\d{1,3})\s*세\s*(이상|초과)?\s*[~∼\-]\s*(?:만\s*)?(\d{1,3})\s*세\s*(이하|미만)?")
_AGE_MIN = re.compile(r"(?:만\s*)?(\d{1,3})\s*세\s*(이상|초과)")
_AGE_MAX = re.compile(r"(?:만\s*)?(\d{1,3})\s*세\s*(이하|미만)")


def derive_income_flag(income_text, target_text=None):
    """문구로 소득기준 여부를 추정한다. income_criteria 가 있으면 그것만, 없으면 대상 문구를 본다."""
    text = (income_text or "").strip()
    if text:
        head = re.split(r"[.。]\s|단[,\s]", text, maxsplit=1)[0][:60]
        if _NO_INCOME.search(head) and not _HAS_INCOME.search(head):
            return False
        return True
    return bool(_HAS_INCOME.search(target_text or ""))


def parse_age_bounds(age_text):
    """문구의 원칙 부분에서 (age_min, age_max) 를 포함 경계로 뽑는다. 정하기 어려우면 (None, None)."""
    text = (age_text or "").strip()
    if not text:
        return None, None
    head = text[:40]
    if _NO_AGE.search(head) or _VARIES.search(head):
        return None, None
    m = _PRINCIPLE_END.search(text)
    principle = text[:m.start()] if m and m.start() > 0 else text
    lo = hi = None
    r = _AGE_RANGE.search(principle)
    if r:
        lo = int(r.group(1)) + (1 if r.group(2) == "초과" else 0)
        hi = int(r.group(3)) - (1 if r.group(4) == "미만" else 0)
    else:
        a = _AGE_MIN.search(principle)
        b = _AGE_MAX.search(principle)
        if a:
            lo = int(a.group(1)) + (1 if a.group(2) == "초과" else 0)
        if b:
            hi = int(b.group(1)) - (1 if b.group(2) == "미만" else 0)
    if lo is not None and hi is not None and lo > hi:
        return None, None
    return lo, hi


def derive_severity(text):
    """대상 문구에 적힌 중증도. 중증만 적혀 있으면 [중증], 둘 다면 둘 다, 없으면 [](제한 없음)."""
    t = text or ""
    mild = bool(re.search(r"심하지\s*않은|경증", t))
    rest = re.sub(r"심하지\s*않은", "", t)
    severe = bool(re.search(r"심한\s*장애|정도가\s*심한|중증", rest))
    if severe and mild:
        return list(SEVERITY_ALL)
    if severe:
        return [SEVERE]
    if mild:
        return [MILD]
    return []


def structured_fields(eligibility):
    """적재용 값: (has_income_criteria, age_min, age_max, severity_levels). 명시 칸이 우선."""
    e = eligibility if isinstance(eligibility, dict) else {}
    if isinstance(e.get("has_income_criteria"), bool):
        income = e["has_income_criteria"]
    else:
        income = derive_income_flag(e.get("income_criteria"), e.get("target"))
    if "age_min" in e or "age_max" in e:
        age_min, age_max = e.get("age_min"), e.get("age_max")
    else:
        age_min, age_max = parse_age_bounds(e.get("age_criteria"))
    if isinstance(e.get("severity_levels"), list):
        severity = e["severity_levels"]
    else:
        severity = derive_severity(e.get("target"))
    return income, age_min, age_max, severity


def fill_missing(eligibility):
    """비어 있는 명시 칸만 문구에서 추정해 채운다(이미 있는 값은 그대로). 등록 전 초안 보정용."""
    if not isinstance(eligibility, dict):
        return eligibility
    income, age_min, age_max, severity = structured_fields(eligibility)
    if not isinstance(eligibility.get("has_income_criteria"), bool):
        eligibility["has_income_criteria"] = income
    if "age_min" not in eligibility and "age_max" not in eligibility:
        eligibility["age_min"], eligibility["age_max"] = age_min, age_max
    if not isinstance(eligibility.get("severity_levels"), list):
        eligibility["severity_levels"] = severity
    return eligibility
