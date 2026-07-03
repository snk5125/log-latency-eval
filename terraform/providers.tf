########################################################################
# providers.tf — Two aliased AWS providers (root module)
#
# WHY THIS FILE EXISTS FOR THE EXPERIMENT:
#   PLAN.md §4.1 defines a two-account topology: a SENDER account holds the
#   generator hosts + forwarding agents, and a LOGGING account holds the
#   aggregator tiers, Cribl leader, and S3 landing/final buckets. Modeling the
#   hops across a realistic cross-account PrivateLink boundary is part of the
#   fidelity of the latency measurement, so we drive two aliased providers
#   (`aws.sender`, `aws.logging`) from named CLI profiles.
#
#   SINGLE-ACCOUNT DRY RUNS: both profile variables (and both account-id
#   variables) MAY point at the SAME AWS account. Nothing here requires two
#   distinct accounts — the cross-account IAM/PrivateLink wiring degrades
#   gracefully to a same-account loopback, which is useful for a cheap dry run
#   before committing to the full two-account deployment.
########################################################################

# Provider for the SENDER account — generator hosts, forwarding agents,
# sender VPC, and the PrivateLink *consumer* interface endpoints.
provider "aws" {
  alias   = "sender"
  region  = var.aws_region
  profile = var.sender_profile

  # Guard-rail: refuse to run against the wrong account. This protects the
  # experiment from accidentally provisioning generator hosts in an unrelated
  # account (which would silently invalidate cross-account latency numbers).
  allowed_account_ids = [var.sender_account_id]

  default_tags {
    tags = local.common_tags
  }
}

# Provider for the LOGGING account — aggregators (Vector + Cribl), NLBs,
# PrivateLink *service* endpoints, S3 buckets, SQS queues.
provider "aws" {
  alias   = "logging"
  region  = var.aws_region
  profile = var.logging_profile

  # Same guard-rail on the logging side. For single-account dry runs, set
  # logging_account_id == sender_account_id and both profiles to the same value.
  allowed_account_ids = [var.logging_account_id]

  default_tags {
    tags = local.common_tags
  }
}
