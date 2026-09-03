#!/usr/bin/env bash
set -euo pipefail

required=(AMI_ID AWS_REGION SUBNET_ID SECURITY_GROUP_ID VALIDATION_INSTANCE_PROFILE TEST_CONFIG_SECRET_ARN TEST_CONFIG_SECRET_VERSION_ID)
for name in "${required[@]}"; do
  [[ -n ${!name:-} ]] || { printf 'validate-ami: %s is required\n' "$name" >&2; exit 64; }
done

readonly instance_type=${INSTANCE_TYPE:-m9g.large}
readonly expected_version=${OPENSIPS_VERSION:-3.6.8}
readonly ssh_user=${SSH_USER:-ubuntu}
[[ $expected_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'validate-ami: invalid OPENSIPS_VERSION\n' >&2; exit 64; }
if [[ -n ${GITHUB_RUN_ID:-} ]]; then
  run_id=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}
else
  run_id=local-$(date -u +%Y%m%d%H%M%S)
fi
readonly run_id
readonly key_name="opensips-ami-validation-${run_id}"
client_token=$(printf '%s' "${AMI_ID}:${AWS_REGION}:${run_id}" | shasum -a 256 | cut -d' ' -f1)
readonly client_token
work=$(mktemp -d "${TMPDIR:-/tmp}/opensips-ami-validation.XXXXXX")
instance_id=

cleanup() {
  local status=$?
  local discovered
  if [[ -z $instance_id ]]; then
    discovered=$(aws --region "$AWS_REGION" ec2 describe-instances \
      --filters "Name=tag:ValidationRun,Values=$run_id" \
        'Name=instance-state-name,Values=pending,running,stopping,stopped' \
      --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null || true)
    instance_id=${discovered%%[[:space:]]*}
  fi
  if [[ -n $instance_id ]]; then
    aws --region "$AWS_REGION" ec2 terminate-instances --instance-ids "$instance_id" >/dev/null || true
    aws --region "$AWS_REGION" ec2 wait instance-terminated --instance-ids "$instance_id" >/dev/null 2>&1 || true
  fi
  aws --region "$AWS_REGION" ec2 delete-key-pair --key-name "$key_name" >/dev/null 2>&1 || true
  rm -rf -- "$work"
  exit "$status"
}
trap cleanup EXIT INT TERM

ssh-keygen -q -t ed25519 -N '' -f "$work/id_ed25519"
aws --region "$AWS_REGION" ec2 import-key-pair \
  --key-name "$key_name" \
  --public-key-material "fileb://$work/id_ed25519.pub" >/dev/null

tags=$(jq -cn --arg secret "$TEST_CONFIG_SECRET_ARN" --arg version "$TEST_CONFIG_SECRET_VERSION_ID" --arg run "$run_id" '[
  {ResourceType:"instance",Tags:[
    {Key:"Name",Value:("opensips-ami-validation-" + $run)},
    {Key:"OpenSIPSConfigSecretArn",Value:$secret},
    {Key:"OpenSIPSConfigSecretVersion",Value:$version},
    {Key:"ValidationRun",Value:$run},
    {Key:"Purpose",Value:"ami-validation"}
  ]}
]')
response=$(aws --region "$AWS_REGION" ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type "$instance_type" \
  --subnet-id "$SUBNET_ID" \
  --security-group-ids "$SECURITY_GROUP_ID" \
  --iam-instance-profile "Name=$VALIDATION_INSTANCE_PROFILE" \
  --key-name "$key_name" \
  --no-associate-public-ip-address \
  --metadata-options 'HttpEndpoint=enabled,HttpTokens=required,HttpPutResponseHopLimit=1,InstanceMetadataTags=enabled' \
  --client-token "$client_token" \
  --tag-specifications "$tags")
instance_id=$(jq -er '.Instances[0].InstanceId' <<<"$response")
aws --region "$AWS_REGION" ec2 wait instance-status-ok --instance-ids "$instance_id"

description=$(aws --region "$AWS_REGION" ec2 describe-instances --instance-ids "$instance_id")
private_ip=$(jq -er '.Reservations[0].Instances[0].PrivateIpAddress' <<<"$description")
volume_id=$(jq -er '.Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' <<<"$description")
[[ $(jq -r '.Reservations[0].Instances[0].MetadataOptions.HttpTokens' <<<"$description") == required ]]
[[ $(jq -r '.Reservations[0].Instances[0].MetadataOptions.InstanceMetadataTags' <<<"$description") == enabled ]]
[[ $(aws --region "$AWS_REGION" ec2 describe-volumes --volume-ids "$volume_id" | jq -r '.Volumes[0].Encrypted') == true ]]

ssh_options=(-i "$work/id_ed25519" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
for _ in $(seq 1 60); do
  if ssh "${ssh_options[@]}" "$ssh_user@$private_ip" true 2>/dev/null; then
    break
  fi
  sleep 5
done
# expected_version is constrained to digits and dots before client-side expansion.
# shellcheck disable=SC2029
ssh "${ssh_options[@]}" "$ssh_user@$private_ip" \
  "test \"\$(uname -m)\" = aarch64 && grep -q '^VERSION_ID=\"24.04\"' /etc/os-release && /usr/sbin/opensips -V 2>&1 | grep -F '$expected_version' && sudo systemctl is-active opensips-runtime-config.service opensips.service && sudo sh -c 'umask 077; output=\$(mktemp); if ! /usr/sbin/opensips -C -f /run/opensips-secure/config/opensips.cfg >\"\$output\" 2>&1; then rm -f \"\$output\"; exit 1; fi; rm -f \"\$output\"'"

boot_id=$(ssh "${ssh_options[@]}" "$ssh_user@$private_ip" 'cat /proc/sys/kernel/random/boot_id')
aws --region "$AWS_REGION" ec2 reboot-instances --instance-ids "$instance_id"
reboot_observed=false
for _ in $(seq 1 90); do
  current_boot_id=$(ssh "${ssh_options[@]}" "$ssh_user@$private_ip" 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true)
  if [[ -n $current_boot_id && $current_boot_id != "$boot_id" ]]; then
    reboot_observed=true
    break
  fi
  sleep 5
done
[[ $reboot_observed == true ]] || { printf 'validate-ami: reboot was not observed\n' >&2; exit 1; }
aws --region "$AWS_REGION" ec2 wait instance-status-ok --instance-ids "$instance_id"
ssh "${ssh_options[@]}" "$ssh_user@$private_ip" \
  "sudo systemctl is-active opensips-runtime-config.service opensips.service"

if [[ -n ${POST_VALIDATION_SCRIPT:-} ]]; then
  INSTANCE_ID=$instance_id PRIVATE_IP=$private_ip "$POST_VALIDATION_SCRIPT"
fi

printf 'Validated AMI %s on instance %s.\n' "$AMI_ID" "$instance_id"
