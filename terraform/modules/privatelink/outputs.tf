########################################################################
# module: privatelink — outputs
#
# These service NAMES flow logging -> sender: the sender-network module's
# consumer interface endpoints are created against them, completing the
# cross-account agent→aggregator link.
########################################################################

output "vector_service_name" {
  description = "Vector aggregator endpoint-service name (consumed by the sender VPC interface endpoint)."
  value       = aws_vpc_endpoint_service.vector.service_name
}

output "cribl_service_name" {
  description = "Cribl aggregator endpoint-service name (consumed by the sender VPC interface endpoint)."
  value       = aws_vpc_endpoint_service.cribl.service_name
}
