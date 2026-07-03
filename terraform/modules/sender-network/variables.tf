########################################################################
# module: sender-network — variables
########################################################################

variable "name_prefix" {
  description = "Resource name prefix (PLAN §7: `llt`)."
  type        = string
}

variable "aws_region" {
  description = "AWS region — used to construct VPC endpoint service names."
  type        = string
}

variable "vpc_cidr" {
  description = "Sender VPC CIDR (PLAN §4.2: 10.10.0.0/16)."
  type        = string
  default     = "10.10.0.0/16"
}

variable "az_count" {
  description = "Number of AZs / private subnets to spread across (PLAN §4.2: 2)."
  type        = number
  default     = 2
}

variable "enable_nat" {
  description = "Provision the IGW/NAT stack for package-install egress (PLAN §4.2: install-only, disable after)."
  type        = bool
  default     = true
}

variable "vector_aggregator_service_name" {
  description = "PrivateLink endpoint-service name for the Vector Tier-1 aggregator (from the logging account's privatelink module)."
  type        = string
}

variable "cribl_aggregator_service_name" {
  description = "PrivateLink endpoint-service name for the Cribl Tier-1 aggregator (from the logging account's privatelink module)."
  type        = string
}

variable "common_tags" {
  description = "Common tags applied to all resources (PLAN §7 base tag set)."
  type        = map(string)
}
