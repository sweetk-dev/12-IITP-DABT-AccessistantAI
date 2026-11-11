# -*- coding: utf-8 -*-
"""Gemini 클라이언트 생성 — 한곳에서 만든다.

SDK 기본 API 버전은 SDK 주 버전에 따라 달라진다(3.x 는 v1). 이 서비스가 쓰는
음성(Live) 연결·임베딩·경량 생성 모델은 v1beta 에서 제공되므로, SDK 기본값에
기대지 않고 API 버전을 항상 명시한다. 환경변수 GEMINI_API_VERSION 으로 바꿀 수 있다.
"""
import os

# 음성(Live)·임베딩·미리보기 모델이 제공되는 API 버전
DEFAULT_API_VERSION = "v1beta"


def api_version() -> str:
    """사용할 API 버전 — 환경변수가 비어 있으면 기본값."""
    return (os.environ.get("GEMINI_API_VERSION") or "").strip() or DEFAULT_API_VERSION


def make_client(api_key: str):
    """API 버전을 명시한 Gemini 클라이언트. 키가 없으면 None."""
    if not api_key:
        return None
    from google import genai  # 지연 임포트 — SDK 없이도 이 모듈을 불러올 수 있게
    return genai.Client(api_key=api_key, http_options={"api_version": api_version()})
