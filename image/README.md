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
b2b_entities b2b_logic clusterer db_postgres dialog freeswitch load_balancer
maxfwd proto_bin proto_hep proto_tls rr rtpengine sipmsgops sl textops
tls_mgm tls_openssl tm topology_hiding tracer
uac_auth
```

UDP and TCP protocol support are part of the core transport build in this release line, but runtime policy must still activate the required `proto_udp.so` or `proto_tcp.so` handler. The installed dynamic inventory is checked exactly and stored at `/usr/share/opensips-ami/modules.txt`.

`freeswitch` and `load_balancer` provide live ESL capacity-based FreeSWITCH selection. `proto_hep` and `tracer` provide HEP export to an external HOMER collector. The AMI does not install `sipcapture` or operate as a capture database.

## Runtime Contract

The AMI installs the reviewed HA B2BUA policy at `/etc/opensips/opensips.cfg.template`. The template is root-owned, is not a runnable configuration, and contains no deployment address, database credential, certificate, private key, FreeSWITCH credential, or RTPengine endpoint. The only supported runtime secret format is schema version 1. Arbitrary `opensips_config` text is rejected.

At boot:

1. The launch template exposes tags through IMDS and requires IMDSv2.
2. `opensips-runtime-config.service` reads only `OpenSIPSConfigSecretArn` and `OpenSIPSConfigSecretVersion`.
3. The helper verifies that the ARN belongs to the instance account and region.
4. The instance role retrieves only the IAM-authorized immutable secret version.
5. The helper validates every schema-v1 field before substituting the fixed template.
6. It writes an atomic root:`opensips` runtime bundle under `/run/opensips-secure/config`.
7. OpenSSL parses the certificate, private key, and CA bundle and verifies that the certificate and key match.
8. OpenSIPS checks policy syntax and route function contexts with `opensips -C` as the service user.
9. A failed render, cryptographic check, or policy parse restores the previous valid bundle and prevents startup.
10. `opensips.service` starts only after successful validation.

The required launch-template metadata settings are:

```text
HttpEndpoint=enabled
HttpTokens=required
HttpPutResponseHopLimit=1
InstanceMetadataTags=enabled
```

### Schema Version 1

`config/runtime-secret.json.example` shows the complete secret, while `config/deployment.json.example` is the non-secret input accepted by the packaging command. The top-level object must contain exactly `schema_version`, `deployment`, and `tls`.

| Field | Type | Validation and purpose |
| --- | --- | --- |
| `schema_version` | integer | Must be exactly `1` |
| `deployment.node_id` | integer | Unique positive cluster node ID |
| `deployment.cluster_id` | integer | Positive cluster ID shared by both nodes |
| `deployment.private_ip` | string | Canonical IPv4 address used by UDP, TLS, BIN, and outbound UDP sockets |
| `deployment.advertised_ip` | string | Canonical frontend IPv4 address advertised in SIP signaling |
| `deployment.state_owner` | string | Exactly `active` or `backup`; deploy one of each |
| `deployment.database_url` | string | `postgres://` URL, at most 1024 characters, with no whitespace, quotes, backslashes, or NUL; percent-encode credentials |
| `deployment.carrier_udp_ips` | array | Non-empty, unique canonical IPv4 source allowlist for UDP |
| `deployment.carrier_tls_ips` | array | Non-empty, unique canonical IPv4 source allowlist for mutual TLS |
| `deployment.rtpengine_nodes` | array | Non-empty unique `udp:host:port` endpoints with integer weights from 1 through 1000 |
| `tls.certificate` | string | Non-empty PEM certificate chain |
| `tls.private_key` | string | Non-empty PEM private key |
| `tls.ca_bundle` | string | Non-empty PEM trust bundle used to verify carrier client certificates |

Unknown, missing, duplicate, malformed, noncanonical, or out-of-range values fail closed. The helper uses OpenSSL to parse the certificate, unencrypted private key, and CA bundle and verifies that the certificate and key match. Trust purpose, certificate lifetime, revocation, hostname/SAN policy, and carrier identity remain deployment acceptance responsibilities. `opensips -C` checks syntax and route contexts but does not initialize PostgreSQL, FreeSWITCH ESL, RTPEngine, or listening sockets; those dependencies are proven only when `opensips.service` starts and deployment health checks pass.

The rendered files are:

```text
/run/opensips-secure/config/opensips.cfg
/run/opensips-secure/config/tls/certificate.pem
/run/opensips-secure/config/tls/private-key.pem
/run/opensips-secure/config/tls/ca-bundle.pem
```

Directories use mode `0750`; files use mode `0640`; ownership is root:`opensips`. The bundle lives on `/run` and is recreated after boot. Neither HUP nor `systemctl reload opensips` fetches a new secret. Roll out a new immutable secret version by updating the launch-template version tag and replacing instances.

The renderer can roll back failures it detects before service startup. A later module-initialization or dependency failure does not automatically restore the previous secret version because the renderer and OpenSIPS are separate systemd units. Production rollout automation must detect `opensips.service` failure and replace the instance with the previous AMI and pinned secret version.

IAM, instance-tag mutation permissions, the secret resource policy, and the KMS key policy must all prevent a workload from selecting or decrypting an unrelated secret. The helper's account/region validation is defense in depth, not an IAM substitute.

## Default SIP Policy

The installed template implements a two-node carrier ingress tier with UDP and mutual TLS, B2BUA topology isolation, PostgreSQL and cluster replication, weighted RTPEngine selection, and live FreeSWITCH ESL capacity. Each node receives a separate schema-v1 secret because `node_id`, `private_ip`, and `state_owner` differ. Both secrets normally share `cluster_id`, `advertised_ip`, database, carrier allowlists, RTPEngine nodes, and TLS trust policy.

The policy removes every case-insensitive carrier-provided `X-SAGE-*` header and supplies exactly one `X-SAGE-Source-IP` to the FreeSWITCH B2B leg using OpenSIPS `$si`. The value is the packet's remote network source, not a SIP header, Via value, forwarded header, or local listener address. Unlisted carrier source addresses receive `403`; initial INVITEs without SDP receive `488`.

FreeSWITCH destinations and ESL credentials are intentionally not in the instance secret. The `load_balancer` module reads them from PostgreSQL so operators can change capacity targets without replacing instances. Apply the canonical schemas listed in `config/production-seed.postgres.sql.example`, replace every seed placeholder, restrict ESL to the OpenSIPS security group and ACL, and use a unique least-privilege ESL credential.

Package reviewed deployment values and TLS material without manually constructing JSON:

```bash
umask 077
make --no-print-directory -C image package-secret \
  DEPLOYMENT=config/deployment.json \
  CERT=/secure/carrier-chain.pem \
  KEY=/secure/carrier-key.pem \
  CA=/secure/carrier-ca.pem > /secure/opensips-runtime-secret.json
```

The packager accepts regular files only and rejects empty, invalid UTF-8, invalid JSON, NUL-containing, and over-64-KiB output. The generated file contains a private key: keep the restrictive umask, upload it as an immutable secret version, verify the version ID, and securely remove local output according to the deployment's secret-handling procedure.

The policy provides weighted new-call failover between RTPEngine nodes and requires an SDP offer in the initial INVITE. OpenSIPS does not transfer established media state. A failed re-INVITE or UPDATE is rejected without changing the stored relay; a failed initial answer tears down its allocation and current B2B leg. OpenSIPS 3.6 configuration does not expose an atomic terminate-both-legs operation, so monitoring must terminate the full tuple through `b2b_terminate_call` when required. Read `docs/media-recovery.md` before changing this behavior.

## AMI Identity

AMI names use:

```text
vida-live-opensips-<version>-arm64-<build-id>-<UTC timestamp>
```

Tags include `Application=opensips`, `Version`, `Architecture=arm64`, `ImageFamily=vida-live-opensips`, source commit, source checksum, build ID, and management identity. Names are unique and existing images are retained for rollback.

See `docs/operations.md` for CI, validation, promotion, and rollback. See `docs/media-recovery.md` for HA and RTPengine recovery boundaries.
