########################################################################
# module: privatelink — variables
########################################################################

variable "name_prefix" {
  description = "Resource name prefix (PLAN §7: `llt`)."
  type        = string
}

variable "vector_t1_nlb_arn" {
  description = "Vector Tier-1 NLB ARN to front with an endpoint service (agent-entry hop)."
  type        = string
}

variable "cribl_t1_nlb_arn" {
  description = "Cribl Tier-1 (worker group t1) NLB ARN to front with an endpoint service."
  type        = string
}

variable "allowed_principal_account_id" {
  description = "Account ID allowed to create consumer endpoints (SENDER account). May equal the logging account for single-account dry runs."
  type        = string
}

variable "common_tags" {
  description = "Common tags applied to all resources (PLAN §7 base tag set)."
  type        = map(string)
}
