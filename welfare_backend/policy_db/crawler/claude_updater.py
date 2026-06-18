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
SYSTEM_PROMPT = """당신은 한국 장애인 복지 정책 데이터베이스 항목을 갱신하는 정밀 엔진입니다.
전체 JSON 을 다시 쓰지 말고, 변경된 필드만 담은 패치만 반환하세요.

## 절대 규칙
1. 변경된 필드만: 출처에서 명확히 새로 확인된 사실(금액·시행일·신청처·자격기준 등)에 한해 패치를 만드세요. 변경 없는 필드는 패치에 포함하지 마세요.
2. 추측 금지: 출처에 명시되지 않은 정보는 만들지 마세요. 모호하면 패치하지 마세요.
3. 삭제 신중: 단지 출처에 안 보인다는 이유로 delete 하지 마세요. 폐지·종료·미시행 등 명시적 종료 근거가 출처에 있을 때만 delete 패치를 만들고 evidence 에 그 문구를 인용하세요.
4. evidence 필수: 각 패치에 근거가 된 출처 문장을 evidence 로 적고, confidence 는 high/medium/low 로 표기하세요.
5. 답변 형식: 오직 아래 JSON 1개만. 설명·코드블록·머리말·꼬리말 없이 순수 JSON.

## 출력 형식
{"patches": [{"op": "update", "path": "supported_amount.rate", "old": "100%", "new": "90%", "evidence": "...", "confidence": "high"}]}
- op: update(값 교체) / add(필드 추가 또는 리스트 append) / delete(검토 대상 표시)
- path: 점(.)으로 구분한 필드 경로. add 의 대상이 리스트면 value 를 append.
- 변경이 전혀 없으면 {"patches": []} 를 반환하세요.


## 추가 규칙 (교차검증)
6. 기존 값 교차검증·정정: [기존 항목 JSON]의 값이 [출처 본문]과 명백히 불일치하면(특히 금액·요율·할인율·면제여부·인원수·자격요건·시행일 등 핵심 사실), 출처를 근거로 정정하는 update 패치를 반드시 만드세요. 출처에 숫자가 직접 없어도 '무임/전액 면제/100%' 같은 표현으로 값이 명확히 결정되면 그 함의를 반영하세요. 단 출처에 근거가 전혀 없으면 추측하지 말고 패치하지 마세요.
"""

USER_TEMPLATE = """[기존 항목 JSON]
{existing_json}

[출처들]
{sources_block}

위 출처를 기준으로 (a) 새로 변경된 사실과 (b) 기존 항목 값이 출처와 명백히 불일치하는 부분을 모두 정정 update 패치로 반환하세요. 근거 없는 추측은 금지. 변경/불일치가 전혀 없으면 {{"patches": []}}."""


def _build_sources_block(related_changes: list) -> str:
    """각 변경 출처를 텍스트 블록으로 정리."""
    blocks = []
    for ch in related_changes:
        text_excerpt = _read_latest_snapshot(ch)
        blocks.append(
            f"### {ch['target_id']} ({ch.get('publisher','?')})\n"
            f"- URL: {ch['url']}\n"
            f"- 변경 사유: {ch['reason']}\n"
            f"- 본문 발췌 (앞 3000자):\n{text_excerpt[:3000]}\n"
        )
    return "\n".join(blocks) if blocks else "(변경 본문 없음)"


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


# ── 필드 단위 패치 (C14) ─────────────────────────────────────
# LLM 은 전체 JSON 대신 아래 형식의 패치만 반환한다:
#   {"patches": [
#     {"op":"update","path":"supported_amount.rate","old":"100%","new":"90%",
#      "evidence":"...","confidence":"high"},
#     {"op":"add","path":"faq","value":{"q":"...","a":"..."},"evidence":"...","confidence":"medium"},
#     {"op":"delete","path":"operating_agencies","evidence":"...","confidence":"low"}
#   ]}
# path 는 점(.)으로 dict 를 따라가는 경로. add 의 대상이 list 면 value 를 append.

def _get_parent(doc: dict, path: str, create: bool = False):
    """점 경로의 부모 dict 와 마지막 키를 반환. 실패 시 (None, None)."""
    keys = path.split(".")
    cur = doc
    for k in keys[:-1]:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        elif create and isinstance(cur, dict):
            cur[k] = {}
            cur = cur[k]
        else:
            return None, None
    return (cur, keys[-1]) if isinstance(cur, dict) else (None, None)


def _set_by_path(doc: dict, path: str, value, require_exists: bool = True) -> bool:
    """update: 기존 키가 있을 때만(require_exists) 값 교체."""
    parent, last = _get_parent(doc, path, create=not require_exists)
    if parent is None:
        return False
    if require_exists and last not in parent:
        return False
    parent[last] = value
    return True


def _add_by_path(doc: dict, path: str, value) -> bool:
    """add: 대상이 list 면 append, 없는 키면 신규 설정. 이미 스칼라면 거부."""
    parent, last = _get_parent(doc, path, create=True)
    if parent is None:
        return False
    if last in parent and isinstance(parent[last], list):
        parent[last].append(value)
        return True
    if last not in parent:
        parent[last] = value
        return True
    return False


# 명시적 종료 신호 — 단순 부재(출처에 안 보임)와 실제 폐지를 구분 (C18)
TERMINATION_MARKERS = ["폐지", "종료", "미시행", "시행 종료", "지원 종료", "중단", "사업 종료"]


def _has_termination_evidence(evidence: str) -> bool:
    """evidence 본문에 명시적 종료 문구가 있는지."""
    text = evidence or ""
    return any(m in text for m in TERMINATION_MARKERS)


def _apply_patch(existing: dict, patches: list):
    """패치를 기존 문서에 적용한다. add/update 만 자동 적용하고, 패치에 명시되지
    않은 필드는 절대 변경하지 않는다(미변경 필드 불변 보장). delete 는 적용하지
    않고 검토 항목으로 분리한다.
    반환: (new_doc, applied, review)"""
    import copy
    new_doc = copy.deepcopy(existing)
    applied, review = [], []
    for op in (patches or []):
        kind = op.get("op")
        path = op.get("path", "")
        if kind == "update":
            if _set_by_path(new_doc, path, op.get("new"), require_exists=True):
                applied.append({"op": "update", "path": path})
            else:
                review.append({"reason": "update 경로 없음", "path": path, "op": op})
        elif kind == "add":
            if _add_by_path(new_doc, path, op.get("value")):
                applied.append({"op": "add", "path": path})
            else:
                review.append({"reason": "add 실패(이미 스칼라 존재 등)", "path": path, "op": op})
        elif kind == "delete":
            ev = op.get("evidence", "")
            cls = "delete_candidate" if _has_termination_evidence(ev) else "review_needed"
            review.append({"reason": "delete 자동 적용 금지", "path": path,
                           "classification": cls,
                           "evidence": ev,
                           "confidence": op.get("confidence", "low")})
        else:
            review.append({"reason": "알 수 없는 op", "op": op})
    return new_doc, applied, review


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
        patch_obj = json.loads(raw)
        patches = patch_obj.get("patches", []) if isinstance(patch_obj, dict) else []
    except Exception as e:
        logger.error("LLM 응답이 유효한 패치 JSON 이 아님: %s", e)
        debug = staging_dir / f"{existing['id']}_FAILED_{date.today()}.txt"
        debug.write_text(raw, encoding="utf-8")
        raise RuntimeError(f"LLM 응답 패치 파싱 실패 — {debug}")

    # 패치 적용 — 미변경 필드는 그대로 두고, delete 는 검토로 분리
    new_data, applied, review = _apply_patch(existing, patches)

    # last_verified, version 자동 갱신
    new_data["last_verified"] = date.today().isoformat()
    if existing.get("version") and new_data.get("version") == existing.get("version"):
        new_data["version"] = _bump_version(existing["version"])

    # schema 검증 (패치 적용 결과)
    if schema_path.exists():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft7Validator(schema)
        errs = list(validator.iter_errors(new_data))
        if errs:
            err_msgs = [f"{list(e.path)}: {e.message[:100]}" for e in errs[:5]]
            raise RuntimeError(f"schema 검증 실패 ({len(errs)}건): " + " / ".join(err_msgs))

    # staging 저장 + 검토 리포트(있으면)
    staging_dir.mkdir(parents=True, exist_ok=True)
    fname = item_path.name.replace(".json", f".{date.today().isoformat()}.staged.json")
    staged_path = staging_dir / fname
    staged_path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
    if review:
        review_path = staging_dir / fname.replace(".staged.json", ".review.json")
        review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("검토 필요 항목 %d건: %s", len(review), review_path)
    # confirm 반영 성공 시 baseline 전진에 쓸 출처 메타 사이드카 (#27 A안)
    sources_meta = [
        {"target_id": c.get("target_id"), "method": c.get("method"),
         "new_hash": c.get("new_hash"), "snapshot_dir": c.get("snapshot_dir")}
        for c in related_changes
    ]
    (staging_dir / fname.replace(".staged.json", ".sources.json")).write_text(
        json.dumps(sources_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("staging 저장: %s (적용 %d / 검토 %d)", staged_path, len(applied), len(review))

    diff = _diff_summary(existing, new_data)
    return staged_path, diff
