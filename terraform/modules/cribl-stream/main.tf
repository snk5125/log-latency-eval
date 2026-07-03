########################################################################
# module: cribl-stream
#
# ROLE IN THE EXPERIMENT:
#   The second aggregator technology under test (PLAN §4.3), shaped identically
#   to the Vector stack so hop topology is the controlled constant and only the
#   vendor differs. Cribl Free supports at most a single worker group per
#   leader — insufficient for this experiment's two physically separate tiers
#   — so there is NO leader / control-plane node here. Each node below is an
#   independent, standalone, single-instance Cribl Stream deployment, locally
#   configured by Ansible (same file-based config model as Cribl Edge):
#     - Tier-1 nodes: 2 × m6i.xlarge standalone, each behind `llt-cs-t1-nlb`
#       (TCP 8080) — the agent → aggregator hop entry (exposed cross-account
#       via PrivateLink).
#     - Tier-2 nodes: 2 × m6i.xlarge standalone, each behind `llt-cs-t2-nlb`
#       (TCP 8081) — the aggregator → aggregator hop (S2/S4), in-VPC only.
#   Self-hosted Cribl (Cribl Free per PLAN §4.3). There is no worker→leader
#   control channel and no shared UI/API port to expose, so these nodes need
#   nothing beyond the shared data-plane aggregator SG (8080/8081 ingress) and
#   SSM egress — no dedicated control-plane security group.
########################################################################

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  worker_indexes = toset(["0", "1"]) # 2 standalone nodes per tier (PLAN §4.3)
}

# ===========================================================================
# WORKER GROUP t1 (agent-entry) — 2 workers behind llt-cs-t1-nlb (8080)
# ===========================================================================
resource "aws_instance" "t1" {
  for_each = local.worker_indexes

  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.worker_instance_type
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
    Name  = "${var.name_prefix}-cs-t1-${each.key}"
    Role  = "agg-t1"
    Stack = "cribl"
    Os    = "linux"
  })
}

# ===========================================================================
# WORKER GROUP t2 (inter-aggregator) — 2 workers behind llt-cs-t2-nlb (8081)
# ===========================================================================
resource "aws_instance" "t2" {
  for_each = local.worker_indexes

  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.worker_instance_type
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
    Name  = "${var.name_prefix}-cs-t2-${each.key}"
    Role  = "agg-t2"
    Stack = "cribl"
    Os    = "linux"
  })
}

# ===========================================================================
# TIER-1 (worker group t1) internal NLB — TCP 8080
# ===========================================================================
resource "aws_lb" "t1" {
  name                             = "${var.name_prefix}-cs-t1-nlb"
  internal                         = true
  load_balancer_type               = "network"
  subnets                          = var.private_subnet_ids
  enable_cross_zone_load_balancing = true

  tags = merge(var.common_tags, {
    Role  = "agg-t1"
    Stack = "cribl"
  })
}

resource "aws_lb_target_group" "t1" {
  name     = "${var.name_prefix}-cs-t1-tg"
  port     = 8080
  protocol = "TCP"
  vpc_id   = var.vpc_id

  # TCP health check (Cribl HTTP Raw source health path is version-dependent; a
  # TCP connect check reliably detects a down worker without coupling to a
  # specific Cribl health route). Pulls dead workers so loss-rate stays clean.
  health_check {
    protocol            = "TCP"
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 10
  }

  # PLAN §4.6(6): minimize target deregistration delay (default 300 s) so
  # detach/reconfigure cycles don't hold stale targets in rotation.
  deregistration_delay = 30

  tags = merge(var.common_tags, {
    Role  = "agg-t1"
    Stack = "cribl"
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
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.t1.arn
  }
}

# ===========================================================================
# TIER-2 (worker group t2) internal NLB — TCP 8081
# ===========================================================================
resource "aws_lb" "t2" {
  name                             = "${var.name_prefix}-cs-t2-nlb"
  internal                         = true
  load_balancer_type               = "network"
  subnets                          = var.private_subnet_ids
  enable_cross_zone_load_balancing = true

  tags = merge(var.common_tags, {
    Role  = "agg-t2"
    Stack = "cribl"
  })
}

resource "aws_lb_target_group" "t2" {
  name     = "${var.name_prefix}-cs-t2-tg"
  port     = 8081
  protocol = "TCP"
  vpc_id   = var.vpc_id

  health_check {
    protocol            = "TCP"
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 10
  }

  # PLAN §4.6(6): minimize target deregistration delay (default 300 s).
  deregistration_delay = 30

  tags = merge(var.common_tags, {
    Role  = "agg-t2"
    Stack = "cribl"
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
