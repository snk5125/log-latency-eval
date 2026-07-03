########################################################################
# module: generator-hosts — variables
########################################################################

variable "name_prefix" {
  description = "Resource name prefix (PLAN §7: `llt`)."
  type        = string
}

variable "instance_type" {
  description = "Instance type for all 4 generator hosts (PLAN §4.2: m6i.large)."
  type        = string
  default     = "m6i.large"
}

variable "private_subnet_ids" {
  description = "Sender VPC private subnet IDs to place generator hosts in (no public IPs)."
  type        = list(string)
}

variable "generator_sg_id" {
  description = "Generator-host security group ID (no ingress; SSM-only)."
  type        = string
}

variable "instance_profile_name" {
  description = "Instance-profile name (SSM core + landing/artifacts PutObject) from the iam module."
  type        = string
}

variable "common_tags" {
  description = "Common tags applied to all resources (PLAN §7 base tag set)."
  type        = map(string)
}
