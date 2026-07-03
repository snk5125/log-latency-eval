########################################################################
# module: s3-buckets — outputs
########################################################################

output "landing_vagg_bucket_id" {
  description = "Vector landing bucket name/id."
  value       = aws_s3_bucket.landing_vagg.id
}

output "landing_vagg_bucket_arn" {
  description = "Vector landing bucket ARN (consumed by sqs-notify for the notification config)."
  value       = aws_s3_bucket.landing_vagg.arn
}

output "landing_cs_bucket_id" {
  description = "Cribl landing bucket name/id."
  value       = aws_s3_bucket.landing_cs.id
}

output "landing_cs_bucket_arn" {
  description = "Cribl landing bucket ARN (consumed by sqs-notify)."
  value       = aws_s3_bucket.landing_cs.arn
}

output "final_bucket_id" {
  description = "Final bucket name/id (terminal hop)."
  value       = aws_s3_bucket.final.id
}

output "artifacts_bucket_id" {
  description = "Artifacts bucket name/id."
  value       = aws_s3_bucket.artifacts.id
}
