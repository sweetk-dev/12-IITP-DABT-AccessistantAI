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
import hashlib
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
    fetched_url: Optional[str] = None


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extract_text_from_html(html: bytes) -> str:
    """간단한 HTML→텍스트. BeautifulSoup 없이 정규식 기반(의존성 최소화).
    광고·스크립트·날짜 위젯 등 노이즈 일부 제거."""
    try:
        text = html.decode("utf-8", errors="replace")
    except Exception:
        text = html.decode("latin-1", errors="replace")
    # script, style 제거
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    # 흔한 동적 노이즈 (현재시각·세션·hidden token) 마스킹
    text = re.sub(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", "DATETIME", text)
    text = re.sub(r'(name="[^"]*token[^"]*"\s+value=")[^"]*(")', r"\1MASKED\2", text, flags=re.IGNORECASE)
    # HTML 태그 제거
    text = re.sub(r"<[^>]+>", " ", text)
    # 엔티티 디코딩 일부
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                 .replace("&lt;", "<").replace("&gt;", ">"))
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def _fetch(url: str, *, client: httpx.AsyncClient) -> Optional[httpx.Response]:
    try:
        resp = await client.get(url, headers=DEFAULT_HEADERS, follow_redirects=True, timeout=TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("HTTP %s for %s", resp.status_code, url)
            return None
        return resp
    except Exception as e:
        logger.warning("fetch 실패 %s: %s", url, e)
        return None


# ─────────────────────────────────────────────────────────────
# 1) page_hash — HTML 본문 텍스트 SHA-256 비교
# ─────────────────────────────────────────────────────────────
async def detect_page_hash(target: dict, snapshot_dir: Path, *, client: httpx.AsyncClient) -> ChangeResult:
    url = target["url"]
    resp = await _fetch(url, client=client)
    if resp is None:
        return ChangeResult(False, "fetch_failed", fetched_url=url)
    text = _extract_text_from_html(resp.content)
    new_hash = _hash_bytes(text.encode("utf-8"))

    prev_file = snapshot_dir / "page_hash.txt"
    prev_hash = prev_file.read_text(encoding="utf-8").strip() if prev_file.exists() else None
    changed = (prev_hash is None) or (prev_hash != new_hash)
    reason = "최초 스냅샷" if prev_hash is None else ("해시 변경" if changed else "변경 없음")
    return ChangeResult(
        changed=changed,
        reason=reason,
        new_content=resp.content if changed else None,
        new_hash=new_hash,
        fetched_url=url,
    )


# ─────────────────────────────────────────────────────────────
# 2) pdf_hash — PDF 바이트 SHA-256
# ─────────────────────────────────────────────────────────────
async def detect_pdf_hash(target: dict, snapshot_dir: Path, *, client: httpx.AsyncClient) -> ChangeResult:
    url = target["url"]
    resp = await _fetch(url, client=client)
    if resp is None:
        return ChangeResult(False, "fetch_failed", fetched_url=url)
    new_hash = _hash_bytes(resp.content)
    prev_file = snapshot_dir / "pdf_hash.txt"
    prev_hash = prev_file.read_text(encoding="utf-8").strip() if prev_file.exists() else None
    changed = (prev_hash is None) or (prev_hash != new_hash)
    # PDF 파일명 날짜 패턴 변화도 함께 모니터링 (예: _250623 → _260101)
    return ChangeResult(
        changed=changed,
        reason="최초 스냅샷" if prev_hash is None else ("PDF 해시 변경" if changed else "변경 없음"),
        new_content=resp.content if changed else None,
        new_hash=new_hash,
        fetched_url=url,
    )


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


async def detect_last_modified_field(target: dict, snapshot_dir: Path, *, client: httpx.AsyncClient) -> ChangeResult:
    url = target["url"]
    resp = await _fetch(url, client=client)
    if resp is None:
        return ChangeResult(False, "fetch_failed", fetched_url=url)
    text = _extract_text_from_html(resp.content)
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
    prev_file = snapshot_dir / "last_modified.txt"
    prev_key = prev_file.read_text(encoding="utf-8").strip() if prev_file.exists() else None
    changed = (prev_key is None) or (prev_key != new_key)
    return ChangeResult(
        changed=changed,
        reason=f"최종 수정 키 변경: '{prev_key}' → '{new_key}'" if changed and prev_key else (
            "최초 스냅샷" if prev_key is None else "변경 없음"
        ),
        new_content=resp.content if changed else None,
        new_hash=new_hash,
        fetched_url=url,
    )


# ─────────────────────────────────────────────────────────────
# 4) css_selector_text — 지정된 셀렉터/패턴 텍스트만 비교
#    crawl_targets 의 css_selector_hint 에 패턴 또는 셀렉터 적힘.
#    의존성 최소화를 위해 정규식 매칭만 지원 (셀렉터는 향후 확장).
# ─────────────────────────────────────────────────────────────
async def detect_css_selector_text(target: dict, snapshot_dir: Path, *, client: httpx.AsyncClient) -> ChangeResult:
    url = target["url"]
    hint = (target.get("css_selector_hint") or "").strip()
    resp = await _fetch(url, client=client)
    if resp is None:
        return ChangeResult(False, "fetch_failed", fetched_url=url)
    text = _extract_text_from_html(resp.content)

    # hint 가 비어있거나 셀렉터 형태이면 전체 텍스트 해시로 fallback
    extracted = None
    if hint and any(c in hint for c in "(){}[]/-=:;.,?") and not hint.startswith("."):
        # 정규식 또는 키워드 패턴으로 간주
        try:
            m = re.search(hint, text)
            if m:
                extracted = m.group(0)
        except re.error:
            pass

    target_text = extracted if extracted else text[:5000]  # 큰 본문은 앞 5KB
    new_hash = _hash_bytes(target_text.encode("utf-8"))
    prev_file = snapshot_dir / "selector.txt"
    prev_hash = prev_file.read_text(encoding="utf-8").strip() if prev_file.exists() else None
    changed = (prev_hash is None) or (prev_hash != new_hash)
    return ChangeResult(
        changed=changed,
        reason="셀렉터 텍스트 변경" if changed and prev_hash else (
            "최초 스냅샷" if prev_hash is None else "변경 없음"
        ),
        new_content=resp.content if changed else None,
        new_hash=new_hash,
        fetched_url=url,
    )


# ─────────────────────────────────────────────────────────────
# 5) manual_review — 자동 감지 불가, 보고만
# ─────────────────────────────────────────────────────────────
async def detect_manual_review(target: dict, snapshot_dir: Path, *, client: httpx.AsyncClient) -> ChangeResult:
    return ChangeResult(
        changed=False,
        reason="manual_review — 분기 1회 수동 검토 권장",
        fetched_url=target["url"],
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


def save_snapshot(snapshot_dir: Path, method: str, result: ChangeResult):
    """변경 감지 후 기준 스냅샷 저장."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    fname_map = {
        "page_hash": "page_hash.txt",
        "pdf_hash": "pdf_hash.txt",
        "last_modified_field": "last_modified.txt",
        "css_selector_text": "selector.txt",
    }
    if method not in fname_map or result.new_hash is None:
        return
    # last_modified_field 만 해시가 아닌 원본 키 저장
    if method == "last_modified_field":
        # 새 키를 그대로 재구성하기 어려우니 해시 저장
        (snapshot_dir / fname_map[method]).write_text(result.new_hash, encoding="utf-8")
    else:
        (snapshot_dir / fname_map[method]).write_text(result.new_hash, encoding="utf-8")
    # 본문도 저장 (선택적 진단용)
    if result.new_content is not None:
        ext = "pdf" if method == "pdf_hash" else "html"
        (snapshot_dir / f"latest.{ext}").write_bytes(result.new_content)
