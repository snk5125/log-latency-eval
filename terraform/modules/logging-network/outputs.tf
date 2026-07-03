########################################################################
# module: logging-network — outputs
########################################################################

output "vpc_id" {
  description = "Logging VPC ID."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "Logging VPC CIDR (used by dependent SGs, e.g. the aggregator SG)."
  value       = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  description = "Private subnet IDs (aggregators, Cribl, NLBs placed here)."
  value       = aws_subnet.private[*].id
}

output "aggregator_sg_id" {
  description = "Shared aggregator security group ID (Vector + Cribl data-plane instances)."
  value       = aws_security_group.aggregator.id
}
