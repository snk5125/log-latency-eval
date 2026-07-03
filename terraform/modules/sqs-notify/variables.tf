########################################################################
# module: sqs-notify — variables
########################################################################

variable "name_prefix" {
  description = "Resource name prefix (PLAN §7: `llt`)."
  type        = string
}

variable "logging_account_id" {
  description = "LOGGING account ID — used in the queue policy SourceAccount condition."
  type        = string
}

variable "landing_vagg_bucket_id" {
  description = "Vector landing bucket id/name — target of the S3 notification config."
  type        = string
}

variable "landing_vagg_bucket_arn" {
  description = "Vector landing bucket ARN — SourceArn condition in the queue policy."
  type        = string
}

variable "landing_cs_bucket_id" {
  description = "Cribl landing bucket id/name — target of the S3 notification config."
  type        = string
}

variable "landing_cs_bucket_arn" {
  description = "Cribl landing bucket ARN — SourceArn condition in the queue policy."
  type        = string
}

variable "common_tags" {
  description = "Common tags applied to all resources (PLAN §7 base tag set)."
  type        = map(string)
}
