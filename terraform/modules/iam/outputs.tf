########################################################################
# module: iam — outputs
########################################################################

output "generator_instance_profile_name" {
  description = "Instance-profile name for generator hosts (attach to the 4 generator EC2s)."
  value       = aws_iam_instance_profile.generator_host.name
}

output "generator_host_role_arns" {
  description = "List of generator-host role ARNs — consumed by the landing-bucket policies to grant cross-account PutObject."
  value       = [aws_iam_role.generator_host.arn]
}

output "aggregator_instance_profile_name" {
  description = "Instance-profile name shared by Vector aggregators and Cribl workers/leader."
  value       = aws_iam_instance_profile.aggregator.name
}

output "aggregator_role_arn" {
  description = "Aggregator role ARN (logging account)."
  value       = aws_iam_role.aggregator.arn
}
