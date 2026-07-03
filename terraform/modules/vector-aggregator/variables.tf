########################################################################
# module: vector-aggregator — variables
########################################################################

variable "name_prefix" {
  description = "Resource name prefix (PLAN §7: `llt`)."
  type        = string
}

variable "vpc_id" {
  description = "Logging VPC ID (target groups are created here)."
  type        = string
}

variable "private_subnet_ids" {
  description = "Logging VPC private subnet IDs (instances + internal NLBs placed here)."
  type        = list(string)
}

variable "instance_type" {
  description = "Instance type for Tier-1 and Tier-2 nodes (PLAN §4.3: m6i.xlarge)."
  type        = string
  default     = "m6i.xlarge"
}

variable "instance_profile_name" {
  description = "Aggregator instance-profile name (SSM + S3 read landing/write final + SQS consume)."
  type        = string
}

variable "aggregator_sg_id" {
  description = "Shared aggregator security group ID (ingress 8080/8081 from VPC)."
  type        = string
}

variable "common_tags" {
  description = "Common tags applied to all resources (PLAN §7 base tag set)."
  type        = map(string)
}
