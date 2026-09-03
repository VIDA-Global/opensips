variable "aws_region" {
  type        = string
  description = "Region in which the source AMI is built and tested."
  default     = "us-east-2"

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "The aws_region value must be a valid commercial AWS region name."
  }
}

variable "instance_type" {
  type        = string
  description = "ARM64 EC2 instance type used during the image build."
  default     = "m9g.large"
}

variable "vpc_id" {
  type        = string
  description = "Existing VPC containing the private build subnet."
}

variable "subnet_id" {
  type        = string
  description = "Existing private subnet reachable by the self-hosted runner."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Existing security groups allowing SSH from the self-hosted runner."

  validation {
    condition     = length(var.security_group_ids) > 0
    error_message = "At least one existing security group ID is required."
  }
}

variable "build_instance_profile" {
  type        = string
  description = "Existing instance profile for the temporary Packer builder."
}

variable "kms_key_id" {
  type        = string
  description = "KMS key ARN or ID used to encrypt the source-region AMI snapshot."
}

variable "source_ami_owner" {
  type        = string
  description = "Canonical AWS account ID."
  default     = "099720109477"
}

variable "source_ami_name" {
  type        = string
  description = "Canonical Ubuntu ARM64 source AMI name filter."
  default     = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"
}

variable "ssh_username" {
  type        = string
  description = "SSH user provided by the source AMI."
  default     = "ubuntu"
}

variable "ssh_timeout" {
  type        = string
  description = "Maximum wait for private-IP SSH connectivity."
  default     = "15m"
}

variable "root_device_name" {
  type        = string
  description = "Root device name exposed by the selected Ubuntu AMI."
  default     = "/dev/sda1"
}

variable "root_volume_size" {
  type        = number
  description = "Root GP3 volume size in GiB."
  default     = 16

  validation {
    condition     = var.root_volume_size >= 8
    error_message = "The root_volume_size value must be at least 8 GiB."
  }
}

variable "root_volume_iops" {
  type        = number
  description = "Root GP3 volume provisioned IOPS."
  default     = 3000
}

variable "root_volume_throughput" {
  type        = number
  description = "Root GP3 volume throughput in MiB/s."
  default     = 125
}

variable "opensips_version" {
  type        = string
  description = "OpenSIPS release version compiled into the AMI."
  default     = "3.6.8"
}

variable "opensips_source_commit" {
  type        = string
  description = "Canonical upstream commit represented by the source archive."
  default     = "f9f85260e5def73e3f854f5e22d148d2d977e85f"
}

variable "opensips_source_sha256" {
  type        = string
  description = "SHA-256 of the exact OpenSIPS source archive."
  default     = "b3e1ab4d82dce763bbd51c99a1733f133465fda8fe2591f86aec9c3eefababf0"

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.opensips_source_sha256))
    error_message = "The opensips_source_sha256 value must be a lowercase SHA-256 digest."
  }
}

variable "opensips_modules" {
  type        = list(string)
  description = "Exact dynamically built OpenSIPS module allowlist."
  default = [
    "b2b_entities",
    "b2b_logic",
    "clusterer",
    "db_postgres",
    "dialog",
    "maxfwd",
    "proto_bin",
    "proto_hep",
    "proto_tls",
    "rr",
    "rtpengine",
    "sipmsgops",
    "sl",
    "textops",
    "tls_mgm",
    "tls_openssl",
    "tm",
    "topology_hiding",
    "tracer"
  ]
}

variable "ami_name_prefix" {
  type        = string
  description = "Immutable AMI name prefix."
  default     = "vida-live-opensips"
}

variable "application_name" {
  type        = string
  description = "Application tag value."
  default     = "opensips"
}

variable "environment" {
  type        = string
  description = "Build environment tag value."
  default     = "image-build"
}

variable "build_id" {
  type        = string
  description = "Unique immutable build identity, normally the GitHub run ID and attempt."
  default     = "local"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+$", var.build_id))
    error_message = "The build_id value may contain only letters, digits, dots, underscores, and hyphens."
  }
}

variable "additional_tags" {
  type        = map(string)
  description = "Additional organization-specific tags applied to instances, AMIs, and snapshots."
  default     = {}
}
