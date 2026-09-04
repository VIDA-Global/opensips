#!/usr/bin/env bash
set -euo pipefail

required=(NODE_A_HOST NODE_B_HOST RTPENGINE_A_HOST RTPENGINE_B_HOST SIP_TEST_COMMAND)
for name in "${required[@]}"; do
  [[ -n ${!name:-} ]] || { printf 'ha-media-boundary: %s is required\n' "$name" >&2; exit 64; }
done

printf '%s\n' \
  'This harness delegates SIP traffic generation to SIP_TEST_COMMAND.' \
  'The command must prove confirmed B2B state takeover, kill the selected RTPengine,' \
  'prove that renegotiation does not migrate to a replacement RTPengine, reject the' \
  'failed operation, and verify deterministic full-call cleanup through monitoring.'

export NODE_A_HOST NODE_B_HOST RTPENGINE_A_HOST RTPENGINE_B_HOST
"$SIP_TEST_COMMAND"
