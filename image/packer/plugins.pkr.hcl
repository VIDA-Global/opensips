packer {
  required_version = ">= 1.11.0, < 2.0.0"

  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "= 1.8.0"
    }
  }
}
