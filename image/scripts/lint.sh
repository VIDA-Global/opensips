#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly script_dir
image_dir=$(dirname -- "$script_dir")
readonly image_dir

cd "$image_dir"
shellcheck scripts/*.sh tests/integration/*.sh
yamllint -c .yamllint ansible \
  ../.github/workflows/opensips-ami.yml \
  ../.github/workflows/unittests.yml
(
  cd ansible
  ANSIBLE_CONFIG=ansible.cfg ansible-lint playbooks/ami.yml
)
