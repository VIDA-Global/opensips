#!/usr/bin/env bash
set -euo pipefail

readonly mode=${1:-}
readonly libtap_commit=b53e4ef5257f80e881762b6143834d8aae29da1a
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly script_dir
repo_root=$(cd -- "$script_dir/../.." && pwd)
readonly repo_root

fail() { printf 'run-source-tests: %s\n' "$*" >&2; exit 64; }

[[ $(uname -s) == Linux ]] || fail 'Linux is required'
cd "$repo_root"
[[ -f Makefile.defs && -x scripts/build/do_build.sh ]] || fail 'repository root is invalid'

export BUILD_OS=${BUILD_OS:-ubuntu:24.04}
export COMPILER=${COMPILER:-gcc}

case "$mode" in
build)
	sh -x scripts/build/do_build.sh
	;;
unit)
	[[ ! -e lib/libtap ]] || fail 'lib/libtap already exists; use a clean source tree'
	git clone --quiet https://github.com/zorgnax/libtap.git lib/libtap
	git -C lib/libtap checkout --quiet "$libtap_commit"
	sh -x scripts/build/build_libtap.sh lib/libtap
	sh -x scripts/build/build_test_harness.sh lib/libtap
	for module in core acc cfgutils registrar; do
		env MAKE_TGT=test sh -x scripts/build/do_build.sh \
			DEFS_EXTRA_OPTS='-DUNIT_TESTS -fPIE -fPIC' module="$module"
	done
	;;
fuzz)
	command -v clang >/dev/null || fail 'clang is required for fuzz tests'
	readonly fuzz_out=${FUZZ_OUT:-$PWD/image/build/fuzz}
	mkdir -p "$fuzz_out"
	OUT="$fuzz_out" CC=clang CFLAGS='-O1 -g -fno-omit-frame-pointer' test/fuzz/oss-fuzz-build.sh
	"$fuzz_out/fuzz_msg_parser" $'OPTIONS sip:test@example.invalid SIP/2.0\r\nContent-Length: 0\r\n\r\n'
	"$fuzz_out/fuzz_uri_parser" 'sip:test@example.invalid;transport=udp'
	"$fuzz_out/fuzz_csv_parser" 'one,"two",three'
	"$fuzz_out/fuzz_core_funcs" 'OpenSIPS'
	;;
*)
	fail 'mode must be build, unit, or fuzz'
	;;
esac
