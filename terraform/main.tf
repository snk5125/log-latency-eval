########################################################################
# main.tf — Root module composition (llt / logging latency test)
#
# WHY THIS FILE EXISTS FOR THE EXPERIMENT:
#   This is the wiring diagram for the whole hop-latency test bed. It stitches
#   together the sender account (generator hosts + agents) and the logging
#   account (aggregators + S3 + SQS) so that the four scenarios in PLAN.md §2
#   (S1..S4) can all be exercised on ONE deployment (§7: "infrastructure is
#   deployed once for all 48 runs; scenario switching is config-only").
#
#   MODULE DATA FLOW (the load-bearing cross-account edges):
#     logging: s3-buckets ─┐
#                          ├─> sqs-notify        (S3 landing → SQS event pickup)
#              iam ─> generator-hosts / aggregators / cribl (roles)
#              vector-aggregator / cribl-stream ─> NLB ARNs
#                          └─> privatelink (endpoint services front Tier-1 NLBs)
#     privatelink.service_names ──────────────> sender-network interface endpoints
#     s3-buckets landing-policy consumes generator-host role ARNs (cross-account)
#
#   SINGLE-ACCOUNT DRY RUN: provider aliases may both target one account; the
#   cross-account references (bucket policy principals, PrivateLink allowed
#   principals) collapse to same-account references and still validate/apply.
########################################################################

# ---------------------------------------------------------------------------
# Shared locals: common tag base (PLAN.md §7) + derived naming.
# Os/Stack/Role are set per-resource in modules; here we set the invariant tags.
# ---------------------------------------------------------------------------
locals {
  # Applied via provider default_tags to EVERY resource in both accounts so the
  # Ansible dynamic inventory (tag-based, PLAN §7) can discover hosts and so all
  # experiment resources are attributable/cleanable under one Project tag.
  common_tags = {
    Project = "llt"
  }
}

# ===========================================================================
# LOGGING ACCOUNT
# ===========================================================================

# --- Shared IAM roles/policies (least privilege; consumed by hosts below) ---
# WHY: Centralizing role definitions keeps the least-privilege statements in one
# reviewable place (PLAN §7) and lets the S3 bucket policies reference stable
# generator-host role ARNs for cross-account PutObject grants.
module "iam" {
  source = "./modules/iam"
  providers = {
    aws.sender  = aws.sender
    aws.logging = aws.logging
  }

  name_prefix        = "llt"
  logging_account_id = var.logging_account_id

  # Bucket ARNs the roles are scoped to are produced by s3-buckets; passing the
  # names (deterministic) avoids a cycle between iam and s3-buckets.
  landing_vagg_bucket_name = "llt-landing-vagg-${var.logging_account_id}"
  landing_cs_bucket_name   = "llt-landing-cs-${var.logging_account_id}"
  final_bucket_name        = "llt-final-${var.logging_account_id}"
  artifacts_bucket_name    = "llt-artifacts-${var.logging_account_id}"

  # SQS queue ARNs the aggregator roles consume from (deterministic names).
  landing_vagg_queue_name = "llt-landing-vagg-q"
  landing_cs_queue_name   = "llt-landing-cs-q"
  aws_region              = var.aws_region

  common_tags = local.common_tags
}

# --- Logging VPC + endpoints ---
# WHY: Home for aggregators, Cribl, and the S3 buckets' gateway endpoint. The
# Tier-1↔Tier-2 hop (S2/S4) stays inside this VPC via the Tier-2 NLBs, so this
# network isolates that inter-aggregator hop from any cross-account path.
module "logging_network" {
  source = "./modules/logging-network"
  providers = {
    aws = aws.logging
  }

  name_prefix = "llt"
  aws_region  = var.aws_region
  vpc_cidr    = "10.20.0.0/16"
  az_count    = 2
  enable_nat  = var.enable_logging_nat

  common_tags = local.common_tags
}

# --- S3 buckets (landing / final / artifacts) ---
# WHY: These are the S3 hops in the scenarios. Landing buckets are the S3
# intermediate hop for S3/S4; the final bucket is the terminal hop for all
# scenarios; artifacts carries SSM/Ansible transfer + results.
module "s3_buckets" {
  source = "./modules/s3-buckets"
  providers = {
    aws = aws.logging
  }

  name_prefix        = "llt"
  logging_account_id = var.logging_account_id
  expiration_days    = var.landing_final_expiration_days

  # Cross-account grant: generator-host roles (sender account) must PutObject to
  # the landing buckets for the S3/S4 data path (agent → landing S3).
  generator_host_role_arns = module.iam.generator_host_role_arns

  common_tags = local.common_tags
}

# --- SQS notify (S3 landing events → queues → event-driven aggregator pickup) ---
# WHY: PLAN §4.3 requires event-driven (not poll-interval) pickup from landing
# S3 so the "landing S3 → aggregator" hop latency reflects notification delay,
# not an arbitrary polling cadence.
module "sqs_notify" {
  source = "./modules/sqs-notify"
  providers = {
    aws = aws.logging
  }

  name_prefix        = "llt"
  logging_account_id = var.logging_account_id

  landing_vagg_bucket_id  = module.s3_buckets.landing_vagg_bucket_id
  landing_vagg_bucket_arn = module.s3_buckets.landing_vagg_bucket_arn
  landing_cs_bucket_id    = module.s3_buckets.landing_cs_bucket_id
  landing_cs_bucket_arn   = module.s3_buckets.landing_cs_bucket_arn

  common_tags = local.common_tags
}

# --- Vector aggregator stack (Tier-1 + Tier-2, each behind an internal NLB) ---
# WHY: One of the two aggregator technologies under test. Tier-1 fronts the
# agent→agg hop; Tier-2 fronts the agg→agg hop (S2/S4). Only Tier-1's NLB is
# exposed cross-account via PrivateLink.
module "vector_aggregator" {
  source = "./modules/vector-aggregator"
  providers = {
    aws = aws.logging
  }

  name_prefix        = "llt"
  vpc_id             = module.logging_network.vpc_id
  private_subnet_ids = module.logging_network.private_subnet_ids
  instance_type      = var.aggregator_instance_type

  instance_profile_name = module.iam.aggregator_instance_profile_name
  aggregator_sg_id      = module.logging_network.aggregator_sg_id

  common_tags = local.common_tags
}

# --- Cribl Stream stack (4 independent standalone single-instance nodes) ---
# WHY: The second aggregator technology under test. Cribl Free supports only a
# single worker group per leader, which can't represent two physically separate
# tiers, so there is no leader/control-plane node — t1/t2 are standalone nodes,
# each locally configured by Ansible, giving the same tiered NLB shape as the
# Vector stack so hop topology is identical and only the vendor differs.
module "cribl_stream" {
  source = "./modules/cribl-stream"
  providers = {
    aws = aws.logging
  }

  name_prefix           = "llt"
  vpc_id                = module.logging_network.vpc_id
  private_subnet_ids    = module.logging_network.private_subnet_ids
  worker_instance_type  = var.aggregator_instance_type
  instance_profile_name = module.iam.aggregator_instance_profile_name
  aggregator_sg_id      = module.logging_network.aggregator_sg_id

  common_tags = local.common_tags
}

# --- PrivateLink endpoint services (front the two Tier-1 NLBs) ---
# WHY: PLAN §4.3 — only Tier-1 NLBs are exposed to the sender account via
# PrivateLink; this is the cross-account edge for the agent→aggregator hop. The
# service names produced here are consumed by the sender VPC's interface
# endpoints, completing the cross-account link.
module "privatelink" {
  source = "./modules/privatelink"
  providers = {
    aws = aws.logging
  }

  name_prefix = "llt"

  # Only Tier-1 NLBs are fronted (agent-entry hop). Tier-2 stays in-VPC.
  vector_t1_nlb_arn = module.vector_aggregator.t1_nlb_arn
  cribl_t1_nlb_arn  = module.cribl_stream.t1_nlb_arn

  # Allowed principal = sender account root, so only our sender account can
  # create consumer endpoints against these services.
  allowed_principal_account_id = var.sender_account_id

  common_tags = local.common_tags
}

# ===========================================================================
# SENDER ACCOUNT
# ===========================================================================

# --- Sender VPC + endpoints (incl. PrivateLink consumer interface endpoints) ---
# WHY: Home of the generator hosts. Private-subnet-only data path; SSM endpoints
# for management; S3 gateway endpoint for the S3/S4 write path; and consumer
# interface endpoints against the logging account's PrivateLink services so the
# agent→aggregator hop crosses the account boundary exactly as designed.
module "sender_network" {
  source = "./modules/sender-network"
  providers = {
    aws = aws.sender
  }

  name_prefix = "llt"
  aws_region  = var.aws_region
  vpc_cidr    = "10.10.0.0/16"
  az_count    = 2
  enable_nat  = var.enable_sender_nat

  # Cross-account wiring: consume the endpoint-service names exported by the
  # logging account's privatelink module. For single-account dry runs these
  # resolve within the same account and still function.
  vector_aggregator_service_name = module.privatelink.vector_service_name
  cribl_aggregator_service_name  = module.privatelink.cribl_service_name

  common_tags = local.common_tags
}

# --- Generator hosts (2 Linux AL2023 + 2 Windows 2022) ---
# WHY: The event sources for the entire experiment. One Linux+Windows pair runs
# Vector agent (Stack=vector); the other pair runs Cribl Edge (Stack=cribl).
# Only the pair matching a run's `agent` dimension generates during that run.
module "generator_hosts" {
  source = "./modules/generator-hosts"
  providers = {
    aws = aws.sender
  }

  name_prefix        = "llt"
  instance_type      = var.generator_instance_type
  private_subnet_ids = module.sender_network.private_subnet_ids
  generator_sg_id    = module.sender_network.generator_sg_id

  instance_profile_name = module.iam.generator_instance_profile_name

  common_tags = local.common_tags
}
