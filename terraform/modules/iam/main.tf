########################################################################
# module: iam
#
# ROLE IN THE EXPERIMENT:
#   Central, least-privilege IAM for the whole latency test bed. Two host roles
#   are defined here so their ARNs are stable and can be referenced by the S3
#   bucket policies (cross-account PutObject grants) without a dependency cycle:
#
#     1. generator-host role  (SENDER account) — SSM managed + PutObject to the
#        landing/artifacts buckets. Generators write directly to landing S3 for
#        the S3/S4 data path and upload results/artifacts.
#     2. aggregator role      (LOGGING account) — SSM managed + read landing /
#        write final + consume the SQS landing queues (event-driven pickup).
#
#   Every statement below carries a comment naming WHY the permission exists for
#   the experiment (PLAN §7: least-privilege, comment every statement's purpose).
#
#   The generator-host role is created with the SENDER provider; the aggregator
#   role with the LOGGING provider — matching where each set of hosts lives.
########################################################################

# ---------------------------------------------------------------------------
# Deterministic ARNs for the buckets/queues these roles are scoped to. We build
# them from names (passed in) rather than importing the s3-buckets/sqs modules
# to avoid a module dependency cycle (s3-buckets depends on this module's role
# ARNs for its bucket policy).
# ---------------------------------------------------------------------------
locals {
  landing_vagg_bucket_arn = "arn:aws:s3:::${var.landing_vagg_bucket_name}"
  landing_cs_bucket_arn   = "arn:aws:s3:::${var.landing_cs_bucket_name}"
  final_bucket_arn        = "arn:aws:s3:::${var.final_bucket_name}"
  artifacts_bucket_arn    = "arn:aws:s3:::${var.artifacts_bucket_name}"

  landing_vagg_queue_arn = "arn:aws:sqs:${var.aws_region}:${var.logging_account_id}:${var.landing_vagg_queue_name}"
  landing_cs_queue_arn   = "arn:aws:sqs:${var.aws_region}:${var.logging_account_id}:${var.landing_cs_queue_name}"
}

# ===========================================================================
# GENERATOR-HOST ROLE (SENDER account)
# ===========================================================================

# Trust policy: EC2 assumes this role. WHY: generator hosts are EC2 instances
# that need an instance profile to talk to SSM and S3 without static keys.
data "aws_iam_policy_document" "generator_assume" {
  statement {
    sid     = "EC2AssumeRole"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "generator_host" {
  provider = aws.sender # generator hosts live in the sender account

  name               = "${var.name_prefix}-generator-host-role"
  assume_role_policy = data.aws_iam_policy_document.generator_assume.json

  tags = merge(var.common_tags, {
    Role  = "generator"
    Stack = "shared"
  })
}

# SSM core managed policy. WHY: PLAN §4.2 mandates SSM-only management (no SSH/
# WinRM). This grants the Session Manager / Run Command agent permissions the
# orchestrator uses to start/stop generators and run Ansible over aws_ssm.
resource "aws_iam_role_policy_attachment" "generator_ssm_core" {
  provider   = aws.sender
  role       = aws_iam_role.generator_host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Inline S3 policy for generators — landing PutObject + artifacts read/write.
data "aws_iam_policy_document" "generator_s3" {
  # WHY: S3/S4 data path — the on-host agent writes event batches directly to
  # the landing buckets (in the logging account). Scoped to object PUT only.
  statement {
    sid    = "PutToLandingBuckets"
    effect = "Allow"
    actions = [
      "s3:PutObject",
    ]
    resources = [
      "${local.landing_vagg_bucket_arn}/*",
      "${local.landing_cs_bucket_arn}/*",
    ]
  }

  # WHY: artifacts bucket is the SSM/Ansible file-transfer channel (aws_ssm
  # connection plugin, PLAN §4.2) and where generators fetch the harness and
  # upload per-run results/evidence.
  statement {
    sid    = "ReadWriteArtifacts"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = [
      "${local.artifacts_bucket_arn}/*",
    ]
  }

  # WHY: the aws_ssm Ansible connection plugin and harness need to list the
  # artifacts bucket to locate transferred files; scoped to that one bucket.
  statement {
    sid    = "ListArtifactsBucket"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
    ]
    resources = [
      local.artifacts_bucket_arn,
    ]
  }
}

resource "aws_iam_role_policy" "generator_s3" {
  provider = aws.sender
  name     = "${var.name_prefix}-generator-s3"
  role     = aws_iam_role.generator_host.id
  policy   = data.aws_iam_policy_document.generator_s3.json
}

resource "aws_iam_instance_profile" "generator_host" {
  provider = aws.sender
  name     = "${var.name_prefix}-generator-host-profile"
  role     = aws_iam_role.generator_host.name

  tags = merge(var.common_tags, {
    Role  = "generator"
    Stack = "shared"
  })
}

# ===========================================================================
# AGGREGATOR ROLE (LOGGING account) — Vector aggregators + Cribl workers/leader
# ===========================================================================

data "aws_iam_policy_document" "aggregator_assume" {
  statement {
    sid     = "EC2AssumeRole"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "aggregator" {
  provider = aws.logging # aggregators live in the logging account

  name               = "${var.name_prefix}-aggregator-role"
  assume_role_policy = data.aws_iam_policy_document.aggregator_assume.json

  tags = merge(var.common_tags, {
    Role  = "aggregator"
    Stack = "shared"
  })
}

# SSM core. WHY: aggregators are also managed SSM-only (Ansible config push,
# port-forward to Cribl UI). Same rationale as the generator role.
resource "aws_iam_role_policy_attachment" "aggregator_ssm_core" {
  provider   = aws.logging
  role       = aws_iam_role.aggregator.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "aggregator_s3_sqs" {
  # WHY: S3/S4 — aggregator S3 sources read event batches back out of the
  # landing buckets after the agent wrote them. Read-only on landing objects.
  statement {
    sid    = "ReadLandingBuckets"
    effect = "Allow"
    actions = [
      "s3:GetObject",
    ]
    resources = [
      "${local.landing_vagg_bucket_arn}/*",
      "${local.landing_cs_bucket_arn}/*",
    ]
  }

  # WHY: aggregator writes the terminal object to the final bucket (last hop of
  # every scenario). Object PUT only, scoped to the final bucket.
  statement {
    sid    = "WriteFinalBucket"
    effect = "Allow"
    actions = [
      "s3:PutObject",
    ]
    resources = [
      "${local.final_bucket_arn}/*",
    ]
  }

  # WHY: list permission on landing/final so the S3 sources/sinks can enumerate
  # keys; scoped to just the buckets they touch.
  statement {
    sid    = "ListDataBuckets"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
    ]
    resources = [
      local.landing_vagg_bucket_arn,
      local.landing_cs_bucket_arn,
      local.final_bucket_arn,
    ]
  }

  # WHY: read/write artifacts for config/harness distribution + evidence upload.
  statement {
    sid    = "ReadWriteArtifacts"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
    ]
    resources = [
      local.artifacts_bucket_arn,
      "${local.artifacts_bucket_arn}/*",
    ]
  }

  # WHY: PLAN §4.3 — aggregator S3 sources consume S3 ObjectCreated events via
  # SQS for event-driven (not polling) landing pickup. Consume = receive/delete
  # + read attributes on exactly the two landing queues.
  statement {
    sid    = "ConsumeLandingQueues"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = [
      local.landing_vagg_queue_arn,
      local.landing_cs_queue_arn,
    ]
  }
}

resource "aws_iam_role_policy" "aggregator_s3_sqs" {
  provider = aws.logging
  name     = "${var.name_prefix}-aggregator-s3-sqs"
  role     = aws_iam_role.aggregator.id
  policy   = data.aws_iam_policy_document.aggregator_s3_sqs.json
}

resource "aws_iam_instance_profile" "aggregator" {
  provider = aws.logging
  name     = "${var.name_prefix}-aggregator-profile"
  role     = aws_iam_role.aggregator.name

  tags = merge(var.common_tags, {
    Role  = "aggregator"
    Stack = "shared"
  })
}
