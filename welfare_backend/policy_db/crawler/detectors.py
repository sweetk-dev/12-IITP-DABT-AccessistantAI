# crawler/detectors.py
# 5종 변경 감지 메서드 — crawl_targets.json 의 change_detection_method 와 1:1 대응.
#
# 호출 시그니처 통일:
#   async def detect_xxx(target: dict, snapshot_path: Path) -> ChangeResult
#
# ChangeResult 는 namedtuple:
#   .changed (bool)        — 변경 감지 여부
#   .reason  (str)         — 사람이 읽을 수 있는 설명
#   .new_content (bytes|None) — 새 본문 (변경 시) — staging 저장용
#   .new_hash (str|None)   — 비교 키 (다음 회차에 비교 기준이 됨)
import difflib
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import os
import httpx

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": f"DisabilityPolicyDB-Crawler/1.0 (+contact: {os.environ.get('CRAWLER_CONTACT', 'contact@example.com')})",
    "Accept-Language": "ko,en;q=0.7",
}
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass
class ChangeResult:
    changed: bool
    reason: str
    new_content: Optional[bytes] = None
    new_hash: Optional[str] = None
    chunk_diff: Optional[dict] = None
    new_chunks: Optional[list] = None
    url_used: Optional[str] = None      # 실제 성공한 URL (기본 또는 fallback)
    used_fallback: bool = False         # fallback_url 로 성공 → 기본 URL 점검 필요 신호


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── 스냅샷 파일 매핑 + 공통 비교 헬퍼 ─────────────────────────
# 변경 감지 방식 → 스냅샷 파일명. 읽기(_read_prev_hash)와 쓰기(save_snapshot)가
# 동일 매핑을 공유하므로 "저장=해시 / 비교=원문" 같은 불일치가 재발하지 않는다.
SNAPSHOT_FILES = {
    "page_hash": "page_hash.txt",
    "pdf_hash": "pdf_hash.txt",
    "last_modified_field": "last_modified.txt",
    "css_selector_text": "selector.txt",
}


def _read_prev_hash(snapshot_dir: Path, method: str) -> Optional[str]:
    """직전 회차에 저장된 비교 해시를 읽는다 (없으면 None = 최초 스냅샷)."""
    fname = SNAPSHOT_FILES.get(method)
    if fname is None:
        return None
    f = snapshot_dir / fname
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


def _mask_dynamic_noise(text: str, mask_dates: bool = True) -> str:
    """동적 노이즈를 placeholder 로 치환한다 (조회수·세션·토큰 등).

    mask_dates=False 면 날짜·시각은 보존한다 — last_modified_field 처럼 날짜 자체가
    비교 키인 경우에 사용한다."""
    patterns = [
        (r"(?:조회\s*수?|조회|view(?:s)?|hit(?:s)?)\s*[:：]?\s*[\d,]+", "VIEWCOUNT"),
        (r"오늘\s*[\d,]+\s*명?", "TODAYCOUNT"),
        (r'(name="[^"]*(?:token|csrf|session)[^"]*"\s+value=")[^"]*(")', r"\1MASKED\2"),
        (r"(?:JSESSIONID|PHPSESSID|csrf[-_]?token)=[A-Za-z0-9]+", "SESSION"),
    ]
    if mask_dates:
        patterns = [
            (r"\d{4}[-./]\d{2}[-./]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", "DATE"),
            (r"\d{2}:\d{2}:\d{2}", "TIME"),
        ] + patterns
    for pat, repl in patterns:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    return text


# ── HTML 파싱 공통 헬퍼 (#25: page_hash 이중 파싱 제거 — soup 1회 재사용) ──
_NOISE_TAGS = ["script", "style", "noscript", "nav", "header", "footer",
               "aside", "form", "iframe", "svg", "button", "input"]


def _decode_html(html: bytes) -> str:
    try:
        return html.decode("utf-8", errors="replace")
    except Exception:
        return html.decode("latin-1", errors="replace")


def _soup_clean(raw: str):
    """BeautifulSoup 파싱 후 비콘텐츠 태그 제거한 soup 반환 (bs4 미설치/실패 시 None)."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None
    try:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(_NOISE_TAGS):
            tag.decompose()
        return soup
    except Exception:
        return None


def _finalize_text(text: str, mask_dates: bool) -> str:
    text = _mask_dynamic_noise(text, mask_dates=mask_dates)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", text).strip()


def _chunks_from_soup(soup) -> list:
    chunks = []
    for el in soup.find_all(BLOCK_TAGS):
        t = re.sub(r"\s+", " ", _mask_dynamic_noise(el.get_text(separator=" "))).strip()
        if len(t) >= 10:
            chunks.append(t)
    return chunks


def _sentence_chunks(flat: str) -> list:
    chunks = []
    for seg in re.split(r"(?<=[.?。])\s+|[\r\n]+", flat):
        seg = seg.strip()
        if len(seg) >= 10:
            chunks.append(seg)
    return chunks


def _normalize_html_text(html: bytes, mask_dates: bool = True) -> str:
    """HTML 바이트 → 정규화된 본문 텍스트.

    BeautifulSoup 가 있으면 script/nav/header/footer 등 비콘텐츠 태그를 제거해
    본문만 남기고, 없으면 정규식 폴백으로 동작한다. 이후 _mask_dynamic_noise 로
    동적 노이즈를 마스킹해 '본문은 그대로인데 노이즈만 바뀐' 경우의 거짓 변경을 막는다.
    """
    raw = _decode_html(html)
    soup = _soup_clean(raw)
    if soup is not None:
        return _finalize_text(soup.get_text(separator=" "), mask_dates)
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return _finalize_text(text, mask_dates)


# ── 의미 단위 청킹 (C8) ──────────────────────────────────────
BLOCK_TAGS = ["p", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
              "dt", "dd", "blockquote", "figcaption", "caption"]


def _chunk_html(html: bytes) -> list:
    """HTML 을 의미 단위(문단/리스트/표 행/제목) 청크 리스트로 변환한다.
    각 청크는 _mask_dynamic_noise 로 노이즈가 마스킹된 정규화 텍스트.
    bs4 미설치 시 정규화 전체 텍스트를 문장 단위로 분할(폴백)."""
    soup = _soup_clean(_decode_html(html))
    chunks = _chunks_from_soup(soup) if soup is not None else []
    if not chunks:
        chunks = _sentence_chunks(_normalize_html_text(html))
    return chunks


def _parse_page_hash(html: bytes):
    """page_hash 전용 — soup 를 1회만 파싱해 정규화 텍스트와 청크를 함께 산출 (#25).
    detect_page_hash 가 _normalize_html_text + _chunk_html 로 같은 HTML 을 두 번
    파싱하던 비용을 제거. 산출 텍스트/청크는 두 함수의 결과와 동일하다."""
    raw = _decode_html(html)
    soup = _soup_clean(raw)
    if soup is None:
        text = _normalize_html_text(html)
        return text, _sentence_chunks(text)
    text = _finalize_text(soup.get_text(separator=" "), True)
    chunks = _chunks_from_soup(soup) or _sentence_chunks(text)
    return text, chunks


def _chunk_diff(old_chunks, new_chunks, sim_threshold: float = 0.6):
    """청크 집합 비교 (C11: 유사도 기반 changed 분류 포함).
    정확 일치 외의 added/removed 쌍 중 유사도 >= sim_threshold 인 것을
    changed(수정)로 묶어 add/remove 과대계상을 방지한다."""
    old_list = list(old_chunks or [])
    new_list = list(new_chunks or [])
    old_set = set(old_list)
    new_set = set(new_list)
    raw_added = [c for c in new_list if c not in old_set]
    raw_removed = [c for c in old_list if c not in new_set]
    unchanged = len(old_set & new_set)

    changed = []
    remaining_removed = list(raw_removed)
    still_added = []
    for a in raw_added:
        best, best_ratio = None, 0.0
        for r in remaining_removed:
            ratio = difflib.SequenceMatcher(None, r, a).ratio()
            if ratio > best_ratio:
                best, best_ratio = r, ratio
        if best is not None and best_ratio >= sim_threshold:
            changed.append({"before": best, "after": a, "ratio": round(best_ratio, 3)})
            remaining_removed.remove(best)
        else:
            still_added.append(a)
    return {"added": still_added, "removed": remaining_removed,
            "changed": changed, "unchanged": unchanged}


def _read_prev_chunks(snapshot_dir: Path) -> list:
    """직전 회차 청크 스냅샷(chunks.json) 로드 (없으면 빈 리스트)."""
    f = snapshot_dir / "chunks.json"
    if not f.exists():
        return []
    try:
        import json as _json
        return _json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_chunks(snapshot_dir: Path, chunks: list) -> None:
    """이번 회차 청크를 chunks.json 에 저장 (다음 회차 비교 기준)."""
    import json as _json
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "chunks.json").write_text(
        _json.dumps(chunks, ensure_ascii=False), encoding="utf-8")


async def _fetch_url(url: str, *, client: httpx.AsyncClient) -> Optional[httpx.Response]:
    """단일 URL 1회 GET. 실패(예외/HTTP>=400) 시 None."""
    try:
        resp = await client.get(url, headers=DEFAULT_HEADERS, follow_redirects=True, timeout=TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("HTTP %s for %s", resp.status_code, url)
            return None
        return resp
    except Exception as e:
        logger.warning("fetch 실패 %s: %s", url, e)
        return None


async def _fetch(target: dict, *, client: httpx.AsyncClient):
    """기본 url 실패 시 crawl_targets 의 fallback_url 로 자동 재시도.

    반환: (resp, info). info = {url_used, used_fallback, primary_failed}.
    - 기본 URL 성공: used_fallback=False
    - 기본 실패 → fallback 성공: used_fallback=True (=기본 URL 점검 필요 신호)
    - 둘 다 실패: resp=None (호출자가 _fetch_failed_reason 으로 사유 구성)
    official_api 는 포맷이 출처마다 달라 자동 호출하지 않고, 전부 실패 시 사유에 표기해
    수동 대체를 유도한다(향후 확장 지점)."""
    info = {"url_used": None, "used_fallback": False, "primary_failed": False}
    primary = target.get("url")
    fallback = target.get("fallback_url")
    for label, u in (("primary", primary), ("fallback", fallback)):
        if not u:
            continue
        resp = await _fetch_url(u, client=client)
        if resp is not None:
            info["url_used"] = u
            info["used_fallback"] = (label == "fallback")
            if info["used_fallback"]:
                logger.warning("⚠ 기본 URL 실패 → fallback_url 로 수집: %s (기본=%s)", u, primary)
            return resp, info
        if label == "primary":
            info["primary_failed"] = True
            if fallback:
                logger.info("기본 URL 실패 → fallback_url 시도: %s", fallback)
    return None, info


def _fetch_failed_reason(target: dict, info: dict) -> str:
    """수집 실패 사유 문자열. 'fetch_failed' 접두어는 유지(크롤러 실패 분류 호환)."""
    parts = ["fetch_failed"]
    if target.get("fallback_url"):
        parts.append("fallback_also_failed")
    if target.get("official_api"):
        parts.append("has_official_api(수동 대체 검토)")
    return " | ".join(parts)


def _annotate(res: "ChangeResult", info: dict) -> "ChangeResult":
    """수집에 쓰인 URL/폴백 여부를 결과에 부착. 폴백 사용 시 사유에 점검 신호 추가."""
    res.url_used = info.get("url_used")
    res.used_fallback = bool(info.get("used_fallback"))
    if res.used_fallback and "fetch_failed" not in res.reason:
        res.reason = f"{res.reason} (fallback_url 사용 — 기본 URL 점검 필요)"
    return res


# ─────────────────────────────────────────────────────────────
# 1) page_hash — HTML 본문 텍스트 SHA-256 비교
# ─────────────────────────────────────────────────────────────
async def detect_page_hash(target: dict, snapshot_dir: Path, *, client: httpx.AsyncClient, revalidate: bool = False) -> ChangeResult:
    resp, finfo = await _fetch(target, client=client)
    if resp is None:
        return ChangeResult(False, _fetch_failed_reason(target, finfo))
    # C6/#25: soup 1회 파싱으로 정규화 텍스트 + 청크 동시 산출 (이중 파싱 제거)
    text, new_chunks = _parse_page_hash(resp.content)
    new_hash = _hash_bytes(text.encode("utf-8"))

    # C10: 청크 단위 비교 — 어디가 바뀌었는지 진단(추가/삭제/수정)
    prev_chunks = _read_prev_chunks(snapshot_dir)
    cdiff = _chunk_diff(prev_chunks, new_chunks) if prev_chunks else None

    prev_hash = _read_prev_hash(snapshot_dir, "page_hash")
    changed = (prev_hash is None) or (prev_hash != new_hash)
    reason = "최초 스냅샷" if prev_hash is None else ("해시 변경" if changed else "변경 없음")
    return _annotate(ChangeResult(
        changed=changed,
        reason=reason,
        new_content=resp.content if (changed or revalidate) else None,
        new_hash=new_hash,
        chunk_diff=cdiff,
        new_chunks=new_chunks,
    ), finfo)


# ─────────────────────────────────────────────────────────────
# 2) pdf_hash — PDF 바이트 SHA-256
# ─────────────────────────────────────────────────────────────
async def detect_pdf_hash(target: dict, snapshot_dir: Path, *, client: httpx.AsyncClient, revalidate: bool = False) -> ChangeResult:
    resp, finfo = await _fetch(target, client=client)
    if resp is None:
        return ChangeResult(False, _fetch_failed_reason(target, finfo))
    new_hash = _hash_bytes(resp.content)
    prev_hash = _read_prev_hash(snapshot_dir, "pdf_hash")
    changed = (prev_hash is None) or (prev_hash != new_hash)
    # PDF 파일명 날짜 패턴 변화도 함께 모니터링 (예: _250623 → _260101)
    return _annotate(ChangeResult(
        changed=changed,
        reason="최초 스냅샷" if prev_hash is None else ("PDF 해시 변경" if changed else "변경 없음"),
        new_content=resp.content if (changed or revalidate) else None,
        new_hash=new_hash,
    ), finfo)


# ─────────────────────────────────────────────────────────────
# 3) last_modified_field — 페이지/API 의 "수정일/시행일" 텍스트 비교
# ─────────────────────────────────────────────────────────────
LAST_MOD_PATTERNS = [
    r"수정일[\s:]*([0-9]{4}[-./][0-9]{2}[-./][0-9]{2})",
    r"시행일[\s:]*([0-9]{4}[-./][0-9]{2}[-./][0-9]{2})",
    r"공포일[\s:]*([0-9]{4}[-./][0-9]{2}[-./][0-9]{2})",
    r"최종\s*갱신[\s:]*([0-9]{4}[-./][0-9]{2}[-./][0-9]{2})",
    r"Last[- ]Modified[\s:]*([A-Za-z0-9, :+]+GMT)?",
]


async def detect_last_modified_field(target: dict, snapshot_dir: Path, *, client: httpx.AsyncClient, revalidate: bool = False) -> ChangeResult:
    resp, finfo = await _fetch(target, client=client)
    if resp is None:
        return ChangeResult(False, _fetch_failed_reason(target, finfo))
    text = _normalize_html_text(resp.content, mask_dates=False)
    # HTTP Last-Modified 헤더도 함께 본다
    http_lm = resp.headers.get("Last-Modified", "")
    body_lm = None
    for pat in LAST_MOD_PATTERNS:
        m = re.search(pat, text)
        if m:
            body_lm = m.group(0)
            break
    new_key = f"{http_lm}|{body_lm or ''}"
    new_hash = _hash_bytes(new_key.encode("utf-8"))
    # 저장/비교 모두 해시 기준 — SNAPSHOT_FILES 매핑 공유로 불일치 재발 방지.
    prev_hash = _read_prev_hash(snapshot_dir, "last_modified_field")
    changed = (prev_hash is None) or (prev_hash != new_hash)
    return _annotate(ChangeResult(
        changed=changed,
        reason="최초 스냅샷" if prev_hash is None else ("최종 수정 키 변경" if changed else "변경 없음"),
        new_content=resp.content if (changed or revalidate) else None,
        new_hash=new_hash,
    ), finfo)


# ─────────────────────────────────────────────────────────────
# 4) css_selector_text — 지정된 패턴 텍스트만 비교
#    crawl_targets 의 css_selector_hint 가 "re:<정규식>" 형태면 그 정규식 매칭 결과를,
#    그 외(빈 값/설명/셀렉터)면 전체 본문(앞 5KB) 해시를 비교 키로 쓴다.
#    (CSS 셀렉터 직접 지원은 의존성 최소화를 위해 향후 확장)
# ─────────────────────────────────────────────────────────────
async def detect_css_selector_text(target: dict, snapshot_dir: Path, *, client: httpx.AsyncClient, revalidate: bool = False) -> ChangeResult:
    hint = (target.get("css_selector_hint") or "").strip()
    resp, finfo = await _fetch(target, client=client)
    if resp is None:
        return ChangeResult(False, _fetch_failed_reason(target, finfo))
    text = _normalize_html_text(resp.content)

    # hint 가 "re:" 프리픽스면 정규식으로 해석, 그 외(빈 값/셀렉터 설명 등)는
    # 침묵 오매칭을 피하기 위해 전체 본문 해시로 폴백한다. (셀렉터 직접 지원은 향후 확장)
    extracted = None
    if hint.startswith("re:"):
        pattern = hint[3:].strip()
        try:
            m = re.search(pattern, text)
            if m:
                extracted = m.group(0)
        except re.error:
            logger.warning("css_selector_text 정규식 오류 — 전체 텍스트 폴백: %s", pattern)

    target_text = extracted if extracted else text[:5000]  # 큰 본문은 앞 5KB
    new_hash = _hash_bytes(target_text.encode("utf-8"))
    prev_hash = _read_prev_hash(snapshot_dir, "css_selector_text")
    changed = (prev_hash is None) or (prev_hash != new_hash)
    return _annotate(ChangeResult(
        changed=changed,
        reason="셀렉터 텍스트 변경" if changed and prev_hash else (
            "최초 스냅샷" if prev_hash is None else "변경 없음"
        ),
        new_content=resp.content if (changed or revalidate) else None,
        new_hash=new_hash,
    ), finfo)


# ─────────────────────────────────────────────────────────────
# 5) manual_review — 자동 감지 불가, 보고만
# ─────────────────────────────────────────────────────────────
async def detect_manual_review(target: dict, snapshot_dir: Path, *, client: httpx.AsyncClient, revalidate: bool = False) -> ChangeResult:
    return ChangeResult(
        changed=False,
        reason="manual_review — 분기 1회 수동 검토 권장",
    )


# ─────────────────────────────────────────────────────────────
# 디스패처
# ─────────────────────────────────────────────────────────────
DETECTORS = {
    "page_hash": detect_page_hash,
    "pdf_hash": detect_pdf_hash,
    "last_modified_field": detect_last_modified_field,
    "css_selector_text": detect_css_selector_text,
    "manual_review": detect_manual_review,
}


# ── 스냅샷 저장 — 감지/확정 2단계 분리 (#27 A안) ──────────────
# 감지 시점에는 본문(latest.*)·pending 청크만 저장하고, 비교 baseline(해시 + chunks.json)은
# confirm_apply 반영 성공 시 save_baseline_snapshot 로 전진시킨다. 따라서 리포트를 놓치거나
# LLM/staging 이 실패하면 baseline 이 그대로 남아 다음 회차에 변경이 재노출된다.
def save_content_snapshot(snapshot_dir: Path, method: str, result: ChangeResult):
    """변경 본문만 저장 (llm_updater LLM 입력·진단용). baseline 은 건드리지 않는다."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    if result.new_content is not None:
        ext = "pdf" if method == "pdf_hash" else "html"
        (snapshot_dir / f"latest.{ext}").write_bytes(result.new_content)
    # page_hash 청크는 pending 으로만 저장 — confirm 시 chunks.json 으로 승격.
    if method == "page_hash" and result.new_chunks is not None:
        (snapshot_dir / "pending_chunks.json").write_text(
            json.dumps(result.new_chunks, ensure_ascii=False), encoding="utf-8")


def save_baseline_snapshot(snapshot_dir: Path, method: str, new_hash) -> bool:
    """비교 baseline(해시 + page_hash chunks.json)을 전진. confirm_apply 반영 성공 시 호출.
    반환: 전진 여부(스냅샷/해시 없으면 False)."""
    fname = SNAPSHOT_FILES.get(method)
    if fname is None or new_hash is None:
        return False
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / fname).write_text(new_hash, encoding="utf-8")
    # pending 청크가 있으면 chunks.json 으로 승격 (다음 회차 청크 diff 기준).
    if method == "page_hash":
        pend = snapshot_dir / "pending_chunks.json"
        if pend.exists():
            pend.replace(snapshot_dir / "chunks.json")
    return True


def save_snapshot(snapshot_dir: Path, method: str, result: ChangeResult):
    """[호환] 본문 + baseline 을 한 번에 저장 (수동 도구·테스트용).
    정기 크롤러는 #27 A안에 따라 save_content_snapshot(감지) + save_baseline_snapshot(confirm)
    로 분리 호출한다."""
    save_content_snapshot(snapshot_dir, method, result)
    save_baseline_snapshot(snapshot_dir, method, result.new_hash)
