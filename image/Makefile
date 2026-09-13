SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

PACKER_DIR := packer
BUILD_DIR := build
VARS_FILE ?= packer/opensips.pkrvars.hcl

.DEFAULT_GOAL := help

.PHONY: test-placement-postgres-proof

.PHONY: help init fmt validate lint source test source-test package-secret build inspect promote clean proof-image test-load-https-proof test-ua-proof test-ua-postgres-proof

help:
	@printf '%s\n' \
	  'OpenSIPS ARM64 AMI targets:' \
	  '  make init                         Install pinned Packer plugins' \
	  '  make fmt                          Format Packer files' \
	  '  make validate                     Run AWS-free static validation' \
	  '  make lint                         Run shell and workflow YAML linters' \
	  '  make source                       Download and verify OpenSIPS source' \
	  '  make test                         Run local contract tests' \
	  '  make test-ua-postgres-proof        Probe UA crash recovery against isolated PostgreSQL' \
	  '  make test-load-https-proof         Probe bounded direct HTTPS on an isolated network' \
	  '  make test-placement-postgres-proof Probe distributed SQL reservations on an isolated network' \
	  '  make source-test MODE=<mode>      Run Linux source build, unit, or fuzz tests' \
	  '  make package-secret DEPLOYMENT=<file> CERT=<file> KEY=<file> CA=<file>' \
	  '  make build VARS_FILE=<file>        Build the source-region AMI (uses AWS)' \
	  '  make inspect                      Inspect the Packer template locally' \
	  '  make promote MANIFEST=<file>       Promote a tested AMI (uses AWS)' \
	  '  make clean                        Remove generated local build files'

init:
	packer init $(PACKER_DIR)

fmt:
	packer fmt -recursive $(PACKER_DIR)

validate:
	./scripts/validate-local.sh

lint:
	./scripts/lint.sh

source:
	./scripts/fetch-source.sh

test:
	python3 -m unittest discover -s tests -p 'test_*.py'

proof-image: source
	docker buildx build --platform linux/arm64 --build-context b2b_entities=../modules/b2b_entities --build-context b2b_logic=../modules/b2b_logic --file tests/integration/ua-proof.Dockerfile --load --tag sage-opensips:ua-proof .

.PHONY: test-b2b-headers-proof
test-b2b-headers-proof: proof-image
	docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges --user 65532:65532 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m sage-opensips:ua-proof python3 /tests/b2b-headers-proof.py
	docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges --user 65532:65532 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m sage-opensips:ua-proof python3 /tests/native-edge-http-proof.py

test-ua-proof: proof-image
	docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges --user 65532:65532 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m sage-opensips:ua-proof

.PHONY: stock-proxy-proof-image test-proxy-reanchor-proof test-proxy-takeover-proof test-demux-proof
stock-proxy-proof-image: source
	docker buildx build --platform linux/arm64 --file tests/integration/proxy-proof.Dockerfile --load --tag sage-opensips:stock-proxy-proof .

test-proxy-reanchor-proof: stock-proxy-proof-image
	sh tests/integration/run-proxy-reanchor-proof.sh

test-proxy-takeover-proof: stock-proxy-proof-image
	sh tests/integration/run-proxy-reanchor-proof.sh --takeover

test-demux-proof: stock-proxy-proof-image
	docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges --user 65532:65532 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m sage-opensips:stock-proxy-proof python3 /tests/demux-proof.py

.PHONY: test-demux-media-proof
test-demux-media-proof: stock-proxy-proof-image
	sh tests/integration/run-proxy-reanchor-proof.sh --demux-media

.PHONY: test-demux-freeswitch-proof
test-demux-freeswitch-proof: stock-proxy-proof-image
	sh tests/integration/run-proxy-reanchor-proof.sh --demux-freeswitch

test-load-https-proof: proof-image
	sh tests/integration/run-load-https-proof.sh

test-placement-postgres-proof: proof-image
	sh tests/integration/run-placement-postgres-proof.sh

test-ua-postgres-proof: test-ua-proof
	for scenario in normal missing truncated version header-only flags expired; do sh tests/integration/run-ua-postgres-proof.sh "$$scenario"; done

source-test:
	@test -n "$${MODE:-}" || { printf 'MODE must be build, unit, or fuzz\n' >&2; exit 64; }
	./scripts/run-source-tests.sh "$$MODE"

package-secret:
	@test -n "$${DEPLOYMENT:-}" -a -n "$${CERT:-}" -a -n "$${KEY:-}" -a -n "$${CA:-}" || { \
		printf 'DEPLOYMENT, CERT, KEY, and CA are required\n' >&2; exit 64; }
	@python3 scripts/package-runtime-secret.py "$$DEPLOYMENT" "$$CERT" "$$KEY" "$$CA"

build:
	@test -f "$(VARS_FILE)" || { printf 'missing VARS_FILE: %s\n' "$(VARS_FILE)" >&2; exit 64; }
	./scripts/fetch-source-from-vars.sh "$(VARS_FILE)"
	packer build -var-file="$(VARS_FILE)" $(PACKER_DIR)

inspect:
	packer inspect $(PACKER_DIR)

promote:
	@test -n "$${MANIFEST:-}" || { printf 'MANIFEST is required\n' >&2; exit 64; }
	python3 scripts/promote-ami.py --manifest "$$MANIFEST"

clean:
	rm -rf -- "$(BUILD_DIR)"
