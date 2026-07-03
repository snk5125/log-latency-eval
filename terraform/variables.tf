########################################################################
# variables.tf — Root module input variables
#
# WHY THIS FILE EXISTS FOR THE EXPERIMENT:
#   PLAN.md §7 requires every Terraform variable to carry a description. These
#   variables are the only knobs an operator turns between a single-account dry
#   run and the full two-account latency deployment. Instance types default to
#   the PLAN-specified sizes so results are comparable to the SPEC, but are
#   overridable so a reviewer can re-run on cheaper sizes if only smoke-testing
#   the topology (which would be documented as a deviation in their report).
########################################################################

# ---------------------------------------------------------------------------
# Accounts, profiles, region (PLAN.md §4.1)
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for BOTH accounts. PLAN.md §4.1 mandates a single region for sender and logging so cross-account hops are not confounded by inter-region latency."
  type        = string
  default     = "us-east-2"
}

variable "sender_profile" {
  description = "Named AWS CLI profile for the SENDER account (generator hosts). Supplied via gitignored terraform.tfvars. May equal logging_profile for a single-account dry run."
  type        = string
}

variable "logging_profile" {
  description = "Named AWS CLI profile for the LOGGING account (aggregators + S3). Supplied via gitignored terraform.tfvars. May equal sender_profile for a single-account dry run."
  type        = string
}

variable "sender_account_id" {
  description = "12-digit account ID of the SENDER account. Used as an allowed_account_ids guard-rail and to scope cross-account IAM/bucket policies to the generator-host roles."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.sender_account_id))
    error_message = "sender_account_id must be a 12-digit AWS account ID."
  }
}

variable "logging_account_id" {
  description = "12-digit account ID of the LOGGING account. Used as an allowed_account_ids guard-rail, as the S3 bucket-name suffix, and as the PrivateLink allowed-principal owner. May equal sender_account_id for a single-account dry run."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.logging_account_id))
    error_message = "logging_account_id must be a 12-digit AWS account ID."
  }
}

# ---------------------------------------------------------------------------
# Networking toggles (PLAN.md §4.2 / §4.3 — NAT is package-install-only)
# ---------------------------------------------------------------------------

variable "enable_sender_nat" {
  description = "Provision a NAT gateway in the SENDER VPC. PLAN.md §4.2: NAT is permitted SOLELY for package installation and should be disabled after provisioning. Data path is private-subnet-only regardless of this toggle."
  type        = bool
  default     = true
}

variable "enable_logging_nat" {
  description = "Provision a NAT gateway in the LOGGING VPC. PLAN.md §4.3: package-install-only; disable after aggregators/Cribl are installed."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Instance type overrides (defaults = PLAN.md §4.2 / §4.3 sizes)
# ---------------------------------------------------------------------------

variable "generator_instance_type" {
  description = "Instance type for the 4 generator hosts (2 Linux + 2 Windows). PLAN.md §4.2 specifies m6i.large."
  type        = string
  default     = "m6i.large"
}

variable "aggregator_instance_type" {
  description = "Instance type for Vector aggregator tiers and Cribl worker groups. PLAN.md §4.3 specifies m6i.xlarge for all aggregator/worker nodes."
  type        = string
  default     = "m6i.xlarge"
}

variable "cribl_leader_instance_type" {
  description = "Instance type for the single Cribl Stream leader node. PLAN.md §4.3 specifies m6i.large (control plane only; not on the data path)."
  type        = string
  default     = "m6i.large"
}

# ---------------------------------------------------------------------------
# Data-lifecycle knobs (PLAN.md §4.3 — 7-day expiry keeps the test cheap)
# ---------------------------------------------------------------------------

variable "landing_final_expiration_days" {
  description = "Lifecycle expiration (days) for objects in landing/final/artifacts buckets. PLAN.md §4.3 mandates 7-day expiry so short-lived experiment data does not accrue storage cost."
  type        = number
  default     = 7
}
