########################################################################
# module: sender-network
#
# ROLE IN THE EXPERIMENT:
#   The SENDER account network (PLAN §4.2). Home to the generator hosts. It is
#   deliberately private-subnet-only on the data path: no IGW, no public IPs.
#   Everything the experiment needs reaches AWS via VPC endpoints:
#     - SSM interface endpoints (ssm, ssmmessages, ec2messages) — the ONLY
#       management path (PLAN §4.2: no SSH/WinRM).
#     - S3 gateway endpoint — the S3/S4 write path (agent → landing S3) and the
#       SSM/Ansible file-transfer channel.
#     - PrivateLink *consumer* interface endpoints against the two logging-
#       account aggregator endpoint services — the cross-account agent→aggregator
#       hop for S1/S2 (and the T1 entry for S3/S4 after the S3 read).
#   A NAT gateway is OPTIONAL (enable_nat) and exists SOLELY for package install
#   (PLAN §4.2); disable it after provisioning so the data path is fully private.
########################################################################

# Availability zones in-region; take the first az_count for subnet spread.
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # Select AZs by ZONE ID, not name: AZ names map to different physical zones
  # per account, so name-based selection across the two accounts can land the
  # PrivateLink consumer endpoints in AZs the endpoint services don't support
  # (apply-time failure) or silently place the accounts in different physical
  # AZs — an unmodeled cross-AZ hop violating PLAN §4.6(7). Sorting zone IDs
  # gives both network modules the same deterministic physical pair.
  az_name_by_id = zipmap(
    data.aws_availability_zones.available.zone_ids,
    data.aws_availability_zones.available.names,
  )
  azs = [
    for id in slice(sort(data.aws_availability_zones.available.zone_ids), 0, var.az_count) :
    local.az_name_by_id[id]
  ]

  # Private subnets: /20 blocks carved from the /16, one per AZ.
  # WHY /20: ample host space for the small host count while leaving room to add
  # public (NAT) subnets from a non-overlapping range below.
  private_subnet_cidrs = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i)]

  # Public (NAT-only) subnets: carved from the high end of the /16 so they never
  # overlap the private /20s. Only created when enable_nat = true.
  public_subnet_cidrs = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, 240 + i)]
}

# ---------------------------------------------------------------------------
# VPC 10.10.0.0/16. WHY: dedicated sender-side network isolates generator-host
# egress and makes the cross-account hop explicit.
# ---------------------------------------------------------------------------
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true # required for interface-endpoint private DNS
  enable_dns_hostnames = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-vpc"
  })
}

# Private subnets (generator hosts live here; no public IPs).
resource "aws_subnet" "private" {
  count             = var.az_count
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  map_public_ip_on_launch = false # PLAN §4.2: no public IPs on generator hosts

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-private-${count.index + 1}"
    Tier = "private"
  })
}

# ---------------------------------------------------------------------------
# OPTIONAL NAT path (package install only). IGW + public subnets + NAT GW.
# WHY: generator hosts need outbound internet ONCE to install agents/packages.
# Gated behind enable_nat so the operator can destroy it afterward, leaving a
# fully private data path per PLAN §4.2.
# ---------------------------------------------------------------------------
resource "aws_internet_gateway" "this" {
  count  = var.enable_nat ? 1 : 0
  vpc_id = aws_vpc.this.id

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-igw"
  })
}

resource "aws_subnet" "public" {
  count             = var.enable_nat ? var.az_count : 0
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.public_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-public-${count.index + 1}"
    Tier = "public-nat"
  })
}

resource "aws_eip" "nat" {
  count  = var.enable_nat ? 1 : 0
  domain = "vpc"

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-nat-eip"
  })
}

# Single NAT GW (cost-minimizing; this is install-only, not the data path).
resource "aws_nat_gateway" "this" {
  count         = var.enable_nat ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-nat"
  })

  depends_on = [aws_internet_gateway.this]
}

# Public route table → IGW (only when NAT stack exists).
resource "aws_route_table" "public" {
  count  = var.enable_nat ? 1 : 0
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this[0].id
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-rt-public"
  })
}

resource "aws_route_table_association" "public" {
  count          = var.enable_nat ? var.az_count : 0
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

# ---------------------------------------------------------------------------
# Private route table. Default route → NAT only while enable_nat is true;
# when NAT is off, the table has no 0.0.0.0/0 route (fully private) and traffic
# to S3/SSM flows via the gateway/interface endpoints.
# ---------------------------------------------------------------------------
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-rt-private"
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
# S3 gateway endpoint. WHY: PLAN §4.2 — the S3/S4 write path and SSM file
# transfer must stay on AWS private networking (no NAT dependency for data).
# ---------------------------------------------------------------------------
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-s3-gw"
  })
}

# ---------------------------------------------------------------------------
# Security group for the SSM interface endpoints — allow HTTPS from within the
# VPC. WHY: the ssm/ssmmessages/ec2messages endpoints terminate TLS on :443;
# hosts in the VPC must reach them. Scoped to the VPC CIDR only. (The
# aggregator PrivateLink endpoints use their own SG below — data port 8080.)
# ---------------------------------------------------------------------------
resource "aws_security_group" "endpoints" {
  name        = "${var.name_prefix}-sender-endpoints-sg"
  description = "Allow HTTPS (443) from the sender VPC to the SSM interface endpoints."
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS from within the sender VPC to endpoints"
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
    Name = "${var.name_prefix}-sender-endpoints-sg"
  })
}

# ---------------------------------------------------------------------------
# SSM interface endpoints (ssm, ssmmessages, ec2messages).
# WHY: PLAN §4.2 — SSM is the ONLY management path. Private DNS on so the SSM
# agent resolves the AWS service names to the endpoint ENIs automatically.
# ---------------------------------------------------------------------------
resource "aws_vpc_endpoint" "ssm" {
  for_each = toset(["ssm", "ssmmessages", "ec2messages"])

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-${each.value}-ep"
  })
}

# ---------------------------------------------------------------------------
# Security group for the AGGREGATOR PrivateLink consumer endpoints.
# WHY separate from the SSM endpoint SG: the fronted endpoint services are the
# logging-account Tier-1 NLBs listening on TCP 8080 (PLAN §4.4) — the agents
# POST NDJSON to the endpoint ENIs on 8080, not 443. The SSM SG's 443-only
# ingress would silently drop the entire S1/S2 data path.
# ---------------------------------------------------------------------------
resource "aws_security_group" "aggregator_endpoints" {
  name        = "${var.name_prefix}-sender-agg-endpoints-sg"
  description = "Allow Tier-1 data port (8080) from the sender VPC to the aggregator PrivateLink endpoints."
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "Agent NDJSON POST to Tier-1 NLBs via PrivateLink (PLAN 4.4)"
    from_port   = 8080
    to_port     = 8080
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
    Name = "${var.name_prefix}-sender-agg-endpoints-sg"
  })
}

# ---------------------------------------------------------------------------
# PrivateLink CONSUMER interface endpoints against the logging-account
# aggregator endpoint services (one per aggregator technology).
# WHY: PLAN §4.2/§4.3 — the agent→aggregator hop crosses the account boundary
# via PrivateLink. private_dns_enabled is false because these are custom
# endpoint services (no verified private-DNS domain); the harness/Ansible uses
# the endpoint's own DNS entries (exported as outputs) as the POST target.
# ---------------------------------------------------------------------------
resource "aws_vpc_endpoint" "vector_aggregator" {
  vpc_id              = aws_vpc.this.id
  service_name        = var.vector_aggregator_service_name
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.aggregator_endpoints.id]
  private_dns_enabled = false

  tags = merge(var.common_tags, {
    Name  = "${var.name_prefix}-sender-vagg-ep"
    Stack = "vector"
  })
}

resource "aws_vpc_endpoint" "cribl_aggregator" {
  vpc_id              = aws_vpc.this.id
  service_name        = var.cribl_aggregator_service_name
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.aggregator_endpoints.id]
  private_dns_enabled = false

  tags = merge(var.common_tags, {
    Name  = "${var.name_prefix}-sender-cs-ep"
    Stack = "cribl"
  })
}

# ---------------------------------------------------------------------------
# Generator-host security group.
# WHY: generator hosts need NO ingress (SSM-only, no SSH/WinRM per PLAN §4.2).
# Egress is open so they can POST to the aggregator endpoints (:8080/:8081),
# write to S3 via the gateway endpoint, reach SSM endpoints, and (while NAT is
# up) install packages. All destinations are private AWS networking.
# ---------------------------------------------------------------------------
resource "aws_security_group" "generator" {
  name        = "${var.name_prefix}-sender-generator-sg"
  description = "Generator hosts: no ingress (SSM-only); egress to aggregators/S3/SSM."
  vpc_id      = aws_vpc.this.id

  # No ingress rules on purpose (management is SSM Session Manager, outbound).

  egress {
    description = "All egress (aggregator POST, S3 endpoint, SSM, package install via NAT)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-sender-generator-sg"
  })
}
