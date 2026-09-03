#!/usr/bin/env bash
set -euo pipefail

readonly vars_file=${1:-}
[[ -n $vars_file && -f $vars_file ]] || { printf 'fetch-source-from-vars: a readable pkrvars file is required\n' >&2; exit 64; }

packer_value() {
  local expression=$1
  printf '%s\n' "$expression" | packer console -var-file="$vars_file" packer
}

export OPENSIPS_VERSION
export OPENSIPS_SOURCE_COMMIT
export OPENSIPS_SOURCE_SHA256
OPENSIPS_VERSION=$(packer_value 'var.opensips_version')
OPENSIPS_SOURCE_COMMIT=$(packer_value 'var.opensips_source_commit')
OPENSIPS_SOURCE_SHA256=$(packer_value 'var.opensips_source_sha256')

exec ./scripts/fetch-source.sh
