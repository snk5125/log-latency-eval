########################################################################
# module: s3-buckets — variables
########################################################################

variable "name_prefix" {
  description = "Resource name prefix (PLAN §7: `llt`)."
  type        = string
}

variable "logging_account_id" {
  description = "LOGGING account ID — used as the bucket-name suffix (PLAN §4.3)."
  type        = string
}

variable "expiration_days" {
  description = "Lifecycle expiration in days for all buckets (PLAN §4.3: 7)."
  type        = number
  default     = 7
}

variable "generator_host_role_arns" {
  description = "Generator-host role ARNs (sender account) granted cross-account s3:PutObject on the landing buckets for the S3/S4 data path."
  type        = list(string)
}

variable "common_tags" {
  description = "Common tags applied to all resources (PLAN §7 base tag set)."
  type        = map(string)
}
