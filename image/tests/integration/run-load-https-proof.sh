#!/bin/sh
set -eu
name="sage-load-https-proof-$$"
cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker network rm "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker network create --internal "$name" >/dev/null
docker run --name "$name" --network "$name" --hostname gateway.example.test \
    --cap-drop ALL --security-opt no-new-privileges --user 65532:65532 --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m sage-opensips:ua-proof \
    python3 /tests/load-https-proof.py
