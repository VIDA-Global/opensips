# OpenSIPS ARM64 AMI

This project builds an immutable OpenSIPS AMI from canonical upstream source. It is intentionally independent of the VIDA-specific OpenSIPS 4.0 release in the repository root.

## Defaults

| Setting | Default |
| --- | --- |
| OpenSIPS | 3.6.8 |
| Source commit | `f9f85260e5def73e3f854f5e22d148d2d977e85f` |
| Source SHA-256 | `b3e1ab4d82dce763bbd51c99a1733f133465fda8fe2591f86aec9c3eefababf0` |
| Operating system | Canonical Ubuntu 24.04 LTS ARM64 |
| Build region | `us-east-2` |
| Build instance | `m9g.large` |
| Root storage | 16 GiB encrypted GP3, 3,000 IOPS, 125 MiB/s |
| Connectivity | Private-IP SSH from a self-hosted VPC runner |
| AMI family | `vida-live-opensips` |

`m9g.large` availability depends on the selected region and Availability Zone. Packer fails rather than silently changing instance families.

## Safety Boundary

These commands are AWS-free:

```bash
make -C image init
make -C image fmt
make -C image validate
make -C image lint
make -C image source
make -C image test
make -C image inspect
```

The maintained OpenSIPS source gates run on Linux after dependencies are installed:

```bash
make -C image source-test MODE=build
make -C image source-test MODE=unit
make -C image source-test MODE=fuzz
```

`MODE=unit` runs every declared TAP suite: core, `acc`, `cfgutils`, and `registrar`. `MODE=fuzz` builds and executes all four standalone fuzz targets. The upstream `system_tests` target is not included because OpenSIPS 3.6.8 ships no `test/Makefile`; the Python 2 `dbtextdb_test.py` suite is also legacy and unsupported on Ubuntu 24.04.

These commands create, mutate, or delete AWS resources and require explicit operational approval:

```bash
make -C image build VARS_FILE=packer/opensips.pkrvars.hcl
AMI_ID=ami-... AWS_REGION=us-east-2 ... image/scripts/validate-ami.sh
make -C image promote MANIFEST=build/promotion.json
```

The build creates a temporary EC2 instance, key pair, encrypted volume, snapshot, and AMI. Validation imports a temporary SSH key, launches and reboots an instance, then terminates it and deletes the key. Promotion copies AMIs and snapshots into destination regions. Failed build and validation cleanup should still be checked in AWS before retrying.

## Prerequisites

- Packer 1.11 or later.
- Ansible and `ansible-lint`.
- GNU Make, Python 3, `jq`, `curl`, `shellcheck`, and `yamllint`.
- AWS CLI v2 for credentialed validation and promotion.
- An ARM64 self-hosted GitHub runner with the label `opensips-ami`.
- Private routing and TCP/22 access from the runner to the build and validation subnets.
- Controlled package egress through NAT, proxies, or an internal Ubuntu mirror.
- Existing VPC, private subnets, security groups, instance profiles, KMS keys, and Secrets Manager test configuration.

The current SSH design is transitional. Once Session Manager endpoints and instance policy exist, replace `ssh_interface = "private_ip"` with the reviewed Packer Session Manager communicator design.

## Local Build

Create an ignored variable file from `packer/opensips.pkrvars.hcl.example`. Never commit real account or network values.

Local AWS configuration must assume the approved builder role. A named AWS profile with `role_arn` and `source_profile` is preferred:

```ini
[profile opensips-ami-builder]
role_arn = arn:aws:iam::123456789012:role/opensips-ami-builder
source_profile = organization-sso
region = us-east-2
```

After authenticating the source profile, select the assumed-role profile and build:

```bash
export AWS_PROFILE=opensips-ami-builder
make -C image build VARS_FILE=packer/opensips.pkrvars.hcl
```

Packer resolves the newest matching Canonical Noble ARM64 image at build time and records the exact source AMI in `build/packer-manifest.json`. Release records must retain that manifest.

## Configuration Variables

All Packer variables are declared in `packer/variables.pkr.hcl`. Important controls include:

| Variable | Purpose |
| --- | --- |
| `aws_region` | Source build and validation region |
| `instance_type` | ARM64 builder type, default `m9g.large` |
| `vpc_id` | Existing VPC |
| `subnet_id` | Existing private build subnet |
| `security_group_ids` | Existing groups permitting runner SSH |
| `build_instance_profile` | Existing temporary builder profile |
| `kms_key_id` | Source-region EBS encryption key |
| `source_ami_owner` | Canonical account, default `099720109477` |
| `source_ami_name` | Ubuntu Noble ARM64 GP3 image filter |
| `root_volume_*` | GP3 size, IOPS, and throughput |
| `opensips_version` | Semantic release identity |
| `opensips_source_commit` | Canonical source commit |
| `opensips_source_sha256` | Required archive digest |
| `opensips_modules` | Exact dynamic module allowlist |
| `ami_name_prefix` | Immutable image family name |
| `application_name` | `Application` tag value |
| `build_id` | Unique workflow or local build identity |
| `additional_tags` | Organization tags |

Changing an OpenSIPS version requires reviewing the canonical tag and commit, calculating the exact archive SHA-256, compiling all requested modules, and passing the full launch and HA integration gates. Version-only overrides are intentionally insufficient.

## Installed Modules

The default dynamic allowlist is:

```text
b2b_entities b2b_logic clusterer db_postgres dialog maxfwd proto_bin
proto_hep proto_tls rr rtpengine sipmsgops sl textops tls_mgm
tls_openssl tm topology_hiding tracer
```

UDP and TCP protocol support are part of the core transport build in this release line. The installed dynamic inventory is checked exactly and stored at `/usr/share/opensips-ami/modules.txt`.

`proto_hep` and `tracer` provide HEP export to an external HOMER collector. The AMI does not install `sipcapture` or operate as a capture database.

## Runtime Contract

The AMI contains no deployable SIP policy, database credential, certificate, private key, topology-hiding secret, HEP credential, or RTPengine endpoint. At boot:

1. The launch template exposes tags through IMDS and requires IMDSv2.
2. `opensips-runtime-config.service` reads only `OpenSIPSConfigSecretArn` and `OpenSIPSConfigSecretVersion`.
3. The helper verifies that the ARN belongs to the instance account and region.
4. The instance role retrieves only the IAM-authorized immutable secret version.
5. The helper validates the strict JSON schema and writes an atomic root:`opensips` runtime bundle.
6. OpenSIPS parses the candidate with `opensips -C` as the service user.
7. `opensips.service` starts only after successful validation.

The required launch-template metadata settings are:

```text
HttpEndpoint=enabled
HttpTokens=required
HttpPutResponseHopLimit=1
InstanceMetadataTags=enabled
```

The secret schema is shown in `config/runtime-secret.json.example`. The `opensips_config` value is trusted application configuration and may refer to TLS files at:

```text
/run/opensips-secure/config/tls/certificate.pem
/run/opensips-secure/config/tls/private-key.pem
/run/opensips-secure/config/tls/ca-bundle.pem
```

IAM, tag mutation permissions, and the secret resource policy must all prevent a workload from selecting an unrelated secret. The helper's account/region check is defense in depth, not an IAM substitute.

## AMI Identity

AMI names use:

```text
vida-live-opensips-<version>-arm64-<build-id>-<UTC timestamp>
```

Tags include `Application=opensips`, `Version`, `Architecture=arm64`, `ImageFamily=vida-live-opensips`, source commit, source checksum, build ID, and management identity. Names are unique and existing images are retained for rollback.

See `docs/operations.md` for CI, validation, promotion, and rollback. See `docs/media-recovery.md` for HA and RTPengine recovery boundaries.
