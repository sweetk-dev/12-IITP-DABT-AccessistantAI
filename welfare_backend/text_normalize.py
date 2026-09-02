# -*- coding: utf-8 -*-
"""상담 답변 텍스트 정규화 — 한글로 풀어 적힌 숫자를 아라비아 숫자로.

배경: 상담원 답변 텍스트는 Gemini Live 의 **출력 음성 전사**(output transcription)다.
음성으로 "일오칠칠에 천번", "관평로 백팔십이"라고 말한 것이 그대로 글자로 적히므로
같은 답변 안에서도 "182"와 "백팔십이"가 뒤섞여 표시됐다. 사람이 읽는 화면·대화 이력·
답변 카드에는 숫자 표기가 맞다(전화번호를 한글로 읽어 주는 화면은 없다).

보수적으로 바꾼다 — 숫자로 확신할 수 있는 자리만 손댄다.
  1) 전화번호: 숫자 낱자(공일이삼사오육칠팔구)가 3자 이상 이어지고 전화 맥락(전화·번호·
     연락처·콜센터·팩스…)이 같은 문장에 있거나, '번'·'에'·'-' 가 뒤따를 때 → 1577
     그리고 "1577에 1000번" 꼴은 "1577-1000번" 으로 붙인다.
  2) 단위 수: 십·백·천·만이 들어간 한자어 수(백팔십이, 이만, 천)가 단위(번·호·층·동·원·명·개·
     세·분·미터·킬로·호선·번지·년·월·일…) 앞이거나 도로명(로·길·동·가) 뒤에 올 때 → 182
     한 글자 수(이 층, 일 개)는 뜻이 갈릴 수 있어 건드리지 않는다.
"""
from __future__ import annotations

import re

_DIGIT = {"공": "0", "영": "0", "일": "1", "이": "2", "삼": "3", "사": "4",
          "오": "5", "육": "6", "륙": "6", "칠": "7", "팔": "8", "구": "9"}
_DIGIT_CLS = "[공영일이삼사오육륙칠팔구]"
_PHONE_CTX = re.compile(r"(전화|번호|연락처|콜센터|팩스|국번|상담|문의|다이얼|대표)")
_SENT_SPLIT = re.compile(r"(?<=[.!?다요\n])")

_UNITS = ("번길", "번지", "호선", "번", "호", "층", "동", "원", "명", "개", "세", "분", "시간",
          "시", "미터", "킬로미터", "킬로", "미리", "센티", "센치", "년", "월", "일", "회", "곳",
          "가지", "장", "매", "대", "권", "건", "건물", "차", "단계", "등급", "급", "인", "명분",
          "퍼센트", "프로", "배")
_ROAD = re.compile(r"(로|길|동|가)\s*$")

# 한자어 수 — 천/백/십 자리 + 일의 자리. 만 자리는 앞에 붙을 수 있다.
_SINO = re.compile(
    r"(?P<num>(?:[일이삼사오육륙칠팔구]?만)?(?:[일이삼사오육륙칠팔구]?천)?"
    r"(?:[일이삼사오육륙칠팔구]?백)?(?:[일이삼사오육륙칠팔구]?십)?[일이삼사오육륙칠팔구]?)"
)
_UNIT_RE = "|".join(sorted(_UNITS, key=len, reverse=True))
# 단위 뒤에는 조사·서술 어미만 허용 — "하십시오"의 "십시", "원래"의 "원" 같은 오탐을 막는다
_AFTER_UNIT = (r"(?:[^가-힣]|$|입니|이에|이고|이며|이라|이야|에|에서|으로|로|을|를|이|가|은|는|의|와|과|도|만|"
               r"까지|부터|씩|정도|이상|이하|째|간|짜리|쯤|당|마다|밖에|이면|이었|였)")
_SINO_UNIT = re.compile(r"(?<![가-힣\d])" + _SINO.pattern + r"(?=\s?(?:" + _UNIT_RE + r")" + _AFTER_UNIT + ")")
_ROAD_SINO = re.compile(r"(?P<road>[가-힣]+(?:로|길|동|가))\s?" + _SINO.pattern
                        + r"(?=[^가-힣]|$|에|에서|의|이|가|은|는|으로|로|입니|이에|번지|호)")


def _sino_to_int(s: str):
    if not s or not re.search(r"[십백천만]", s):
        return None                      # 단위 없는 한 글자 수는 뜻이 갈린다 — 손대지 않음
    total, cur = 0, 0
    for ch in s:
        if ch in _DIGIT:
            cur = int(_DIGIT[ch])
        elif ch == "십":
            total += (cur or 1) * 10; cur = 0
        elif ch == "백":
            total += (cur or 1) * 100; cur = 0
        elif ch == "천":
            total += (cur or 1) * 1000; cur = 0
        elif ch == "만":
            total = (total + (cur or (1 if total == 0 else 0))) * 10000; cur = 0
        else:
            return None
    return total + cur


def _phone_runs(sent: str) -> str:
    has_ctx = bool(_PHONE_CTX.search(sent))

    def repl(m):
        run = m.group(0)
        rest = sent[m.end():]
        tail = rest[:1]
        after_ok = (tail in ("번", "에", "-", "의", "") or tail.isspace()
                    or rest.startswith(("입니", "이에", "이고", "이며", "이라", "로", "으로", "까지", "이나")))
        if len(run) >= 3 and (has_ctx and after_ok or len(run) >= 4 and tail in ("번", "에", "-")):
            return "".join(_DIGIT[c] for c in run)
        return run
    return re.sub(r"(?<![가-힣])" + _DIGIT_CLS + "{3,}", repl, sent)


def _join_phone(out: str) -> str:
    """"1577에 1000번" / "031 389 1234" / "031에 8175 3488" → 하이픈 결합 (숫자 변환 뒤에)."""
    out = re.sub(r"(?<!\d)(\d{2,4})\s?(?:에|의)\s?(\d{3,4})(?=번|[^\d]|$)", r"\1-\2", out)
    out = re.sub(r"(?<!\d)(\d{2,4})-(\d{3,4})\s?(?:에|의|\s)\s?(\d{4})(?=번|[^\d]|$)", r"\1-\2-\3", out)
    out = re.sub(r"(?<![\d-])(\d{2,4}) (\d{3,4}) (\d{4})(?=번|[^\d]|$)", r"\1-\2-\3", out)
    out = re.sub(r"(?<![\d-])(\d{4}) (\d{4})(?=번)", r"\1-\2", out)
    out = re.sub(r"(\d{3,4}-\d{3,4})\s번", r"\1번", out)
    return out


def _sino_units(sent: str) -> str:
    def repl(m):
        val = _sino_to_int(m.group("num"))
        return str(val) if val is not None else m.group(0)
    out = _SINO_UNIT.sub(lambda m: repl(m) if m.group("num") else m.group(0), sent)

    def road_repl(m):
        val = _sino_to_int(m.group("num"))
        if val is None:
            return m.group(0)
        return "%s %d" % (m.group("road"), val)
    out = _ROAD_SINO.sub(lambda m: road_repl(m) if m.group("num") else m.group(0), out)
    return out


def normalize_numbers(text: str) -> str:
    """답변 텍스트의 한글 숫자를 아라비아 숫자로. 확신이 없는 자리는 그대로 둔다."""
    if not text or not re.search(_DIGIT_CLS + "|[십백천만]", text):
        return text
    parts = _SENT_SPLIT.split(text)
    out = []
    for p in parts:
        if not p:
            continue
        p = _phone_runs(p)
        p = _sino_units(p)
        p = _join_phone(p)
        out.append(p)
    return "".join(out)


def looks_like_echo(text: str, ai_tail: str) -> bool:
    """짧은 사용자 전사가 직전 상담원 발화의 끝말과 같으면 스피커 에코로 본다.

    "안내해 드릴게요" 뒤에 사용자 전사 "요"·"게요" 가 오는 것이 전형이다. 진짜 짧은
    대답("네", "아니요")은 상담원 끝말과 겹치지 않으므로 걸리지 않는다.
    """
    t = re.sub(r"[\s.,!?~…'\"]", "", text or "")
    a = re.sub(r"[\s.,!?~…'\"]", "", ai_tail or "")
    return 0 < len(t) <= 4 and len(a) >= len(t) and a.endswith(t)
