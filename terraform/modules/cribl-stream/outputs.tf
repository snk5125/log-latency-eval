########################################################################
# module: cribl-stream — outputs
########################################################################

output "t1_nlb_arn" {
  description = "Worker-group-t1 NLB ARN — consumed by the privatelink module (only Tier-1 is exposed cross-account)."
  value       = aws_lb.t1.arn
}

output "t1_nlb_dns" {
  description = "Worker-group-t1 NLB DNS name (agent→aggregator hop entry)."
  value       = aws_lb.t1.dns_name
}

output "t2_nlb_arn" {
  description = "Worker-group-t2 NLB ARN (in-VPC inter-aggregator hop)."
  value       = aws_lb.t2.arn
}

output "t2_nlb_dns" {
  description = "Worker-group-t2 NLB DNS name (Tier-1→Tier-2 hop, S2/S4)."
  value       = aws_lb.t2.dns_name
}

output "t1_instance_ids" {
  description = "Worker-group-t1 instance IDs."
  value       = [for i in aws_instance.t1 : i.id]
}

output "t2_instance_ids" {
  description = "Worker-group-t2 instance IDs."
  value       = [for i in aws_instance.t2 : i.id]
}
