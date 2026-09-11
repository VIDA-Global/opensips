#!/bin/sh
set -eu

case "${1:-normal}" in
    normal|missing|truncated|version|header-only|flags|expired) ;;
    *) printf 'unsupported PostgreSQL UA proof case\n' >&2; exit 64 ;;
esac
test "$#" -le 1

# Disposable PostgreSQL authority; no host ports, external network or ambient AWS identity.
name="sage-ua-postgres-proof-$$"
database_image='postgres:18-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15'
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
    -e POSTGRES_USER=ua_proof -e POSTGRES_DB=ua_proof \
    -e POSTGRES_PASSWORD=ua-proof-only "$database_image" >/dev/null
attempt=0
until docker exec "$name-db" pg_isready -h 127.0.0.1 -U ua_proof -d ua_proof >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        printf 'PostgreSQL proof database did not become ready\n' >&2
        exit 1
    fi
    sleep 1
done
docker run --name "$name-probe" --network "$name" --cap-drop ALL \
    --security-opt no-new-privileges --user 65532:65532 --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m sage-opensips:ua-proof \
    python3 /tests/ua-proof.py --postgres --state-case "${1:-normal}"
