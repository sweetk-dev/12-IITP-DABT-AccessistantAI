# -*- coding: utf-8 -*-
"""접근성 제보 사진의 입력 검증과 응답 형식 판정 (v2.24.0).

제보 사진은 두 번 이 서비스를 지난다.
  1) 들어올 때 — 이동경로 안내 화면 → POST /api/v1/nav/report → 경로 서비스 저장
  2) 나갈 때  — 경로 서비스 저장본 → 운영 콘솔 GET /admin/api/nav-reports/{id}/photo

두 지점 모두에서 "이 바이트가 정말 허용한 이미지인가" 를 바이트 자체(파일 시그니처)로 판정한다.
요청이 적어 보낸 형식 값(photo_mime)이나 경로 서비스가 돌려준 Content-Type 은 믿지 않는다.
형식 값은 아무 문자열이나 넣을 수 있고, 그 값이 그대로 응답 Content-Type 이 되면 브라우저가
사진이 아닌 문서(HTML 등)로 해석할 수 있기 때문이다. 이미지로 판정되지 않은 바이트는 콘솔
오리진에서 문서로 열리지 않게 내려받기로만 내보낸다(admin_router 의 사진 응답).

허용 형식의 근거
  - image/jpeg : 이동경로 안내 화면(static/accessistant.html pickReportPhoto)은 고른 사진을
                 캔버스로 1280px 이하로 줄인 뒤 toDataURL("image/jpeg", 0.8) 로만 보낸다.
                 안드로이드 앱은 같은 화면을 WebView 로 띄우므로 경로가 같다.
  - image/png, image/webp : 화면이 지금 보내지는 않지만, 캔버스가 만들 수 있는 나머지 표준
                 형식이라 화면 변경(무손실 저장 등)에 대비해 열어 둔다.
  그 밖(GIF·SVG·HEIC 등)은 받지 않는다. 특히 SVG 는 스크립트를 품을 수 있는 문서 형식이다.

크기 상한 2MB 는 경로 서비스 저장 계층의 상한(MAX_PHOTO_BYTES, 2MB)과 같다. 여기서 먼저
거절해 큰 본문이 경로 서비스까지 가지 않게 하고, 거절 문구를 화면이 그대로 보여 줄 수 있게 한다.
1280px·품질 0.8 JPEG 는 보통 수백 KB 라 정상 제보는 상한에 닿지 않는다.
"""
import base64
import binascii
import re
from typing import Optional, Tuple

# 허용 형식 — 바깥(admin.html)에서도 같은 목록을 쓴다. 바꿀 때 함께 바꾼다.
ALLOWED_MIMES = ("image/jpeg", "image/png", "image/webp")

# 확장자 — 내려받기 파일 이름용
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

# 디코드 후 바이트 상한 (경로 서비스 저장 상한과 같다)
MAX_PHOTO_BYTES = 2 * 1024 * 1024

# base64 문자열 길이 상한 — 디코드 전에 먼저 자른다(큰 문자열을 디코드하느라 메모리를 쓰지 않게).
# 2MB 를 base64 로 바꾸면 4/3 배(약 2.67MB). data URL 접두·줄바꿈 여유로 64KB 를 더 준다.
MAX_PHOTO_B64_CHARS = (MAX_PHOTO_BYTES + 2) // 3 * 4 + 64 * 1024

# 형식 값의 별칭 — 일부 브라우저·라이브러리가 쓰는 비표준 표기를 표준으로 맞춘다.
_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}

# 브라우저 FileReader.readAsDataURL 결과 앞머리: "data:image/jpeg;base64,"
_DATA_URL_RE = re.compile(r"^data:([^;,]*)(?:;[^;,]*)*;base64,", re.IGNORECASE)


class PhotoRejected(ValueError):
    """제보 사진을 받지 않을 때. 메시지는 이용자 화면에 그대로 보여 줄 수 있는 문장이다."""


def sniff_image_mime(data: bytes) -> Optional[str]:
    """바이트 앞머리(파일 시그니처)로 허용 이미지 형식을 판정한다. 허용 형식이 아니면 None.

    - JPEG : FF D8 FF  (SOI 표지 + 다음 표지의 시작 바이트)
    - PNG  : 89 50 4E 47 0D 0A 1A 0A  (8바이트 고정 서명)
    - WebP : "RIFF" <크기 4바이트> "WEBP"  (RIFF 컨테이너 중 WEBP 형식만 — WAV·AVI 는 제외)
    """
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def normalize_mime(raw) -> Optional[str]:
    """형식 값을 비교 가능한 꼴로 — 소문자, 매개변수(;charset=…) 제거, 별칭 정리. 비었으면 None."""
    if raw is None:
        return None
    s = str(raw).split(";", 1)[0].strip().lower()
    if not s:
        return None
    return _MIME_ALIASES.get(s, s)


def extension_for(mime: str) -> str:
    return _EXT.get(mime, "bin")


def validate_report_photo(photo_b64, photo_mime) -> Tuple[Optional[str], Optional[str]]:
    """제보 사진을 검증해 경로 서비스로 넘길 (base64, 형식) 을 돌려준다. 사진이 없으면 (None, None).

    거절 조건(PhotoRejected):
      - 문자열이 아니거나, 길이가 상한을 넘거나, base64 알파벳 밖 글자가 섞였다
      - 디코드한 바이트가 2MB 를 넘는다
      - 파일 시그니처가 허용 형식(JPEG·PNG·WebP)이 아니다
      - 선언한 형식(photo_mime 또는 data URL 접두의 형식)이 허용 목록 밖이거나 시그니처와 다르다
    통과하면 형식은 선언값이 아니라 시그니처로 판정한 값을, base64 는 접두·공백을 뗀 정규형을 넘긴다
    (경로 서비스가 저장하는 형식 값이 늘 허용 목록 안의 값이 되게 한다).
    """
    if photo_b64 is None or photo_b64 == "":
        return None, None                    # 사진 없는 제보 — 형식 값도 버린다
    if not isinstance(photo_b64, str):
        raise PhotoRejected("사진 데이터 형식이 올바르지 않습니다")
    if len(photo_b64) > MAX_PHOTO_B64_CHARS:
        raise PhotoRejected("사진이 너무 큽니다 (2MB 이하)")

    s = photo_b64.strip()
    declared = normalize_mime(photo_mime)
    m = _DATA_URL_RE.match(s)
    if m:
        url_mime = normalize_mime(m.group(1))
        if declared and url_mime and declared != url_mime:
            raise PhotoRejected("사진 형식 정보가 서로 다릅니다")
        declared = declared or url_mime
        s = s[m.end():]
    s = "".join(s.split())                   # 76자 줄바꿈 base64 도 받는다
    try:
        data = base64.b64decode(s, validate=True)   # 알파벳 밖 글자는 버리지 않고 오류로 본다
    except (binascii.Error, ValueError):
        raise PhotoRejected("사진 데이터가 올바른 base64 가 아닙니다")
    if not data:
        return None, None
    if len(data) > MAX_PHOTO_BYTES:
        raise PhotoRejected("사진이 너무 큽니다 (2MB 이하)")

    actual = sniff_image_mime(data)
    if actual is None:
        raise PhotoRejected("JPEG·PNG·WebP 사진만 첨부할 수 있습니다")
    if declared is not None:
        if declared not in ALLOWED_MIMES:
            raise PhotoRejected("JPEG·PNG·WebP 사진만 첨부할 수 있습니다")
        if declared != actual:
            raise PhotoRejected("사진 형식 정보와 실제 파일이 다릅니다")
    return base64.b64encode(data).decode("ascii"), actual


# 운영 콘솔 사진 응답에 늘 붙이는 헤더.
#  - nosniff : 브라우저가 바이트를 보고 형식을 추측(문서로 승격)하지 않게 한다
#  - CSP     : 이 응답을 문서로 열더라도 스크립트·외부 자원을 못 쓰고(default-src 'none'),
#              sandbox 로 고유 출처에 격리되어 콘솔 오리진의 API 를 부를 수 없다.
#              img-src 'self' data: 는 브라우저가 이미지를 단독으로 열 때 쓰는 자체 보기 화면용이다.
PHOTO_RESPONSE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; sandbox",
}


def photo_response_parts(data: bytes, report_id: int) -> Tuple[str, dict]:
    """콘솔 사진 응답의 (Content-Type, 헤더) — 경로 서비스가 준 형식 값은 쓰지 않는다.

    시그니처가 허용 이미지면 그 형식으로 inline, 아니면 application/octet-stream + attachment
    (브라우저가 화면에 열지 않고 파일로만 내려받는다).
    """
    headers = dict(PHOTO_RESPONSE_HEADERS)
    mime = sniff_image_mime(data)
    rid = int(report_id)
    if mime in ALLOWED_MIMES:
        headers["Content-Disposition"] = 'inline; filename="report-%d.%s"' % (rid, extension_for(mime))
        return mime, headers
    headers["Content-Disposition"] = 'attachment; filename="report-%d.bin"' % rid
    return "application/octet-stream", headers
