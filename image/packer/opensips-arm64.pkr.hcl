locals {
  architecture = "arm64"
  build_time   = formatdate("YYYYMMDDhhmmss", timestamp())
  b2b_header_sources = {
    for name in ["records.c", "logic.c"] :
    name => filesha256("${path.root}/../../modules/b2b_logic/${name}")
  }
  ua_sources = {
    for name in ["b2b_entities.c", "ua_api.c", "ua_api.h", "ua_storage.c"] :
    name => filesha256("${path.root}/../../modules/b2b_entities/${name}")
  }
  placement_sources = merge({
    for name in ["gateway_load_polling", "gateway_load_selection", "placement_store", "placement_observer", "placement_service", "placement_secret"] :
    "placement/${name}.py" => filesha256("${path.root}/../scripts/${name}.py")
    }, {
    for name in ["placement_config.py", "placement-schema.sql", "opensips.cfg.template", "opensips-placement.service"] :
    "assets/${name}" => filesha256("${path.root}/../assets/${name}")
  })
  common_tags = merge(var.additional_tags, {
    Application    = var.application_name
    Architecture   = local.architecture
    BuildId        = var.build_id
    Environment    = var.environment
    ImageFamily    = var.ami_name_prefix
    ManagedBy      = "packer"
    OpenSIPSCommit = var.opensips_source_commit
    SourceSHA256   = var.opensips_source_sha256
    SourceAMI      = var.source_ami_id
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
      image-id            = var.source_ami_id
      name                = var.source_ami_name
      root-device-type    = "ebs"
      state               = "available"
      virtualization-type = "hvm"
    }
    most_recent = false
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

  provisioner "shell" {
    inline = ["install -d -m 0700 /tmp/opensips-image-upload /tmp/opensips-image-upload/assets /tmp/opensips-image-upload/provision /tmp/opensips-image-upload/ua-overrides /tmp/opensips-image-upload/placement /tmp/opensips-image-upload/b2b-header-overrides"]
  }

  provisioner "file" {
    source      = "${path.root}/../build/sources/opensips-${var.opensips_version}.tar.gz"
    destination = "/tmp/opensips-image-upload/source.tar.gz"
  }

  provisioner "file" {
    source      = "${path.root}/../assets/"
    destination = "/tmp/opensips-image-upload/assets"
  }
  provisioner "file" {
    source      = "${path.root}/../provision/"
    destination = "/tmp/opensips-image-upload/provision"
  }
  provisioner "file" {
    source      = "${path.root}/../scripts/gateway_load_polling.py"
    destination = "/tmp/opensips-image-upload/placement/gateway_load_polling.py"
  }
  provisioner "file" {
    source      = "${path.root}/../scripts/gateway_load_selection.py"
    destination = "/tmp/opensips-image-upload/placement/gateway_load_selection.py"
  }
  provisioner "file" {
    source      = "${path.root}/../scripts/placement_store.py"
    destination = "/tmp/opensips-image-upload/placement/placement_store.py"
  }
  provisioner "file" {
    source      = "${path.root}/../scripts/placement_observer.py"
    destination = "/tmp/opensips-image-upload/placement/placement_observer.py"
  }
  provisioner "file" {
    source      = "${path.root}/../scripts/placement_service.py"
    destination = "/tmp/opensips-image-upload/placement/placement_service.py"
  }
  provisioner "file" {
    source      = "${path.root}/../scripts/placement_secret.py"
    destination = "/tmp/opensips-image-upload/placement/placement_secret.py"
  }
  provisioner "file" {
    content = jsonencode({
      version            = var.opensips_version
      commit             = var.opensips_source_commit
      sha256             = var.opensips_source_sha256
      modules            = var.opensips_modules
      ua_sources         = local.ua_sources
      placement_sources  = local.placement_sources
      b2b_header_sources = local.b2b_header_sources
    })
    destination = "/tmp/opensips-image-upload/input.json"
  }
  provisioner "file" {
    source      = "${path.root}/../../modules/b2b_entities/b2b_entities.c"
    destination = "/tmp/opensips-image-upload/ua-overrides/b2b_entities.c"
  }
  provisioner "file" {
    source      = "${path.root}/../../modules/b2b_entities/ua_api.c"
    destination = "/tmp/opensips-image-upload/ua-overrides/ua_api.c"
  }
  provisioner "file" {
    source      = "${path.root}/../../modules/b2b_entities/ua_api.h"
    destination = "/tmp/opensips-image-upload/ua-overrides/ua_api.h"
  }
  provisioner "file" {
    source      = "${path.root}/../../modules/b2b_entities/ua_storage.c"
    destination = "/tmp/opensips-image-upload/ua-overrides/ua_storage.c"
  }
  provisioner "file" {
    source      = "${path.root}/../../modules/b2b_logic/records.c"
    destination = "/tmp/opensips-image-upload/b2b-header-overrides/records.c"
  }
  provisioner "file" {
    source      = "${path.root}/../../modules/b2b_logic/logic.c"
    destination = "/tmp/opensips-image-upload/b2b-header-overrides/logic.c"
  }
  provisioner "shell" {
    inline = [
      "sudo install -d -o root -g root -m 0755 /opt/opensips-image-build",
      "sudo cp -a /tmp/opensips-image-upload/. /opt/opensips-image-build/",
      "sudo chown -R root:root /opt/opensips-image-build",
      "sudo chmod -R go-w /opt/opensips-image-build",
      "sudo bash /opt/opensips-image-build/provision/provision.sh preflight",
      "sudo bash /opt/opensips-image-build/provision/provision.sh dependencies",
      "sudo bash /opt/opensips-image-build/provision/provision.sh build",
      "sudo bash /opt/opensips-image-build/provision/provision.sh configure",
      "sudo bash /opt/opensips-image-build/provision/provision.sh cleanup",
      "sudo bash /opt/opensips-image-build/provision/provision.sh verify",
      "sudo bash /opt/opensips-image-build/provision/provision.sh sanitize"
    ]
  }

  post-processor "manifest" {
    output     = "${path.root}/../build/packer-manifest.json"
    strip_path = true
    custom_data = {
      architecture             = local.architecture
      opensips_source_commit   = var.opensips_source_commit
      opensips_source_sha256   = var.opensips_source_sha256
      opensips_version         = var.opensips_version
      source_ami_id            = var.source_ami_id
      ua_source_sha256         = sha256(jsonencode(local.ua_sources))
      b2b_header_source_sha256 = sha256(jsonencode(local.b2b_header_sources))
      placement_source_sha256  = sha256(jsonencode(local.placement_sources))
    }
  }
}
