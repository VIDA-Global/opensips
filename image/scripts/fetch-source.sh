#!/usr/bin/env bash
set -euo pipefail

readonly version=${OPENSIPS_VERSION:-3.6.8}
readonly commit=${OPENSIPS_SOURCE_COMMIT:-f9f85260e5def73e3f854f5e22d148d2d977e85f}
readonly expected_sha256=${OPENSIPS_SOURCE_SHA256:-b3e1ab4d82dce763bbd51c99a1733f133465fda8fe2591f86aec9c3eefababf0}
readonly source_url=${OPENSIPS_SOURCE_URL:-https://github.com/OpenSIPS/opensips/archive/${commit}.tar.gz}
readonly output=${OPENSIPS_SOURCE_OUTPUT:-build/sources/opensips-${version}.tar.gz}

fail() { printf 'fetch-source: %s\n' "$*" >&2; exit 64; }

[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'invalid OPENSIPS_VERSION'
[[ $commit =~ ^[0-9a-f]{40}$ ]] || fail 'invalid OPENSIPS_SOURCE_COMMIT'
[[ $expected_sha256 =~ ^[0-9a-f]{64}$ ]] || fail 'invalid OPENSIPS_SOURCE_SHA256'
[[ $output != /* && $output != *'..'* ]] || fail 'OPENSIPS_SOURCE_OUTPUT must be a safe relative path'

mkdir -p -- "$(dirname -- "$output")"
temporary=$(mktemp "${output}.tmp.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT

curl --fail --location --proto '=https' --tlsv1.2 --silent --show-error \
  --retry 3 --retry-all-errors --output "$temporary" "$source_url"
actual_sha256=$(shasum -a 256 "$temporary" | cut -d' ' -f1)
[[ $actual_sha256 == "$expected_sha256" ]] || fail "SHA-256 mismatch: expected $expected_sha256, got $actual_sha256"
tar -tzf "$temporary" >/dev/null || fail 'source archive is not a valid gzip-compressed tar archive'
mv -f -- "$temporary" "$output"
trap - EXIT
printf 'Verified OpenSIPS %s source: %s\n' "$version" "$output"
