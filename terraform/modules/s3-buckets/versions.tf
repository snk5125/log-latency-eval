########################################################################
# module: s3-buckets — provider requirements
# WHY: single-provider module (logging account); root passes aws = aws.logging.
########################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
