# crawler/llm_backends.py
# LLM 갱신 엔진 추상화 — 외부 API(Anthropic)와 온프레미스 LLM(Gemma 등)을 동일 인터페이스로.
#
# 현재 단계: claude (Anthropic API) — 검증·기본값
# 향후 단계: gemma (Ollama / vLLM / 자체 HTTP 서빙) — 온프레미스 서버 도입 시
#
# 백엔드 선택은 환경변수 LLM_BACKEND 로:
#   LLM_BACKEND=claude   (기본)
#   LLM_BACKEND=gemma
#
# 각 백엔드는 동일한 generate_json_update() 시그니처를 따름:
#   async def generate_json_update(system_prompt: str, user_message: str,
#                                  max_tokens: int) -> str
#
# 반환은 "갱신된 JSON 본체 문자열" 1개. 파싱/검증은 호출자(claude_updater) 책임.
import os
import logging
from abc import ABC, abstractmethod
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class LLMBackend(ABC):
    """모든 백엔드의 공통 인터페이스."""

    name: str = "base"
    model: str = ""

    @abstractmethod
    async def generate_json_update(
        self, *, system_prompt: str, user_message: str, max_tokens: int = 16000
    ) -> str:
        ...


# ─────────────────────────────────────────────────────────────
# 1) Anthropic Claude — 외부 API
# ─────────────────────────────────────────────────────────────
class AnthropicBackend(LLMBackend):
    name = "claude"

    def __init__(self, model: Optional[str] = None):
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY 환경변수가 비어 있습니다 — welfare_backend/.env 확인"
            )
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise RuntimeError("anthropic SDK 미설치 — pip install anthropic") from e
        self._client = Anthropic(api_key=api_key)

    async def generate_json_update(
        self, *, system_prompt: str, user_message: str, max_tokens: int = 16000
    ) -> str:
        # anthropic SDK 는 sync — to_thread 로 비동기화
        import asyncio
        resp = await asyncio.to_thread(
            self._client.messages.create,
            model=self.model,
            max_tokens=max_tokens,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return resp.content[0].text if resp.content else ""


# ─────────────────────────────────────────────────────────────
# 2) Gemma (온프레미스) — Ollama / vLLM / 자체 HTTP 서빙 호환
# ─────────────────────────────────────────────────────────────
class GemmaBackend(LLMBackend):
    """
    온프레미스 Gemma(또는 다른 오픈모델) 호출. 기본은 Ollama 호환 API.

    환경변수:
      GEMMA_API_URL   기본 http://127.0.0.1:11434  (Ollama 기본 포트)
      GEMMA_MODEL     기본 gemma-3n-9b 또는 사용자 환경의 모델명
      GEMMA_API_KEY   (선택) vLLM·자체 서빙에 Bearer 인증이 있는 경우
      GEMMA_API_STYLE 기본 ollama. 또는 openai (OpenAI-compatible /v1/chat/completions)

    Ollama 스타일:
      POST /api/chat
        { model, messages: [{role:'system',...},{role:'user',...}], options:{temperature:0} }

    OpenAI 스타일 (vLLM·LiteLLM 등):
      POST /v1/chat/completions
        { model, messages, temperature, max_tokens }
    """

    name = "gemma"

    def __init__(
        self,
        api_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        api_style: Optional[str] = None,
    ):
        self.api_url = (api_url or os.environ.get("GEMMA_API_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.model = model or os.environ.get("GEMMA_MODEL", "gemma-3n")
        self.api_key = api_key or os.environ.get("GEMMA_API_KEY") or None
        self.api_style = (api_style or os.environ.get("GEMMA_API_STYLE", "ollama")).lower()
        self._timeout = httpx.Timeout(180.0, connect=10.0)  # 온프레미스는 느릴 수 있음

    async def generate_json_update(
        self, *, system_prompt: str, user_message: str, max_tokens: int = 16000
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        if self.api_style == "openai":
            url = f"{self.api_url}/v1/chat/completions"
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
            }
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        else:
            # Ollama 기본
            url = f"{self.api_url}/api/chat"
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "options": {"temperature": 0, "num_predict": max_tokens},
                "stream": False,
            }
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data.get("message", {}).get("content", "")


# ─────────────────────────────────────────────────────────────
# 3) Gemini (외부 API) — Google generativelanguage REST
# ─────────────────────────────────────────────────────────────
class GeminiBackend(LLMBackend):
    """
    Google Gemini API(generativelanguage REST) 호출. 임베딩과 동일한 GEMINI_API_KEY 재사용.

    환경변수:
      GEMINI_API_KEY    (필수) Google AI Studio 키 — 임베딩과 공유
      GEMINI_LLM_MODEL  기본 gemini-3.1-pro-preview
      GEMINI_API_URL    기본 https://generativelanguage.googleapis.com

    generateContent 호출:
      POST /v1beta/models/{model}:generateContent
        { systemInstruction, contents,
          generationConfig:{temperature:0, responseMimeType:'application/json'} }
    응답 JSON 본문(문자열) 1개 반환 — 파싱/검증은 claude_updater 책임(백엔드 무관).
    """

    name = "gemini"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        api_url: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "GEMINI_API_KEY 환경변수가 비어 있습니다 — welfare_backend/.env 확인"
            )
        self.model = model or os.environ.get("GEMINI_LLM_MODEL", "gemini-3.1-pro-preview")
        self.api_url = (
            api_url or os.environ.get("GEMINI_API_URL")
            or "https://generativelanguage.googleapis.com"
        ).rstrip("/")
        self._timeout = httpx.Timeout(180.0, connect=10.0)

    async def generate_json_update(
        self, *, system_prompt: str, user_message: str, max_tokens: int = 16000
    ) -> str:
        url = f"{self.api_url}/v1beta/models/{self.model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        # thinking 파트(thought=True)는 제외하고 실제 응답 텍스트만 결합
        texts = [
            p.get("text", "")
            for p in parts
            if p.get("text") and not p.get("thought")
        ]
        return "".join(texts)


# ─────────────────────────────────────────────────────────────
# 팩토리
# ─────────────────────────────────────────────────────────────
def get_backend(name: Optional[str] = None) -> LLMBackend:
    """LLM_BACKEND 환경변수 또는 인자 기반으로 백엔드 인스턴스 반환."""
    name = (name or os.environ.get("LLM_BACKEND", "claude")).lower()
    if name == "claude":
        return AnthropicBackend()
    if name in ("gemma", "ollama"):
        return GemmaBackend()
    if name == "gemini":
        return GeminiBackend()
    raise ValueError(f"unknown LLM_BACKEND: {name} (지원: claude, gemma, gemini)")
