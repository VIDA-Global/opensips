locals {
  architecture = "arm64"
  build_time   = formatdate("YYYYMMDDhhmmss", timestamp())
  common_tags = merge(var.additional_tags, {
    Application    = var.application_name
    Architecture   = local.architecture
    BuildId        = var.build_id
    Environment    = var.environment
    ImageFamily    = var.ami_name_prefix
    ManagedBy      = "packer"
    OpenSIPSCommit = var.opensips_source_commit
    SourceSHA256   = var.opensips_source_sha256
    Version        = var.opensips_version
  })
}

source "amazon-ebs" "opensips_arm64" {
  region                      = var.aws_region
  instance_type               = var.instance_type
  vpc_id                      = var.vpc_id
  subnet_id                   = var.subnet_id
  security_group_ids          = var.security_group_ids
  iam_instance_profile        = var.build_instance_profile
  associate_public_ip_address = false

  communicator              = "ssh"
  ssh_username              = var.ssh_username
  ssh_interface             = "private_ip"
  ssh_timeout               = var.ssh_timeout
  ssh_clear_authorized_keys = true

  source_ami_filter {
    filters = {
      architecture        = "arm64"
      name                = var.source_ami_name
      root-device-type    = "ebs"
      state               = "available"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = [var.source_ami_owner]
  }

  ami_name                = "${var.ami_name_prefix}-${var.opensips_version}-arm64-${var.build_id}-${local.build_time}"
  ami_description         = "OpenSIPS ${var.opensips_version} on Ubuntu 24.04 ARM64"
  ami_virtualization_type = "hvm"
  ena_support             = true
  imds_support            = "v2.0"

  metadata_options {
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
    http_tokens                 = "required"
    instance_metadata_tags      = "disabled"
  }

  launch_block_device_mappings {
    delete_on_termination = true
    device_name           = var.root_device_name
    encrypted             = true
    iops                  = var.root_volume_iops
    kms_key_id            = var.kms_key_id
    throughput            = var.root_volume_throughput
    volume_size           = var.root_volume_size
    volume_type           = "gp3"
  }

  run_tags        = merge(local.common_tags, { Name = "${var.ami_name_prefix}-packer-${var.build_id}" })
  run_volume_tags = local.common_tags
  snapshot_tags   = local.common_tags
  tags            = local.common_tags
}

build {
  name    = "opensips-arm64"
  sources = ["source.amazon-ebs.opensips_arm64"]

  provisioner "file" {
    source      = "${path.root}/../build/sources/opensips-${var.opensips_version}.tar.gz"
    destination = "/tmp/opensips-source.tar.gz"
  }

  provisioner "ansible" {
    playbook_file = "${path.root}/../ansible/playbooks/ami.yml"
    user          = var.ssh_username
    extra_arguments = [
      "--extra-vars",
      "opensips_ami_version=${var.opensips_version} opensips_ami_source_commit=${var.opensips_source_commit} opensips_ami_source_sha256=${var.opensips_source_sha256}",
      "--extra-vars",
      "opensips_ami_modules=${jsonencode(var.opensips_modules)}"
    ]
    ansible_env_vars = [
      "ANSIBLE_CONFIG=${path.root}/../ansible/ansible.cfg",
      "ANSIBLE_HOST_KEY_CHECKING=False"
    ]
  }

  post-processor "manifest" {
    output     = "${path.root}/../build/packer-manifest.json"
    strip_path = true
    custom_data = {
      architecture           = local.architecture
      opensips_source_commit = var.opensips_source_commit
      opensips_source_sha256 = var.opensips_source_sha256
      opensips_version       = var.opensips_version
    }
  }
}
