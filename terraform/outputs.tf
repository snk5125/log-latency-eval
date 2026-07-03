########################################################################
# outputs.tf — Root module outputs
#
# WHY THIS FILE EXISTS FOR THE EXPERIMENT:
#   Downstream automation (Ansible dynamic inventory context, the orchestrator
#   in harness/, and the analysis step) needs stable references to the deployed
#   endpoints, bucket names, and queue URLs. Exposing them as outputs lets the
#   run harness (PLAN §6 harness/) discover the test bed without re-deriving
#   names, and gives reviewers a single manifest of what was built.
########################################################################

# --- Networking ---
output "sender_vpc_id" {
  description = "SENDER VPC ID (generator hosts live here)."
  value       = module.sender_network.vpc_id
}

output "logging_vpc_id" {
  description = "LOGGING VPC ID (aggregators + S3 endpoint live here)."
  value       = module.logging_network.vpc_id
}

# --- Aggregator entry points (agent → aggregator hop targets) ---
output "vector_t1_nlb_dns" {
  description = "Vector Tier-1 internal NLB DNS name (in-VPC target; agents reach it cross-account via PrivateLink)."
  value       = module.vector_aggregator.t1_nlb_dns
}

output "vector_t2_nlb_dns" {
  description = "Vector Tier-2 internal NLB DNS name (Tier-1 → Tier-2 hop, S2/S4; stays in the logging VPC)."
  value       = module.vector_aggregator.t2_nlb_dns
}

output "cribl_t1_nlb_dns" {
  description = "Cribl Stream Tier-1 (worker group t1) internal NLB DNS name."
  value       = module.cribl_stream.t1_nlb_dns
}

output "cribl_t2_nlb_dns" {
  description = "Cribl Stream Tier-2 (worker group t2) internal NLB DNS name (in-VPC agg→agg hop)."
  value       = module.cribl_stream.t2_nlb_dns
}

# --- PrivateLink service names (cross-account edge; also useful for auditing) ---
output "vector_privatelink_service_name" {
  description = "Endpoint service name fronting the Vector Tier-1 NLB (consumed by the sender VPC)."
  value       = module.privatelink.vector_service_name
}

output "cribl_privatelink_service_name" {
  description = "Endpoint service name fronting the Cribl Tier-1 NLB (consumed by the sender VPC)."
  value       = module.privatelink.cribl_service_name
}

# --- Sender-side consumer endpoint DNS (what agents actually POST to) ---
output "sender_vector_endpoint_dns" {
  description = "Sender-account interface-endpoint DNS entries for the Vector aggregator PrivateLink service (agent→aggregator target for S1/S2)."
  value       = module.sender_network.vector_endpoint_dns_entries
}

output "sender_cribl_endpoint_dns" {
  description = "Sender-account interface-endpoint DNS entries for the Cribl aggregator PrivateLink service."
  value       = module.sender_network.cribl_endpoint_dns_entries
}

# --- S3 buckets (the S3 hops + artifacts store) ---
output "landing_vagg_bucket" {
  description = "Vector landing bucket name (S3/S4 intermediate hop for the Vector aggregator path)."
  value       = module.s3_buckets.landing_vagg_bucket_id
}

output "landing_cs_bucket" {
  description = "Cribl landing bucket name (S3/S4 intermediate hop for the Cribl path)."
  value       = module.s3_buckets.landing_cs_bucket_id
}

output "final_bucket" {
  description = "Final destination bucket name (terminal hop for all scenarios)."
  value       = module.s3_buckets.final_bucket_id
}

output "artifacts_bucket" {
  description = "Artifacts bucket name (SSM/Ansible transfer, harness distribution, results)."
  value       = module.s3_buckets.artifacts_bucket_id
}

# --- SQS queues (event-driven landing pickup) ---
output "landing_vagg_queue_url" {
  description = "SQS queue URL for Vector landing-bucket ObjectCreated events (event-driven pickup)."
  value       = module.sqs_notify.landing_vagg_queue_url
}

output "landing_cs_queue_url" {
  description = "SQS queue URL for Cribl landing-bucket ObjectCreated events (event-driven pickup)."
  value       = module.sqs_notify.landing_cs_queue_url
}

# --- Generator hosts (event sources) ---
output "generator_instance_ids" {
  description = "Map of generator host logical name -> instance ID (used by the SSM-driven orchestrator to start/stop generators per run)."
  value       = module.generator_hosts.instance_ids
}

# --- Cribl leader (UI reachable via SSM port-forward only) ---
output "cribl_leader_instance_id" {
  description = "Cribl Stream leader instance ID (UI on :9000 reachable only via SSM port-forward, no ingress)."
  value       = module.cribl_stream.leader_instance_id
}

output "cribl_leader_private_ip" {
  description = "Cribl Stream leader private IP — bridged into Ansible as cribl_leader_private_ip (workers join on :4200) by scripts/gen-ansible-vars.sh."
  value       = module.cribl_stream.leader_private_ip
}
