#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly script_dir
image_dir=$(dirname -- "$script_dir")
readonly image_dir

cd "$image_dir"
packer fmt -check -recursive packer
packer validate -syntax-only packer
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook \
  --syntax-check -i 'localhost,' ansible/playbooks/ami.yml
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/opensips-ami-pycache" \
  python3 -m py_compile \
  ansible/roles/opensips_ami/files/opensips-runtime-config.py \
  scripts/promote-ami.py
python3 -m unittest discover -s tests -p 'test_*.py'
printf 'AWS-free validation passed.\n'
