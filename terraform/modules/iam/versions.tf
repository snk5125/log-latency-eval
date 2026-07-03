########################################################################
# module: iam — provider requirements
#
# WHY: this module creates roles in BOTH accounts, so it declares two provider
# configuration aliases (aws.sender, aws.logging) that the root module passes in.
########################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = "~> 5.0"
      configuration_aliases = [aws.sender, aws.logging]
    }
  }
}
