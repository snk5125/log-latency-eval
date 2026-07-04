########################################################################
# module: s3-buckets
#
# ROLE IN THE EXPERIMENT:
#   Provisions the four S3 buckets that ARE the S3 hops (and the artifacts
#   store) of the latency test (PLAN §4.3):
#     - llt-landing-vagg-<acct> : S3/S4 intermediate hop for the Vector path
#     - llt-landing-cs-<acct>   : S3/S4 intermediate hop for the Cribl path
#     - llt-final-<acct>        : terminal hop for ALL scenarios
#     - llt-artifacts-<acct>    : SSM/Ansible transfer + harness + results
#
#   Every bucket: SSE-S3 (server-side encryption at rest), versioning OFF
#   (PLAN §4.3 — short-lived experiment data; versioning would inflate cost and
#   confuse "PutObject time" semantics used for S3-hop latency), public access
#   fully blocked, and a 7-day lifecycle expiry so test data self-cleans.
#
#   The landing buckets additionally carry a bucket policy granting cross-
#   account s3:PutObject to the sender-account generator-host role ARNs — this
#   is what makes the "agent → landing S3" hop (S3/S4) possible across accounts.
########################################################################

locals {
  # Account-id suffix per PLAN §4.3 (globally-unique, attributable names).
  landing_vagg_name = "${var.name_prefix}-landing-vagg-${var.logging_account_id}"
  landing_cs_name   = "${var.name_prefix}-landing-cs-${var.logging_account_id}"
  final_name        = "${var.name_prefix}-final-${var.logging_account_id}"
  artifacts_name    = "${var.name_prefix}-artifacts-${var.logging_account_id}"

  # All four buckets share the same hardening; iterate to keep it DRY and to
  # guarantee no bucket is accidentally left un-hardened.
  all_bucket_ids = {
    landing_vagg = aws_s3_bucket.landing_vagg.id
    landing_cs   = aws_s3_bucket.landing_cs.id
    final        = aws_s3_bucket.final.id
    artifacts    = aws_s3_bucket.artifacts.id
  }
}

# ---------------------------------------------------------------------------
# Buckets. WHY separate resources (not for_each): each has a distinct semantic
# role in the experiment and distinct downstream references (policies, SQS
# notifications), so explicit resources keep the wiring readable/reviewable.
# ---------------------------------------------------------------------------

# Vector landing bucket — S3/S4 intermediate hop, Vector path.
resource "aws_s3_bucket" "landing_vagg" {
  bucket = local.landing_vagg_name
  tags = merge(var.common_tags, {
    Role  = "landing"
    Stack = "vector"
  })
}

# Cribl landing bucket — S3/S4 intermediate hop, Cribl path.
resource "aws_s3_bucket" "landing_cs" {
  bucket = local.landing_cs_name
  tags = merge(var.common_tags, {
    Role  = "landing"
    Stack = "cribl"
  })
}

# Final bucket — terminal hop for every scenario (keyed final/{run_id}/{os}/...).
resource "aws_s3_bucket" "final" {
  bucket = local.final_name
  tags = merge(var.common_tags, {
    Role  = "final"
    Stack = "shared"
  })
}

# Artifacts bucket — SSM/Ansible transfer channel + harness + results/evidence.
resource "aws_s3_bucket" "artifacts" {
  bucket = local.artifacts_name
  tags = merge(var.common_tags, {
    Role  = "artifacts"
    Stack = "shared"
  })
}

# ---------------------------------------------------------------------------
# SSE-S3 encryption at rest on all four buckets.
# WHY: PLAN §4.3 mandates SSE-S3. Even for a test bed, encrypting at rest keeps
# the artifact aligned with the org's open/secure-by-default posture.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket_server_side_encryption_configuration" "sse" {
  for_each = local.all_bucket_ids
  bucket   = each.value

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3 (S3-managed keys), no KMS cost/complexity
    }
  }
}

# ---------------------------------------------------------------------------
# Versioning explicitly disabled on all buckets.
# WHY: PLAN §4.3 — "versioning off". S3-hop latency is derived from PutObject
# time; versioning would add noise and cost for zero experimental benefit.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket_versioning" "versioning" {
  for_each = local.all_bucket_ids
  bucket   = each.value

  versioning_configuration {
    status = "Disabled"
  }
}

# ---------------------------------------------------------------------------
# Block ALL public access on every bucket.
# WHY: these buckets carry experiment data and are reached only via VPC S3
# endpoints / cross-account roles; no public path should ever exist.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket_public_access_block" "pab" {
  for_each = local.all_bucket_ids
  bucket   = each.value

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# 7-day lifecycle expiry on every bucket.
# WHY: PLAN §4.3 — experiment data is ephemeral; expiring at 7 days keeps
# storage cost negligible and prevents stale runs from polluting analysis.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket_lifecycle_configuration" "expire" {
  for_each = local.all_bucket_ids
  bucket   = each.value

  rule {
    id     = "expire-experiment-data"
    status = "Enabled"

    filter {} # apply to all objects in the bucket

    expiration {
      days = var.expiration_days
    }

    # Also clean up incomplete multipart uploads (10 MB batches from the S3
    # sinks are multipart-eligible) so aborted uploads don't linger.
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# ---------------------------------------------------------------------------
# Landing-bucket policies: cross-account PutObject for generator-host roles.
# WHY: PLAN §4.3 — the landing buckets must accept s3:PutObject from the
# sender-account generator-host role(s) so the S3/S4 "agent → landing S3" hop
# works across the account boundary. Scoped to PutObject on that one bucket and
# to exactly the generator-host role principal(s).
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "landing_vagg_policy" {
  statement {
    sid    = "AllowGeneratorHostPut"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = var.generator_host_role_arns
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.landing_vagg.arn}/*"]
  }
}

resource "aws_s3_bucket_policy" "landing_vagg" {
  bucket = aws_s3_bucket.landing_vagg.id
  policy = data.aws_iam_policy_document.landing_vagg_policy.json

  # Public-access-block must exist first, else BlockPublicPolicy can reject.
  depends_on = [aws_s3_bucket_public_access_block.pab]
}

data "aws_iam_policy_document" "landing_cs_policy" {
  statement {
    sid    = "AllowGeneratorHostPut"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = var.generator_host_role_arns
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.landing_cs.arn}/*"]
  }
}

resource "aws_s3_bucket_policy" "landing_cs" {
  bucket     = aws_s3_bucket.landing_cs.id
  policy     = data.aws_iam_policy_document.landing_cs_policy.json
  depends_on = [aws_s3_bucket_public_access_block.pab]
}

# ---------------------------------------------------------------------------
# Artifacts-bucket policy: cross-account read/write for generator-host roles.
# WHY: cross-account S3 needs an allow on BOTH sides. The generator-host role
# (sender account) carries an identity policy for this bucket, but the bucket
# lives in the logging account — without a bucket policy naming that role,
# every instance-credential operation (eventgen fetch, per-run manifest
# upload, aws_ssm file transfer) gets AccessDenied in a two-account deploy.
# Scoped to object Get/Put + ListBucket, mirroring the identity policy in
# modules/iam exactly.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "artifacts_policy" {
  statement {
    sid    = "AllowGeneratorHostObjectRW"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = var.generator_host_role_arns
    }
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }

  statement {
    sid    = "AllowGeneratorHostList"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = var.generator_host_role_arns
    }
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
  }

  # WHY: the aws_ssm Ansible connection to SENDER-account hosts runs its
  # control-node S3 staging (GetBucketLocation / List / Get / Put on the
  # transfer objects) with the SENDER account's credentials, not the
  # generator-host role. Cross-account access therefore needs the sender
  # account root delegated on this logging-account bucket, or every SSM
  # connection to a sender host fails with AccessDenied on GetBucketLocation.
  # Delegating to the account root lets the sender account's own IAM govern
  # which of its principals (the operator profile) may use the bucket.
  statement {
    sid    = "AllowSenderAccountSsmTransfer"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.sender_account_id}:root"]
    }
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket     = aws_s3_bucket.artifacts.id
  policy     = data.aws_iam_policy_document.artifacts_policy.json
  depends_on = [aws_s3_bucket_public_access_block.pab]
}
