########################################################################
# module: iam — variables
########################################################################

variable "name_prefix" {
  description = "Resource name prefix (PLAN §7: `llt`)."
  type        = string
}

variable "sender_account_id" {
  description = "SENDER account ID (owns the generator-host role)."
  type        = string
}

variable "logging_account_id" {
  description = "LOGGING account ID (owns the aggregator role; used to build SQS queue ARNs)."
  type        = string
}

variable "aws_region" {
  description = "AWS region — used to construct the SQS queue ARNs the aggregator role consumes from."
  type        = string
}

variable "landing_vagg_bucket_name" {
  description = "Vector landing bucket name — scopes generator PutObject and aggregator GetObject."
  type        = string
}

variable "landing_cs_bucket_name" {
  description = "Cribl landing bucket name — scopes generator PutObject and aggregator GetObject."
  type        = string
}

variable "final_bucket_name" {
  description = "Final bucket name — scopes aggregator PutObject (terminal hop)."
  type        = string
}

variable "artifacts_bucket_name" {
  description = "Artifacts bucket name — scopes SSM/Ansible transfer + results read/write."
  type        = string
}

variable "landing_vagg_queue_name" {
  description = "Vector landing SQS queue name — scopes aggregator consume permissions."
  type        = string
}

variable "landing_cs_queue_name" {
  description = "Cribl landing SQS queue name — scopes aggregator consume permissions."
  type        = string
}

variable "common_tags" {
  description = "Common tags applied to all resources (PLAN §7 base tag set)."
  type        = map(string)
}
