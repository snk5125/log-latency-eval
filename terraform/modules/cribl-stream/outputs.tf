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

output "leader_instance_id" {
  description = "Cribl leader instance ID (UI on :9000 via SSM port-forward only)."
  value       = aws_instance.leader.id
}

output "leader_private_ip" {
  description = "Cribl leader private IP — consumed by the Ansible cribl-worker role (cribl_leader_host) so workers can join the leader on :4200. Exported via scripts/gen-ansible-vars.sh."
  value       = aws_instance.leader.private_ip
}

output "t1_instance_ids" {
  description = "Worker-group-t1 instance IDs."
  value       = [for i in aws_instance.t1 : i.id]
}

output "t2_instance_ids" {
  description = "Worker-group-t2 instance IDs."
  value       = [for i in aws_instance.t2 : i.id]
}
