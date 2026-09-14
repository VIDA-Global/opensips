#!/bin/sh
set -eu
root=$(CDPATH='' cd -- "$(dirname "$0")/../.." && pwd)
name="sage-ownership-proof-$$"
cleanup() {
    docker rm -fv "$name-probe" "$name-db" >/dev/null 2>&1 || true
    docker network rm "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker network create --internal "$name" >/dev/null
docker run -d --name "$name-db" --network "$name" --network-alias postgres \
    --tmpfs /var/lib/postgresql:rw,nosuid,nodev,size=512m \
    -e POSTGRES_USER=ownership -e POSTGRES_DB=ownership -e POSTGRES_PASSWORD=ownership-proof-only \
    postgres:18-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15 >/dev/null
attempt=0
until docker exec "$name-db" pg_isready -h 127.0.0.1 -U ownership -d ownership >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then exit 1; fi
    sleep 1
done
docker run --name "$name-probe" --network "$name" --cap-drop ALL \
    --security-opt no-new-privileges --user 65532:65532 --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    --mount "type=bind,source=$root/scripts/ownership.py,target=/tests/ownership.py,readonly" \
    --mount "type=bind,source=$root/scripts/ownership_fencing.py,target=/tests/ownership_fencing.py,readonly" \
    --mount "type=bind,source=$root/assets/ownership-schema.sql,target=/tests/ownership-schema.sql,readonly" \
    --mount "type=bind,source=$root/tests/integration/ownership-proof.py,target=/tests/ownership-proof.py,readonly" \
    sage-opensips:stock-proxy-proof python3 /tests/ownership-proof.py
