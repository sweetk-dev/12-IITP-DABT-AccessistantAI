# ingest_v1.5.py — [DEPRECATED] 부트스트랩 셔틀
# 스키마 생성 + 전량 적재 로직은 ingest_sync.py 로 통합되었습니다(스키마 SoT 단일화).
# 빈 DB 초기 구축:  python ingest_sync.py --rebuild
# 증분 동기화:      python ingest_sync.py
#
# 본 파일은 하위호환을 위해 유지되며, 실행 시 ingest_sync --rebuild 로 위임합니다.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest_sync

if __name__ == "__main__":
    print("[deprecated] ingest_v1.5.py 는 ingest_sync.py 로 통합되었습니다. "
          "앞으로는 'python ingest_sync.py --rebuild' 를 사용하세요. "
          "(이번 실행은 --rebuild 로 위임합니다)")
    ingest_sync.main(["--rebuild"])
