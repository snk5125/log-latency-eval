########################################################################
# module: vector-aggregator
#
# ROLE IN THE EXPERIMENT:
#   One of the two aggregator technologies under test (PLAN §4.3). Two tiers,
#   each a horizontally-scaled pair behind an internal NLB:
#     - Tier-1: 2 × m6i.xlarge behind `llt-vagg-t1-nlb` (TCP 8080). Fronts the
#       agent → aggregator hop (S1/S2) and, cross-account via PrivateLink, is
#       the entry point the sender hosts POST NDJSON to.
#     - Tier-2: 2 × m6i.xlarge behind `llt-vagg-t2-nlb` (TCP 8081). Fronts the
#       aggregator → aggregator hop (S2/S4). Stays inside the logging VPC.
#   Internal NLBs (not ALBs) because the wire protocol is HTTP/1.1 POST of
#   NDJSON and we want L4 pass-through with source-IP preservation and minimal
#   added latency — the NLB itself must not become a measured variable.
#   Health checks target the tier's HTTP listener so unhealthy Vector nodes are
#   pulled from rotation (a hung node would corrupt loss-rate stats).
########################################################################

# AMI for aggregator nodes: Amazon Linux 2023 (Vector runs on Linux here).
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  # Two nodes per tier (PLAN §4.3). Indexed 0/1 to spread across the 2 subnets.
  tier1_indexes = toset(["0", "1"])
  tier2_indexes = toset(["0", "1"])
}

# ===========================================================================
# TIER-1 instances (agent-entry)
# ===========================================================================
resource "aws_instance" "t1" {
  for_each = local.tier1_indexes

  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.instance_type
  subnet_id                   = var.private_subnet_ids[tonumber(each.key) % length(var.private_subnet_ids)]
  vpc_security_group_ids      = [var.aggregator_sg_id]
  iam_instance_profile        = var.instance_profile_name
  associate_public_ip_address = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 required
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted   = true
    volume_size = 40
    volume_type = "gp3"
  }

  tags = merge(var.common_tags, {
    Name  = "${var.name_prefix}-vagg-t1-${each.key}"
    Role  = "agg-t1"
    Stack = "vector"
    Os    = "linux"
  })
}

# ===========================================================================
# TIER-2 instances (inter-aggregator hop)
# ===========================================================================
resource "aws_instance" "t2" {
  for_each = local.tier2_indexes

  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.instance_type
  subnet_id                   = var.private_subnet_ids[tonumber(each.key) % length(var.private_subnet_ids)]
  vpc_security_group_ids      = [var.aggregator_sg_id]
  iam_instance_profile        = var.instance_profile_name
  associate_public_ip_address = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted   = true
    volume_size = 40
    volume_type = "gp3"
  }

  tags = merge(var.common_tags, {
    Name  = "${var.name_prefix}-vagg-t2-${each.key}"
    Role  = "agg-t2"
    Stack = "vector"
    Os    = "linux"
  })
}

# ===========================================================================
# TIER-1 internal NLB (TCP 8080)
# ===========================================================================
# WHY internal + TCP: keeps the agent→aggregator hop on private networking and
# passes the HTTP POST through at L4 so the NLB adds negligible, constant
# latency and is not itself a measured hop variable.
resource "aws_lb" "t1" {
  name               = "${var.name_prefix}-vagg-t1-nlb"
  internal           = true
  load_balancer_type = "network"
  subnets            = var.private_subnet_ids

  # Cross-zone LB so both Tier-1 nodes receive traffic regardless of which AZ
  # the PrivateLink ENI lands in — avoids AZ-skew confounding the hop latency.
  enable_cross_zone_load_balancing = true

  tags = merge(var.common_tags, {
    Role  = "agg-t1"
    Stack = "vector"
  })
}

resource "aws_lb_target_group" "t1" {
  name     = "${var.name_prefix}-vagg-t1-tg"
  port     = 8080
  protocol = "TCP"
  vpc_id   = var.vpc_id

  # HTTP health check against the Vector http source's health path. WHY: pull a
  # hung Vector node from rotation so it can't silently drop events.
  health_check {
    protocol            = "HTTP"
    port                = "traffic-port"
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 10
  }

  tags = merge(var.common_tags, {
    Role  = "agg-t1"
    Stack = "vector"
  })
}

resource "aws_lb_target_group_attachment" "t1" {
  for_each         = aws_instance.t1
  target_group_arn = aws_lb_target_group.t1.arn
  target_id        = each.value.id
  port             = 8080
}

resource "aws_lb_listener" "t1" {
  load_balancer_arn = aws_lb.t1.arn
  port              = 8080
  protocol          = "TCP" # L4 pass-through of the HTTP POST

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.t1.arn
  }
}

# ===========================================================================
# TIER-2 internal NLB (TCP 8081)
# ===========================================================================
resource "aws_lb" "t2" {
  name                             = "${var.name_prefix}-vagg-t2-nlb"
  internal                         = true
  load_balancer_type               = "network"
  subnets                          = var.private_subnet_ids
  enable_cross_zone_load_balancing = true

  tags = merge(var.common_tags, {
    Role  = "agg-t2"
    Stack = "vector"
  })
}

resource "aws_lb_target_group" "t2" {
  name     = "${var.name_prefix}-vagg-t2-tg"
  port     = 8081
  protocol = "TCP"
  vpc_id   = var.vpc_id

  health_check {
    protocol            = "HTTP"
    port                = "traffic-port"
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 10
  }

  tags = merge(var.common_tags, {
    Role  = "agg-t2"
    Stack = "vector"
  })
}

resource "aws_lb_target_group_attachment" "t2" {
  for_each         = aws_instance.t2
  target_group_arn = aws_lb_target_group.t2.arn
  target_id        = each.value.id
  port             = 8081
}

resource "aws_lb_listener" "t2" {
  load_balancer_arn = aws_lb.t2.arn
  port              = 8081
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.t2.arn
  }
}
