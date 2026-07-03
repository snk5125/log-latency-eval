########################################################################
# module: privatelink
#
# ROLE IN THE EXPERIMENT:
#   Publishes the cross-account edge for the agent → aggregator hop (PLAN §4.3).
#   Creates one VPC endpoint SERVICE per aggregator technology, each fronting
#   ONLY that technology's Tier-1 NLB:
#     - Vector Tier-1 NLB  -> Vector endpoint service
#     - Cribl  Tier-1 NLB  -> Cribl  endpoint service
#   Tier-2 NLBs are deliberately NOT fronted: PLAN §4.3 keeps Tier-1→Tier-2
#   traffic inside the logging VPC, so only the agent-entry tier is reachable
#   cross-account. The endpoint-service NAMES are exported and consumed by the
#   sender VPC's interface endpoints, completing the PrivateLink link.
#
#   allowed principal = SENDER account root, so only our sender account can
#   create consumer endpoints against these services (least exposure).
#
#   SINGLE-ACCOUNT DRY RUN: when the allowed principal account == the logging
#   account, this collapses to a same-account PrivateLink loopback and still
#   applies cleanly.
########################################################################

# ---------------------------------------------------------------------------
# Vector Tier-1 endpoint service.
# WHY acceptance_required = false: this is an automated test bed; auto-accepting
# the sender endpoint avoids a manual approval step in the deploy workflow. The
# allowed-principals allowlist below still restricts WHO may connect.
# ---------------------------------------------------------------------------
resource "aws_vpc_endpoint_service" "vector" {
  acceptance_required        = false
  network_load_balancer_arns = [var.vector_t1_nlb_arn]

  tags = merge(var.common_tags, {
    Name  = "${var.name_prefix}-vagg-endpoint-service"
    Role  = "agg-t1"
    Stack = "vector"
  })
}

# Restrict to the sender account root — only it may create a consumer endpoint.
resource "aws_vpc_endpoint_service_allowed_principal" "vector" {
  vpc_endpoint_service_id = aws_vpc_endpoint_service.vector.id
  principal_arn           = "arn:aws:iam::${var.allowed_principal_account_id}:root"
}

# ---------------------------------------------------------------------------
# Cribl Tier-1 endpoint service (identical shape, Cribl NLB).
# ---------------------------------------------------------------------------
resource "aws_vpc_endpoint_service" "cribl" {
  acceptance_required        = false
  network_load_balancer_arns = [var.cribl_t1_nlb_arn]

  tags = merge(var.common_tags, {
    Name  = "${var.name_prefix}-cs-endpoint-service"
    Role  = "agg-t1"
    Stack = "cribl"
  })
}

resource "aws_vpc_endpoint_service_allowed_principal" "cribl" {
  vpc_endpoint_service_id = aws_vpc_endpoint_service.cribl.id
  principal_arn           = "arn:aws:iam::${var.allowed_principal_account_id}:root"
}
