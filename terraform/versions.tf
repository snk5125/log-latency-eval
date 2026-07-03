########################################################################
# versions.tf — Terraform + provider version constraints (root module)
#
# WHY THIS FILE EXISTS FOR THE EXPERIMENT:
#   The latency test is a peer-reviewable artifact. Reproducibility of the
#   infrastructure depends on pinning the toolchain: PLAN.md §7 mandates the
#   AWS provider be pinned to `~> 5.x` and instance AMIs resolved via SSM at
#   apply time. Pinning here guarantees every reviewer / re-runner materializes
#   the same provider behavior, so measured hop latencies are attributable to
#   architecture (hop count / volume), not to drift in provider defaults.
########################################################################

terraform {
  # Terraform >= 1.5 is required (PLAN mandates a modern core; 1.5 introduced
  # the stable feature set this config relies on, e.g. nested module for_each).
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0" # pinned per PLAN.md §7 — provider drift would confound results
    }
  }
}
