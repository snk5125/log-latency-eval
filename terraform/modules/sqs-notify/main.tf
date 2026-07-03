########################################################################
# module: sqs-notify
#
# ROLE IN THE EXPERIMENT:
#   Turns S3 landing-bucket writes into event-driven aggregator pickup (PLAN
#   §4.3). Each landing bucket emits an s3:ObjectCreated:* notification to its
#   own SQS queue; the aggregator S3 source (Vector aws_s3 / Cribl S3) consumes
#   that queue. WHY this matters for latency: it makes the "landing S3 →
#   aggregator" hop reflect true notification+pickup delay rather than an
#   arbitrary poll interval, which would otherwise dominate and confound S3/S4.
#
#   Each queue has:
#     - a DLQ (redrive after maxReceiveCount) so a poison object can't wedge the
#       source and silently drop events (which would corrupt loss-rate stats);
#     - visibility timeout 300s (PLAN spec) — long enough for the aggregator to
#       fetch+process a landing object before the message reappears;
#     - a queue policy allowing S3 (from the specific bucket ARN) to SendMessage.
########################################################################

# ---------------------------------------------------------------------------
# Dead-letter queues. WHY: isolate un-processable notifications so the main
# queue keeps flowing; a stuck message would otherwise skew pickup latency and
# loss-rate measurements for the affected run.
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "vagg_dlq" {
  name = "${var.name_prefix}-landing-vagg-dlq"
  tags = merge(var.common_tags, {
    Role  = "landing-dlq"
    Stack = "vector"
  })
}

resource "aws_sqs_queue" "cs_dlq" {
  name = "${var.name_prefix}-landing-cs-dlq"
  tags = merge(var.common_tags, {
    Role  = "landing-dlq"
    Stack = "cribl"
  })
}

# ---------------------------------------------------------------------------
# Main landing queues (one per aggregator technology).
# visibility_timeout_seconds = 300 per PLAN; redrive to the DLQ after 5 tries.
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "vagg" {
  name                       = "${var.name_prefix}-landing-vagg-q"
  visibility_timeout_seconds = 300 # PLAN spec: give the Vector source time to fetch+process

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.vagg_dlq.arn
    maxReceiveCount     = 5 # after 5 failed deliveries the notification is poison → DLQ
  })

  tags = merge(var.common_tags, {
    Role  = "landing-queue"
    Stack = "vector"
  })
}

resource "aws_sqs_queue" "cs" {
  name                       = "${var.name_prefix}-landing-cs-q"
  visibility_timeout_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.cs_dlq.arn
    maxReceiveCount     = 5
  })

  tags = merge(var.common_tags, {
    Role  = "landing-queue"
    Stack = "cribl"
  })
}

# ---------------------------------------------------------------------------
# Queue policies: allow the SPECIFIC landing bucket to SendMessage.
# WHY: S3 event notifications are delivered as the S3 service principal; the
# queue must explicitly allow it, scoped by SourceArn to the one bucket and by
# SourceAccount to the logging account, so no other bucket can inject events.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "vagg_queue_policy" {
  statement {
    sid    = "AllowS3SendMessage"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.vagg.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [var.landing_vagg_bucket_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.logging_account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "vagg" {
  queue_url = aws_sqs_queue.vagg.id
  policy    = data.aws_iam_policy_document.vagg_queue_policy.json
}

data "aws_iam_policy_document" "cs_queue_policy" {
  statement {
    sid    = "AllowS3SendMessage"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.cs.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [var.landing_cs_bucket_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.logging_account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "cs" {
  queue_url = aws_sqs_queue.cs.id
  policy    = data.aws_iam_policy_document.cs_queue_policy.json
}

# ---------------------------------------------------------------------------
# S3 → SQS notifications on ObjectCreated:*.
# WHY: fires the moment an agent lands an object, driving event-driven pickup.
# depends_on the queue policy so S3 can validate SendMessage at creation time.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket_notification" "vagg" {
  bucket = var.landing_vagg_bucket_id

  queue {
    queue_arn = aws_sqs_queue.vagg.arn
    events    = ["s3:ObjectCreated:*"] # any create (Put or multipart complete)
  }

  depends_on = [aws_sqs_queue_policy.vagg]
}

resource "aws_s3_bucket_notification" "cs" {
  bucket = var.landing_cs_bucket_id

  queue {
    queue_arn = aws_sqs_queue.cs.arn
    events    = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_sqs_queue_policy.cs]
}
