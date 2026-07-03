########################################################################
# module: logging-network — variables
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
  description = "Logging VPC CIDR (PLAN §4.3: 10.20.0.0/16)."
  type        = string
  default     = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of AZs / private subnets (PLAN §4.3: 2)."
  type        = number
  default     = 2
}

variable "enable_nat" {
  description = "Provision the IGW/NAT stack for package-install egress (install-only; disable after)."
  type        = bool
  default     = true
}

variable "common_tags" {
  description = "Common tags applied to all resources (PLAN §7 base tag set)."
  type        = map(string)
}
