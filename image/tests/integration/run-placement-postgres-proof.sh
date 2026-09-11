#!/bin/sh
set -eu
name="sage-placement-proof-$$"
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
    -e POSTGRES_USER=placement -e POSTGRES_DB=placement -e POSTGRES_PASSWORD=placement-proof-only \
    postgres:18-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15 >/dev/null
attempt=0
until docker exec "$name-db" pg_isready -h 127.0.0.1 -U placement -d placement >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then exit 1; fi
    sleep 1
done
docker run --name "$name-probe" --network "$name" --cap-drop ALL \
    --security-opt no-new-privileges --user 65532:65532 --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m sage-opensips:ua-proof \
    python3 /tests/placement-postgres-proof.py
