# -*- coding: utf-8 -*-
"""답변 텍스트 숫자 정규화·에코 판정 (v1.40.0) — 의존성 없이 단독 실행 가능.

    python3 -m pytest tests/test_text_normalize.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from text_normalize import looks_like_echo, normalize_numbers  # noqa: E402


def test_phone_with_context_and_hyphen():
    assert normalize_numbers("대표전화는 일오칠칠에 천번입니다.") == "대표전화는 1577-1000번입니다."
    assert normalize_numbers("대표번호 일오칠칠 천번으로 전화하세요.") == "대표번호 1577-1000번으로 전화하세요."
    assert normalize_numbers("연락처는 공삼일 삼팔구 일이삼사입니다.") == "연락처는 031-389-1234입니다."
    assert normalize_numbers("팩스는 공삼일에 팔일칠오 삼사팔팔번입니다.") == "팩스는 031-8175-3488번입니다."
    assert normalize_numbers("전화번호는 공삼일 팔구구 육천 번입니다.") == "전화번호는 031-899-6000번입니다."


def test_digit_runs_without_context_stay_words():
    # 전화 맥락이 없고 '번/에/-' 도 안 따르면 낱자 나열은 낱말일 수 있다
    assert normalize_numbers("구사일생으로 살아났다는 이야기입니다.") == "구사일생으로 살아났다는 이야기입니다."
    assert normalize_numbers("이사 비용 문의는 삼십일 층으로.") == "이사 비용 문의는 31 층으로."


def test_address_after_road_name():
    assert normalize_numbers("관평로 백팔십이에 있습니다.") == "관평로 182에 있습니다."
    assert normalize_numbers("시민대로 이백삼십오이고요.") == "시민대로 235이고요."
    assert normalize_numbers("주소는 동안구 시민대로 이백삼십오입니다.") == "주소는 동안구 시민대로 235입니다."


def test_sino_numbers_with_units():
    assert normalize_numbers("십오층입니다. 오만원 지원. 이십사시간 상담, 삼백육십오일 운영.") == \
        "15층입니다. 50000원 지원. 24시간 상담, 365일 운영."
    assert normalize_numbers("만원입니다.") == "10000원입니다."


def test_single_syllable_numbers_untouched():
    # 한 글자 수는 뜻이 갈린다 — 손대지 않는다 ("이 층", "삼일", "사번")
    assert normalize_numbers("이 층에 있어요. 삼층으로 가세요. 사번 출구는 계단만 있습니다.") == \
        "이 층에 있어요. 삼층으로 가세요. 사번 출구는 계단만 있습니다."
    assert normalize_numbers("일주일에 삼일 정도") == "일주일에 삼일 정도"


def test_false_positive_guards():
    assert normalize_numbers("그렇게 하십시오. 원래 그렇습니다. 십시일반으로 돕습니다.") == \
        "그렇게 하십시오. 원래 그렇습니다. 십시일반으로 돕습니다."
    assert normalize_numbers("") == ""
    assert normalize_numbers(None) is None
    assert normalize_numbers("숫자 없는 문장입니다.") == "숫자 없는 문장입니다."


def test_already_digits_untouched():
    t = "안양지사는 관평로 182에 있고 대표전화는 1577-1000번입니다."
    assert normalize_numbers(t) == t


def test_echo_detection():
    assert looks_like_echo("요", "사용자분께 딱 맞는 지원 정책을 안내해 드릴게요")
    assert looks_like_echo("게요.", "안내해 드릴게요.")
    assert looks_like_echo(" 드릴게요 ", "안내해 드릴게요")
    assert not looks_like_echo("네", "안내해 드릴게요")
    assert not looks_like_echo("아니요", "안내해 드릴게요")        # 끝말 '요'만 겹치는 게 아니라 전체가 끝말이어야
    assert not looks_like_echo("안내해 드릴게요 감사합니다", "안내해 드릴게요")   # 길면 발화
    assert not looks_like_echo("", "안내해 드릴게요")
    assert not looks_like_echo("요", "")
