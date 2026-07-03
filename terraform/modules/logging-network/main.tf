########################################################################
# module: logging-network
#
# ROLE IN THE EXPERIMENT:
#   The LOGGING account network (PLAN §4.3). Home to the Vector aggregator
#   tiers, the standalone Cribl Stream nodes, their internal NLBs, and the
#   S3 gateway endpoint. Private-subnet-only on the data path; SSM-only
#   management (no SSH ingress). The Tier-1↔Tier-2 hop (S2/S4) stays entirely
#   inside this VPC (via the Tier-2 NLBs), so this network isolates the
#   inter-aggregator hop from any cross-account path.
#   A NAT gateway is OPTIONAL (enable_nat) for one-time package install only.
########################################################################

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # Select AZs by ZONE ID, not name — must match sender-network's selection so
  # both accounts land in the same physical AZ pair (AZ names shuffle per
  # account; PrivateLink endpoint AZs must align, PLAN §4.6(7)).
  az_name_by_id = zipmap(
    data.aws_availability_zones.available.zone_ids,
    data.aws_availability_zones.available.names,
  )
  azs = [
    for id in slice(sort(data.aws_availability_zones.available.zone_ids), 0, var.az_count) :
    local.az_name_by_id[id]
  ]
  private_subnet_cidrs = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnet_cidrs  = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, 240 + i)]
}

# ---------------------------------------------------------------------------
# VPC 10.20.0.0/16. WHY: dedicated logging-side network; distinct CIDR from the
# sender VPC (10.10/16) so any future peering/inspection is unambiguous.
# ---------------------------------------------------------------------------
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-vpc"
  })
}

# Private subnets — aggregators, Cribl, NLB nodes, endpoint ENIs live here.
resource "aws_subnet" "private" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.private_subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false # no public IPs on aggregators

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-private-${count.index + 1}"
    Tier = "private"
  })
}

# --- OPTIONAL NAT stack (package install only) ---
resource "aws_internet_gateway" "this" {
  count  = var.enable_nat ? 1 : 0
  vpc_id = aws_vpc.this.id
  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-igw"
  })
}

resource "aws_subnet" "public" {
  count             = var.enable_nat ? var.az_count : 0
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.public_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-public-${count.index + 1}"
    Tier = "public-nat"
  })
}

resource "aws_eip" "nat" {
  count  = var.enable_nat ? 1 : 0
  domain = "vpc"
  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-nat-eip"
  })
}

resource "aws_nat_gateway" "this" {
  count         = var.enable_nat ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-nat"
  })
  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  count  = var.enable_nat ? 1 : 0
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this[0].id
  }
  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-rt-public"
  })
}

resource "aws_route_table_association" "public" {
  count          = var.enable_nat ? var.az_count : 0
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-rt-private"
  })
}

resource "aws_route" "private_nat" {
  count                  = var.enable_nat ? 1 : 0
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------------------
# S3 gateway endpoint. WHY: PLAN §4.3 — aggregators read landing objects and
# write final objects over the private S3 path (no NAT dependency for data).
# ---------------------------------------------------------------------------
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-s3-gw"
  })
}

# ---------------------------------------------------------------------------
# Endpoint SG (HTTPS 443 from within the VPC) for the SSM interface endpoints.
# ---------------------------------------------------------------------------
resource "aws_security_group" "endpoints" {
  name        = "${var.name_prefix}-logging-endpoints-sg"
  description = "Allow HTTPS (443) from the logging VPC to SSM interface endpoints."
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS from within the logging VPC to endpoints"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Allow all egress from endpoint ENIs (responses)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-endpoints-sg"
  })
}

# SSM interface endpoints (ssm, ssmmessages, ec2messages) — SSM-only management.
resource "aws_vpc_endpoint" "ssm" {
  for_each = toset(["ssm", "ssmmessages", "ec2messages"])

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-${each.value}-ep"
  })
}

# ---------------------------------------------------------------------------
# Aggregator security group (shared by Vector aggregators + standalone Cribl nodes).
# WHY these specific ingress rules:
#   - 8080 (Tier-1) and 8081 (Tier-2) from within the VPC: the NLBs forward
#     agent/inter-aggregator HTTP POSTs to the instances; NLBs preserve source
#     IP, and cross-account PrivateLink traffic arrives from the endpoint ENIs
#     inside this VPC, so VPC-CIDR scoping covers both in-VPC and PrivateLink.
#   - Standalone Cribl Stream nodes need no control-plane ports (no leader,
#     no worker→leader channel); this shared SG covers their 8080/8081 ingress.
#   No SSH ingress (SSM-only). Egress open for S3/SSM/inter-tier POSTs.
# ---------------------------------------------------------------------------
resource "aws_security_group" "aggregator" {
  name        = "${var.name_prefix}-logging-aggregator-sg"
  description = "Aggregators: ingress 8080/8081 from VPC (NLB + PrivateLink), SSM-only mgmt, open egress."
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "Tier-1 HTTP listener (agent to aggregator hop) from within VPC / PrivateLink ENIs"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  ingress {
    description = "Tier-2 HTTP listener (aggregator to aggregator hop, S2/S4) from within VPC"
    from_port   = 8081
    to_port     = 8081
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "All egress (S3 endpoint, SSM, inter-tier POST, package install via NAT)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-logging-aggregator-sg"
  })
}
