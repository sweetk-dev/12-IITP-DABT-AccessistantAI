#!/bin/sh
# docker-entrypoint.sh
# 컨테이너 시작 시 POLICY_DATA_DIR(영속 볼륨) 시드.
#  - 이미지 baseline items 중 데이터 볼륨에 "없는 것만" 복사(cp -n 무클로버)
#  - 운영 중 갱신/추가된 항목은 그대로 보존
#  - 가변 데이터 하위 디렉터리 보장 생성
# POLICY_DATA_DIR 미설정 시 시드 없이 그대로 앱 기동(하위호환).
set -e

if [ -n "$POLICY_DATA_DIR" ]; then
  mkdir -p \
    "$POLICY_DATA_DIR/items/.backups" \
    "$POLICY_DATA_DIR/crawler/snapshots" \
    "$POLICY_DATA_DIR/crawler/staging/.applied" \
    "$POLICY_DATA_DIR/crawler/staging/.rejected" \
    "$POLICY_DATA_DIR/crawler/reports"

  if [ -d /app/policy_db/items ]; then
    cp -rn /app/policy_db/items/. "$POLICY_DATA_DIR/items/" 2>/dev/null || true
  fi

  _n=$(ls "$POLICY_DATA_DIR"/items/*.json 2>/dev/null | wc -l)
  echo "[entrypoint] POLICY_DATA_DIR=$POLICY_DATA_DIR seeded — items=$_n"
fi

exec "$@"
