########################################################################
# module: cribl-stream — variables
########################################################################

variable "name_prefix" {
  description = "Resource name prefix (PLAN §7: `llt`)."
  type        = string
}

variable "vpc_id" {
  description = "Logging VPC ID (target groups created here)."
  type        = string
}

variable "private_subnet_ids" {
  description = "Logging VPC private subnet IDs (standalone nodes, internal NLBs placed here)."
  type        = list(string)
}

variable "worker_instance_type" {
  description = "Instance type for the standalone nodes in tiers t1/t2 (PLAN §4.3: m6i.xlarge)."
  type        = string
  default     = "m6i.xlarge"
}

variable "instance_profile_name" {
  description = "Aggregator instance-profile name (same IAM pattern as Vector: SSM + S3 + SQS)."
  type        = string
}

variable "aggregator_sg_id" {
  description = "Shared aggregator security group ID (data-plane 8080/8081 ingress for the standalone nodes)."
  type        = string
}

variable "common_tags" {
  description = "Common tags applied to all resources (PLAN §7 base tag set)."
  type        = map(string)
}
