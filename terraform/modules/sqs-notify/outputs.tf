########################################################################
# module: sqs-notify — outputs
########################################################################

output "landing_vagg_queue_url" {
  description = "Vector landing queue URL (Vector aws_s3 source consumes this)."
  value       = aws_sqs_queue.vagg.id
}

output "landing_vagg_queue_arn" {
  description = "Vector landing queue ARN."
  value       = aws_sqs_queue.vagg.arn
}

output "landing_cs_queue_url" {
  description = "Cribl landing queue URL (Cribl S3 source consumes this)."
  value       = aws_sqs_queue.cs.id
}

output "landing_cs_queue_arn" {
  description = "Cribl landing queue ARN."
  value       = aws_sqs_queue.cs.arn
}
