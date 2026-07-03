########################################################################
# module: cribl-stream — variables
########################################################################

variable "name_prefix" {
  description = "Resource name prefix (PLAN §7: `llt`)."
  type        = string
}

variable "vpc_id" {
  description = "Logging VPC ID (target groups + leader SG created here)."
  type        = string
}

variable "vpc_cidr" {
  description = "Logging VPC CIDR — scopes the Cribl UI (9000) ingress to in-VPC only (reached via SSM port-forward)."
  type        = string
}

variable "private_subnet_ids" {
  description = "Logging VPC private subnet IDs (leader, workers, internal NLBs placed here)."
  type        = list(string)
}

variable "worker_instance_type" {
  description = "Instance type for worker groups t1/t2 (PLAN §4.3: m6i.xlarge)."
  type        = string
  default     = "m6i.xlarge"
}

variable "leader_instance_type" {
  description = "Instance type for the Cribl leader (PLAN §4.3: m6i.large; control plane only)."
  type        = string
  default     = "m6i.large"
}

variable "instance_profile_name" {
  description = "Aggregator instance-profile name (same IAM pattern as Vector: SSM + S3 + SQS)."
  type        = string
}

variable "aggregator_sg_id" {
  description = "Shared aggregator security group ID (data-plane 8080/8081; also the worker principal allowed to reach leader:4200)."
  type        = string
}

variable "common_tags" {
  description = "Common tags applied to all resources (PLAN §7 base tag set)."
  type        = map(string)
}
