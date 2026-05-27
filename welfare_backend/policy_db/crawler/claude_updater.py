# crawler/claude_updater.py  (LLM Updater — Backend-agnostic)
# 정책 항목 JSON 을 LLM 으로 갱신. 백엔드는 llm_backends.get_backend() 가 결정.
#
# 백엔드 옵션 (환경변수 LLM_BACKEND):
#   - "claude" (기본, 외부 Anthropic API)
#   - "gemma"  (온프레미스 — Ollama / vLLM 호환)
#
# 흐름:
#   1) 기존 items/B0XX.json + 변경된 출처(들)의 본문 텍스트를 모음
#   2) LLMBackend.generate_json_update() 호출 — temperature 0, schema 보존 SI
#   3) 반환 JSON 의 schema 검증
#   4) PASS 시 staging/B0XX_*.json 으로 저장 + diff 요약 반환
#
# 안전 장치:
#   - schema 검증 실패 시 staging 저장 안 함, 에러 반환
#   - last_verified·version 자동 갱신
#   - LLM 의 의역·추가 정보 노이즈 방지: temperature 0
#
# 파일명은 호환성 위해 claude_updater.py 유지 (외부 import 영향 없음).
# 향후 llm_updater.py 로 리네임할 때 import 만 갱신하면 됨.
import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional, Tuple

import jsonschema

try:
    from .llm_backends import get_backend, LLMBackend
except ImportError:
    from llm_backends import get_backend, LLMBackend  # type: ignore

logger = logging.getLogger(__name__)


# 안전한 시스템 프롬프트 — Hallucination 방지 + schema 보존
SYSTEM_PROMPT = """당신은 한국 장애인 복지 정책 데이터베이스의 항목 JSON 을 정확하게 갱신하는 정밀 엔진입니다.

## 절대 규칙
1. **schema 보존**: 입력으로 받은 JSON 의 모든 필드를 그대로 유지하세요. 새 필드를 추가하거나 기존 필드를 제거하지 마세요.
2. **사실 변경만 반영**: 제공된 "변경된 출처 본문"에서 명확히 새로 확인되는 사실(금액·시행일·신청처·자격기준 등)만 업데이트하세요.
3. **추측·추론 금지**: 출처에 명시되지 않은 정보는 절대 추가·변경하지 마세요. 기존 내용이 더 정확하다고 판단되면 유지.
4. **문장 스타일 유지**: 기존 항목의 문체·요약 길이·테이블 구조를 그대로 유지하세요.
5. **last_verified 갱신**: 오늘 날짜로 업데이트.
6. **version 갱신**: minor 자릿수 +0.1 (예: 1.1.0 → 1.2.0).
7. **변경이 모호하면 보존**: 출처 본문이 짧거나 불명확하면 해당 필드를 그대로 두세요.
8. **답변 형식**: 오직 갱신된 JSON 본체 1개만 반환. 설명·코드블록·머리말·꼬리말 없이 순수 JSON.

## 출력 형식
```
{"id": "B001", ... 전체 JSON 그대로 ...}
```
(위의 ``` 는 예시 표기일 뿐, 실제 답변에는 ``` 을 포함하지 마세요.)
"""

USER_TEMPLATE = """[기존 항목 JSON]
{existing_json}

[변경된 출처들]
{sources_block}

위 출처에서 확인된 변경 사실만 반영해 갱신된 JSON 을 반환하세요. 변경이 모호하거나 출처 본문이 부족하면 기존 값을 그대로 유지하세요."""


def _build_sources_block(related_changes: list) -> str:
    """각 변경 출처를 텍스트 블록으로 정리."""
    blocks = []
    for ch in related_changes:
        snap_html = ROOT_HINT(ch.get("snapshot_dir", ""))  # type: ignore
        text_excerpt = _read_latest_snapshot(ch)
        blocks.append(
            f"### {ch['target_id']} ({ch.get('publisher','?')})\n"
            f"- URL: {ch['url']}\n"
            f"- 변경 사유: {ch['reason']}\n"
            f"- 본문 발췌 (앞 3000자):\n{text_excerpt[:3000]}\n"
        )
    return "\n".join(blocks) if blocks else "(변경 본문 없음)"


def ROOT_HINT(s: str) -> str:  # placeholder for path hint
    return s


def _extract_main_text_from_html(raw: bytes) -> str:
    """HTML bytes 에서 메인 콘텐츠 텍스트만 추출 (Boilerplate Removal).

    Phase 5 보완: 단순 raw 디코딩 + 3000자 자르기 → nav/CSS/script 찌꺼기 때문에
    핵심 정책 본문이 뒤로 밀려 잘려나가는 문제. BeautifulSoup + readability-lxml 로
    유효 텍스트만 추출해 LLM 프롬프트 효율 극대화.

    추출 우선순위 (한국 공공기관 사이트에서 본문 추출 정확도 순):
      1) **trafilatura.extract()** — 뉴스/공공기관 게시글 본문 추출에 최강. 1순위.
      2) BeautifulSoup + <main>/<article>/#content 등 주요 컨테이너 셀렉터
      3) readability-lxml Document.summary() 폴백
      4) 전체 body 텍스트 폴백

    파이프라인:
      a) 인코딩 추정 (UTF-8 우선, chardet 폴백)
      b) trafilatura 1차 시도 — 성공하면 바로 반환
      c) 실패 시 BeautifulSoup 으로 노이즈 제거 후 셀렉터/readability 폴백
      d) 공백 정리한 순수 텍스트 반환
    """
    from bs4 import BeautifulSoup  # 지연 import: detectors 의존성 최소 원칙 유지

    # a) 디코딩
    try:
        html_str = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            import chardet
            enc = (chardet.detect(raw) or {}).get("encoding") or "utf-8"
            html_str = raw.decode(enc, errors="replace")
        except Exception:
            html_str = raw.decode("latin-1", errors="replace")

    # b) trafilatura 1차 시도 — 본문 추출 최적
    try:
        import trafilatura
        # favor_recall=True: 공공기관 게시글처럼 boilerplate 가 적은 페이지에서 본문 누락 방지
        # include_comments=False: 댓글·게시판 답글 제외
        extracted = trafilatura.extract(
            html_str,
            favor_recall=True,
            include_comments=False,
            include_tables=True,  # 정책 비교표 보존
            no_fallback=False,
        )
        if extracted and len(extracted.strip()) >= 200:
            return re.sub(r"\s+", " ", extracted).strip()
        # 200자 미만이면 너무 짧은 추출 — 다음 단계로 폴백
    except Exception as e:
        logger.debug("trafilatura 추출 실패, 폴백으로 진행: %s", e)

    soup = BeautifulSoup(html_str, "lxml")

    # 2) 노이즈 태그 제거
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer",
                     "aside", "form", "iframe", "svg", "button", "input"]):
        tag.decompose()

    # 흔한 비콘텐츠 영역 (한국 정부·지자체 사이트 GNB/LNB 명명 패턴 포함)
    NOISE_SELECTORS = [
        "[role='navigation']", "[role='banner']", "[role='contentinfo']",
        ".gnb", ".lnb", ".snb", ".header", ".footer", ".nav", ".menu",
        ".sidebar", ".side", ".aside", ".breadcrumb", ".breadcrum",
        "#gnb", "#lnb", "#snb", "#header", "#footer", "#nav", "#sidebar",
        ".skip", ".util", ".sns", ".banner", ".ad", ".advertisement",
        ".pagination", ".paging", ".search-form", ".search_form",
        ".copyright", ".social", ".share",
    ]
    for sel in NOISE_SELECTORS:
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception:
            pass  # 잘못된 셀렉터는 조용히 무시

    # 3) 메인 컨테이너 탐색 — 의미 있는 길이(200자+) 가진 첫 번째 매치 사용
    main_candidates = [
        "main", "article", "[role='main']",
        "#content", ".content", "#contents", ".contents",
        "#main", ".main", "#container", ".container",
        "#bbsView", "#cntntsBox", ".board_view", ".view_cont",
    ]
    main_node = None
    for sel in main_candidates:
        try:
            cand = soup.select_one(sel)
        except Exception:
            continue
        if cand and len(cand.get_text(strip=True)) >= 200:
            main_node = cand
            break

    if main_node is not None:
        extracted = main_node.get_text(separator=" ", strip=True)
    else:
        # 4) readability 폴백 — 가독성 알고리즘으로 본문 추출
        extracted = ""
        try:
            from readability import Document  # readability-lxml
            doc = Document(html_str)
            summary_html = doc.summary(html_partial=True)
            extracted = BeautifulSoup(summary_html, "lxml").get_text(separator=" ", strip=True)
        except Exception as e:
            logger.debug("readability 폴백 실패: %s", e)
        # 그래도 비면 전체 body 텍스트
        if not extracted:
            body = soup.body or soup
            extracted = body.get_text(separator=" ", strip=True)

    # 5) 공백 정리
    extracted = re.sub(r"\s+", " ", extracted).strip()
    return extracted


def _extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """PDF bytes 에서 텍스트 추출 (Phase 5 — pypdf 통합).

    실패 시 안전한 placeholder 반환 — LLM 에는 "URL 직접 확인 권장" 명시.
    """
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(chunks)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return "(PDF 본문 추출 결과 비어있음 — URL 직접 확인 권장)"
        return text
    except Exception as e:
        logger.warning("PDF 텍스트 추출 실패: %s", e)
        return "(PDF 본문 추출 실패 — URL 직접 확인 권장)"


def _read_latest_snapshot(ch: dict) -> str:
    """스냅샷의 latest.html / latest.pdf 에서 정제 텍스트 추출.

    Phase 5 보완:
      - HTML: raw 디코딩 → BeautifulSoup+readability 로 메인 본문만 추출
      - PDF : 메시지만 반환 → pypdf 로 실제 텍스트 추출
    """
    try:
        from pathlib import Path as _P
        snap_dir = _P(__file__).resolve().parent.parent / ch.get("snapshot_dir", "")
        for ext in ("html", "pdf"):
            f = snap_dir / f"latest.{ext}"
            if not f.exists():
                continue
            raw = f.read_bytes()
            if ext == "html":
                cleaned = _extract_main_text_from_html(raw)
                logger.info("📄 HTML 정제: %s — raw %dB → cleaned %d자",
                            f.name, len(raw), len(cleaned))
                return cleaned
            else:
                text = _extract_text_from_pdf(raw)
                logger.info("📄 PDF 추출: %s — raw %dB → text %d자",
                            f.name, len(raw), len(text))
                return text
    except Exception as e:
        logger.warning("스냅샷 읽기 실패: %s", e)
    return "(스냅샷 본문을 찾을 수 없음 — URL 직접 확인 필요)"


def _bump_version(v: str) -> str:
    """1.1.0 → 1.2.0 형태로 minor +1."""
    try:
        parts = v.split(".")
        if len(parts) >= 2:
            parts[1] = str(int(parts[1]) + 1)
            if len(parts) >= 3:
                parts[2] = "0"
            return ".".join(parts)
    except Exception:
        pass
    return v + ".1"


def _diff_summary(old: dict, new: dict) -> str:
    """간단한 diff 요약 (큰 필드는 길이 변화만, 작은 필드는 값 변화)."""
    diffs = []
    keys = set(old.keys()) | set(new.keys())
    for k in sorted(keys):
        ov, nv = old.get(k), new.get(k)
        if ov == nv:
            continue
        if isinstance(ov, (list, dict)) or isinstance(nv, (list, dict)):
            ol = len(json.dumps(ov, ensure_ascii=False)) if ov is not None else 0
            nl = len(json.dumps(nv, ensure_ascii=False)) if nv is not None else 0
            diffs.append(f"{k}: {ol}B → {nl}B")
        else:
            ov_s = str(ov)[:40] if ov else "(없음)"
            nv_s = str(nv)[:40] if nv else "(없음)"
            diffs.append(f"{k}: '{ov_s}' → '{nv_s}'")
    return "; ".join(diffs[:6]) + (" …" if len(diffs) > 6 else "") if diffs else "변경 사항 없음"


async def update_item_via_claude(
    *,
    item_path: Path,
    related_changes: list,
    staging_dir: Path,
    schema_path: Path,
    backend: Optional[LLMBackend] = None,
    max_tokens: int = 16000,
) -> Tuple[Optional[Path], str]:
    """단일 항목 JSON 을 LLM 으로 갱신해 staging/ 에 저장.

    함수명은 호환성 위해 update_item_via_claude 유지하지만, 실제 LLM 은
    환경변수 LLM_BACKEND (claude/gemma) 에 따라 선택됨.
    """
    existing = json.loads(item_path.read_text(encoding="utf-8"))
    existing_json_str = json.dumps(existing, ensure_ascii=False, indent=2)
    sources_block = _build_sources_block(related_changes)

    user_msg = USER_TEMPLATE.format(
        existing_json=existing_json_str,
        sources_block=sources_block,
    )

    if backend is None:
        backend = get_backend()
    logger.info("📡 LLM 호출 (backend=%s, model=%s, prompt=%dB)",
                backend.name, backend.model, len(user_msg))

    raw = await backend.generate_json_update(
        system_prompt=SYSTEM_PROMPT,
        user_message=user_msg,
        max_tokens=max_tokens,
    )
    # 종종 코드블록 형태로 올 수 있어 stripping
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()

    try:
        new_data = json.loads(raw)
    except Exception as e:
        logger.error("Claude 응답이 유효한 JSON 이 아님: %s", e)
        # 디버깅 파일로 저장
        debug = staging_dir / f"{existing['id']}_FAILED_{date.today()}.txt"
        debug.write_text(raw, encoding="utf-8")
        raise RuntimeError(f"Claude 응답 JSON 파싱 실패 — {debug}")

    # last_verified, version 자동 갱신 (Claude 가 깜빡한 경우 보강)
    new_data["last_verified"] = date.today().isoformat()
    if existing.get("version") and new_data.get("version") == existing.get("version"):
        new_data["version"] = _bump_version(existing["version"])

    # schema 검증
    if schema_path.exists():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft7Validator(schema)
        errs = list(validator.iter_errors(new_data))
        if errs:
            err_msgs = [f"{list(e.path)}: {e.message[:100]}" for e in errs[:5]]
            raise RuntimeError(f"schema 검증 실패 ({len(errs)}건): " + " / ".join(err_msgs))

    # staging 저장
    staging_dir.mkdir(parents=True, exist_ok=True)
    fname = item_path.name.replace(".json", f".{date.today().isoformat()}.staged.json")
    staged_path = staging_dir / fname
    staged_path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("💾 staging 저장: %s", staged_path)

    diff = _diff_summary(existing, new_data)
    return staged_path, diff
