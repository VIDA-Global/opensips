SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

PACKER_DIR := packer
ANSIBLE_DIR := ansible
BUILD_DIR := build
VARS_FILE ?= packer/opensips.pkrvars.hcl

.DEFAULT_GOAL := help

.PHONY: help init fmt validate lint source test source-test build inspect promote clean

help:
	@printf '%s\n' \
	  'OpenSIPS ARM64 AMI targets:' \
	  '  make init                         Install pinned Packer plugins' \
	  '  make fmt                          Format Packer and Ansible YAML files' \
	  '  make validate                     Run AWS-free static validation' \
	  '  make lint                         Run shell, YAML, and Ansible linters' \
	  '  make source                       Download and verify OpenSIPS source' \
	  '  make test                         Run local contract tests' \
	  '  make source-test MODE=<mode>      Run Linux source build, unit, or fuzz tests' \
	  '  make build VARS_FILE=<file>        Build the source-region AMI (uses AWS)' \
	  '  make inspect                      Inspect the Packer template locally' \
	  '  make promote MANIFEST=<file>       Promote a tested AMI (uses AWS)' \
	  '  make clean                        Remove generated local build files'

init:
	packer init $(PACKER_DIR)
	ansible-galaxy collection install -r $(ANSIBLE_DIR)/requirements.yml

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

source-test:
	@test -n "$${MODE:-}" || { printf 'MODE must be build, unit, or fuzz\n' >&2; exit 64; }
	./scripts/run-source-tests.sh "$$MODE"

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
