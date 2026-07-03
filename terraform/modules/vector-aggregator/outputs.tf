########################################################################
# module: vector-aggregator — outputs
########################################################################

output "t1_nlb_arn" {
  description = "Tier-1 NLB ARN — consumed by the privatelink module (Tier-1 is the only tier exposed cross-account)."
  value       = aws_lb.t1.arn
}

output "t1_nlb_dns" {
  description = "Tier-1 NLB DNS name (in-VPC target for the agent→aggregator hop)."
  value       = aws_lb.t1.dns_name
}

output "t2_nlb_arn" {
  description = "Tier-2 NLB ARN (in-VPC inter-aggregator hop; NOT exposed cross-account)."
  value       = aws_lb.t2.arn
}

output "t2_nlb_dns" {
  description = "Tier-2 NLB DNS name (Tier-1→Tier-2 hop target, S2/S4)."
  value       = aws_lb.t2.dns_name
}

output "t1_instance_ids" {
  description = "Tier-1 Vector aggregator instance IDs."
  value       = [for i in aws_instance.t1 : i.id]
}

output "t2_instance_ids" {
  description = "Tier-2 Vector aggregator instance IDs."
  value       = [for i in aws_instance.t2 : i.id]
}
