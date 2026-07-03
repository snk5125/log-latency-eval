########################################################################
# module: sender-network — outputs
########################################################################

output "vpc_id" {
  description = "Sender VPC ID."
  value       = aws_vpc.this.id
}

output "private_subnet_ids" {
  description = "Private subnet IDs (generator hosts are placed here)."
  value       = aws_subnet.private[*].id
}

output "generator_sg_id" {
  description = "Generator-host security group ID."
  value       = aws_security_group.generator.id
}

output "vector_endpoint_dns_entries" {
  description = "DNS entries of the Vector aggregator consumer interface endpoint (agents POST here for the Vector path)."
  value       = aws_vpc_endpoint.vector_aggregator.dns_entry
}

output "cribl_endpoint_dns_entries" {
  description = "DNS entries of the Cribl aggregator consumer interface endpoint (agents POST here for the Cribl path)."
  value       = aws_vpc_endpoint.cribl_aggregator.dns_entry
}
