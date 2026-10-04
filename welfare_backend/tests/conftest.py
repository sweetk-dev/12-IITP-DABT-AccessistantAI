# -*- coding: utf-8 -*-
"""pytest 공통 설정 — `python3 -m pytest welfare_backend/tests` 로 전체를 한 번에 돌리기 위한 준비.

이 폴더의 테스트는 두 형식이 섞여 있다.
  - pytest 형식: `test_*` 함수
  - 스크립트 형식: `t_*` 함수를 정의하고 `python3 tests/test_x.py` 로 직접 실행
    (맨 아래 `if __name__ == "__main__":` 실행부가 PASS/FAIL 을 출력)
두 형식을 그대로 두고 pytest 가 둘 다 수집·실행하게 한다. 스크립트 실행 방식은 바뀌지 않는다.
"""
import importlib
import os
import sys
from pathlib import Path

# 테스트 대상 모듈(welfare_backend/*.py)을 import 할 수 있게 한다.
# 스크립트 형식 테스트는 각자 sys.path 를 고치지만 pytest 형식 일부는 고치지 않는다.
_APP = Path(__file__).resolve().parents[1]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

# 경로 기능 플래그는 route_client 가 import 될 때 한 번 읽는다. 스크립트 형식 테스트는 각자
# import 전에 아래 값을 넣지만, 한 프로세스에서는 먼저 수집된 파일이 route_client 를 플래그
# 없이 올려 버릴 수 있다. 어떤 테스트 모듈보다 먼저 같은 기본값을 넣어 둔다.
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")
os.environ.setdefault("FEATURE_TOUR", "1")

# 스크립트 형식 테스트는 DB 계층(sqlalchemy·database·models)이 sys.modules 에 없을 때만
# 최소 스텁을 끼워 넣는다. 한 프로세스에서 전체를 돌리면 그 스텁이 남아, 진짜 모듈이
# 필요한 테스트(미답변 분류 등)가 수집 순서에 따라 import 에 실패한다. 진짜 모듈을 쓸 수
# 있는 환경이면 먼저 올려 두어 스텁이 끼어들지 않게 한다. 의존성이 없는 환경에서는 여기서
# 실패해도 그냥 넘어가고, 진짜 모듈이 필요한 테스트는 pytest.importorskip 으로 건너뛴다.
for _name in ("sqlalchemy", "sqlalchemy.ext.asyncio", "database", "models"):
    try:
        importlib.import_module(_name)
    except Exception:       # noqa: BLE001 — 의존성 없음·설정 없음 모두 같은 처리
        break


def pytest_configure(config):
    # 스크립트 형식의 `t_*` 함수도 테스트로 수집한다(인자 없는 동기 함수들이다).
    # 설정 파일 대신 여기서 넣는 이유: 어느 디렉터리에서 pytest 를 실행해도 적용되게 하려고.
    config.addinivalue_line("python_functions", "t_*")
