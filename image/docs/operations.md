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
- PostgreSQL, RTPengine, cluster BIN peers, SIP peers, and the external HEP collector as selected by deployment configuration.

Security groups should separate SIP UDP/TCP/TLS, private BIN traffic, PostgreSQL, RTPengine control, HEP export, health checks, and operator access. BIN must never be internet-accessible.

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
6. Invokes an optional external SIP/HEP/HA harness.
7. Terminates the instance and deletes the key pair through its exit trap.

The Ansible role is bake-only. It deliberately removes source inputs and resets cloud-init and machine identity at the end, so it is not a supported day-two configuration mechanism.

For production acceptance, `AMI_POST_VALIDATION_SCRIPT` should additionally verify UDP, TCP, TLS, PostgreSQL, HEP receipt, two-node cluster synchronization, established B2B call takeover, RTPengine selection persistence, and the media-recovery scenarios in `media-recovery.md`.

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
