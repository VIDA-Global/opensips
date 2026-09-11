# AMI Operations

## GitHub Configuration

The `OpenSIPS ARM64 AMI` workflow is manual-only and runs on a trusted ARM64 self-hosted runner inside the VPC. Configure these repository or organization variables:

| Variable | Description |
| --- | --- |
| `AWS_ACCOUNT_ID` | Expected 12-digit AWS account |
| `AMI_TRUSTED_REF` | Exact allowed workflow ref, such as `refs/heads/main` |
| `AMI_BUILD_REGION` | Build region, normally `us-east-2` |
| `AMI_BUILDER_ROLE_ARN` | OIDC role used by Packer |
| `AMI_VALIDATOR_ROLE_ARN` | OIDC role used for temporary validation instances |
| `AMI_PROMOTION_ROLE_ARN` | OIDC role allowed to copy approved images |
| `AMI_BUILD_VPC_ID` | Existing build VPC |
| `AMI_BUILD_SUBNET_ID` | Existing private build subnet |
| `AMI_BUILD_SECURITY_GROUP_IDS_JSON` | JSON array of existing security group IDs |
| `AMI_BUILD_INSTANCE_PROFILE` | Temporary builder instance profile name |
| `AMI_BUILD_KMS_KEY_ID` | Source-region EBS KMS key ARN |
| `AMI_VALIDATION_SUBNET_ID` | Existing private validation subnet |
| `AMI_VALIDATION_SECURITY_GROUP_ID` | Existing validation security group |
| `AMI_VALIDATION_INSTANCE_PROFILE` | Runtime profile permitted to read only the test secret |
| `AMI_TEST_CONFIG_SECRET_ARN` | Non-production boot-validation secret |
| `AMI_TEST_CONFIG_SECRET_VERSION_ID` | Immutable version ID of the test secret |
| `AMI_POST_VALIDATION_SCRIPT` | Optional executable integration harness on the runner |
| `AMI_DESTINATIONS_JSON` | Region-to-KMS-key JSON object |

Create the GitHub Environment `opensips-ami-production`, require reviewers, prevent administrators from bypassing protection, and permit only the trusted release branch. The promotion role's OIDC trust must be limited to this environment subject.

Do not expose credentialed jobs to pull requests or untrusted refs on a self-hosted runner. The workflow uses a manual dispatch and AWS trust-policy branch conditions, but repository permissions and runner-group access remain part of the security boundary.

## IAM Templates

Files under `iam/` are examples requiring placeholder substitution and policy review. They are not infrastructure-as-code and do not create resources.

- `github-oidc-build-trust.json.tmpl` restricts builder and validator roles to the exact trusted ref.
- `github-oidc-promotion-trust.json.tmpl` restricts the promotion role to the protected environment.
- `builder-policy.json.tmpl` supports Packer and one build instance role/KMS key.
- `validator-policy.json.tmpl` supports temporary validation keys and instances.
- `promotion-policy.json.tmpl` supports encrypted regional copies.
- `runtime-instance-policy.json.tmpl` permits one secret and its KMS key.

Further restrict EC2 resources, request tags, VPC IDs, subnets, and security groups using organization SCPs and permission boundaries. Test rendered policies with IAM Access Analyzer before attachment.

`ALL_PROMOTION_KMS_KEY_ARNS_JSON` must contain the source-region build key and every destination-region key. The source key policy must authorize decrypt/re-encryption, and each destination key policy must authorize grants and encryption for the promotion role.

## Network Contract

The build and validation instances have no public IP. The self-hosted runner requires routes, NACLs, DNS, and security-group access to TCP/22 on each instance. Instances require controlled access to Ubuntu repositories during baking. Runtime instances require private or routed access to:

- EC2 IMDS at `169.254.169.254`.
- Regional Secrets Manager endpoints.
- The secret's KMS key through Secrets Manager.
- PostgreSQL, RTPengine, private cluster BIN peers, carrier SIP peers, and FreeSWITCH SIP endpoints selected by schema v1 and database rows.

Security groups should separate SIP UDP/TLS, private BIN traffic, PostgreSQL, RTPengine control and media, FreeSWITCH SIP, health checks, and operator access. BIN must never be internet-accessible. OpenSIPS must not reach FreeSWITCH ESL.

Allow SIP signaling to FreeSWITCH and the negotiated media range between RTPengine
and media peers. Physical channel observations must come from the fixed gateway's
authenticated private HTTPS load endpoint, never OpenSIPS ESL. Do not expose BIN,
PostgreSQL, RTPengine control, or ESL to carrier/public networks.

## Default Policy Deployment

The AMI installs `/etc/opensips/opensips.cfg.template`; operators do not supply arbitrary OpenSIPS text. Create one schema-v2 deployment object per node from `config/deployment.json.example`. Give each node a unique `node_id`, `private_ip` and placement service token. Keep the placement namespace shared across the cohort. Set the existing B2B cluster `state_owner` fields as required by the reviewed topology; these tags are not the placement observer lease. Keep `cluster_id`, advertised identity, carrier allowlists, RTPengine nodes and TLS policy equivalent. The frontend must preserve SIP flow affinity; established-dialog takeover still requires its own qualification.

Before starting the service, apply the installed `placement-schema.sql` in its
PostgreSQL database with owner credentials. Give the runtime role schema USAGE and
table SELECT/INSERT/UPDATE/DELETE, without DDL or Sage tenant-table authority. The
service requires verified PostgreSQL TLS and explicit credentials; configure
`database_ca_bundle` for the database issuer and `ca_bundle` for its HTTPS peers.
The instance role reads the configured gateway-load secret namespace using exact
version IDs, independently of the configuration secret. Grant the corresponding
KMS decrypt key and keep control/ESL namespaces excluded.

Use `systemctl status opensips-placement` for service health and bounded fixed-message
logs for observation failures. The authenticated loopback `/readyz` proves PostgreSQL
access, not spare capacity or media health. Secret or configuration changes require
a controlled service restart after draining. Gateway credential versions are discovered
through refreshed Sage metadata and evict the prior cached version automatically.

From a checkout at the exact OpenSIPS source commit recorded by the AMI, apply the repository-root PostgreSQL schema files named in `config/production-seed.postgres.sql.example`, then apply its reviewed cluster and FreeSWITCH rows. These schema files are deployment inputs and are not retained in the baked AMI. Use one seed node per cluster. Confirm that both cluster nodes report the B2B entity and load-balancer replication capabilities before admitting traffic.

Package each node's secret using `make --no-print-directory -C image package-secret DEPLOYMENT=... CERT=... KEY=... CA=...`. The `--no-print-directory` option keeps Make status banners out of the JSON stream. Use `umask 077`, store the output as a new immutable Secrets Manager version, set `OpenSIPSConfigSecretVersion` to its exact version ID, and replace instances gradually. A service reload does not retrieve a new secret version.

The renderer accepts only schema version 1. Its deployment object must contain exactly `node_id`, `cluster_id`, `private_ip`, `advertised_ip`, `state_owner`, `database_url`, `carrier_udp_ips`, `carrier_tls_ips`, and `rtpengine_nodes`. The TLS object must contain exactly `certificate`, `private_key`, and `ca_bundle`. See the README schema table for types and bounds. Legacy `opensips_config` payloads and unknown extension fields are rejected.

The boot sequence attempts rendering up to ten times with a 15-second backoff, bounded by the unit's 180-second startup timeout. Successful rendering atomically replaces `/run/opensips-secure/config`. The helper validates TLS parsing and key matching with OpenSSL, then checks policy syntax as the `opensips` service user. If either check fails, it restores the previous bundle, logs only a sanitized error, and exits nonzero. On a new instance there is no previous bundle, so `opensips.service` remains stopped.

Configuration checking does not initialize database, RTPengine, or network listeners. A failure during subsequent `opensips.service` module initialization does not trigger renderer rollback. The deployment controller must treat service readiness failure as a failed rollout and replace the instance using the previous AMI and exact secret version.

Before production admission, verify all of the following in a deployment-owned SIP harness:

- Spoofed and mixed-case `X-SAGE-*` headers never reach FreeSWITCH.
- FreeSWITCH receives one `X-SAGE-Source-IP` containing the carrier packet's `$si` value.
- Untrusted source addresses and invalid TLS clients receive no routing privileges.
- The native policy uses direct gateway observations and PostgreSQL pending reservations. Qualify it with real FreeSWITCH physical load and burst arrivals; synthetic SIP placement is not capacity evidence. An uncertain B2B setup is not retried on a different destination under the same allocation ID.
- The stored RTPEngine selection never changes; any fallback allocation attempted internally by the module is detected, deleted, and rejected.
- OpenSIPS node replacement preserves confirmed B2B state within the documented HA boundary.
- RTPEngine renegotiation failure is rejected without moving media; monitoring can terminate the full tuple through `b2b_terminate_call` when policy requires hard failure.
- B2B setup and maximum-duration expiry may produce duplicate idempotent teardown requests from replicated nodes; monitoring removes orphaned RTPEngine sessions and deduplicates alerts.
- Initial-answer media failure may leave the carrier transaction pending until the B2B setup timeout; monitoring should invoke `b2b_terminate_call` immediately using the logged tuple key.

## Build And Validation

Run AWS-free gates first:

```bash
make -C image validate
make -C image lint
make -C image source
```

CI additionally runs a normal source build, every declared TAP suite, and all standalone fuzz targets before any AWS-authenticated job. A failing source gate prevents Packer from receiving AWS credentials.

After change approval, a local build uses:

```bash
AWS_PROFILE=opensips-ami-builder \
  make -C image build VARS_FILE=packer/opensips.pkrvars.hcl
```

The Packer manifest contains the source-region AMI ID. The credentialed validation script then:

1. Imports an ephemeral Ed25519 public key.
2. Launches the AMI privately with IMDSv2 and tag access.
3. Verifies architecture, Ubuntu version, EBS encryption, OpenSIPS version, runtime rendering, and systemd readiness.
4. Runs `opensips -C` against the retrieved configuration.
5. Reboots and checks both services again.
6. Invokes an optional external SIP and HA harness.
7. Terminates the instance and deletes the key pair through its exit trap.

The shell provisioner is bake-only. It runs preflight, dependencies, build,
configuration, cleanup, final verification, and sanitization in that order.
Packer stages non-secret JSON input and repository-owned assets, then promotes
them into a root-owned build directory. The final verification runs after build
package removal and checks the installed dynamic dependencies and module inventory.
Sanitization removes staging and resets cloud-init/machine identity. Never run
these phases as day-two configuration on a serving node. Runtime configuration
assets live in `image/assets`; no provisioning framework is installed or required.

This provisioning migration does not qualify the existing B2BUA policy for Sage.
Its direct FreeSWITCH load integration must still be replaced by the gateway
telemetry consumer, and established-dialog recovery needs live proof. The image
is not a production-approved Sage artifact until those independent gates pass.

For production acceptance, `AMI_POST_VALIDATION_SCRIPT` must verify UDP, mutual TLS,
PostgreSQL, FreeSWITCH SIP and gateway telemetry, cluster synchronization,
established B2B call takeover, RTPengine affinity, trusted-header behavior, and
the scenarios in `media-recovery.md`. These are live gates, not current claims.

## Promotion

Promotion is deliberately separate from Packer. After launch validation, the workflow waits at the protected `opensips-ami-production` environment. Approved promotion:

- Re-verifies source AMI ownership, tags, state, ARM64 architecture, and encryption.
- Resolves each regional KMS key.
- Copies the AMI and snapshots with deterministic idempotency tokens.
- Tags images and snapshots with source identity.
- Waits for availability.
- Verifies snapshot encryption against the exact regional key.
- Writes `build/promoted-amis.json`.

Rerunning promotion discovers an existing source/build-tagged copy and verifies it instead of creating another image. A partial regional failure leaves successful immutable copies in place so a reviewed rerun can continue.

## Rollback And Retention

Rollback changes the launch template or deployment reference to a previously validated AMI and replaces instances. Do not modify packages or modules in place. Drain new-call admission, preserve established calls for the agreed timeout, remove the target, replace it, and repeat readiness and SIP checks.

The launch template must pin `OpenSIPSConfigSecretVersion` to a configuration version compatible with that AMI. Rollback changes the AMI and secret version together; moving only the `AWSCURRENT` stage does not alter a pinned deployment.

No automatic deregistration is implemented. Apply a separately reviewed retention process only after confirming that an AMI is not referenced by a launch template, Auto Scaling Group, rollback record, or regional release manifest.

## Troubleshooting

If `opensips-runtime-config.service` fails, inspect only the generic error with `journalctl -u opensips-runtime-config`. The helper intentionally suppresses AWS exception details and OpenSIPS parser output to avoid leaking secret configuration. Validate a candidate in a controlled location using `opensips -C`; never paste secret contents into CI logs.

If Packer cannot connect, verify private routing and TCP/22 from the runner before changing the AMI security posture. Do not add a public IP as a workaround.

If `m9g.large` is unavailable, select a reviewed ARM64 instance type explicitly in the variable file. Never silently fall back to x86_64.
