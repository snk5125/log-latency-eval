########################################################################
# module: cribl-stream
#
# ROLE IN THE EXPERIMENT:
#   The second aggregator technology under test (PLAN §4.3), shaped identically
#   to the Vector stack so hop topology is the controlled constant and only the
#   vendor differs:
#     - Leader:        1 × m6i.large (control plane only; NOT on the data path).
#     - Worker group t1: 2 × m6i.xlarge behind `llt-cs-t1-nlb` (TCP 8080) — the
#       agent → aggregator hop entry (exposed cross-account via PrivateLink).
#     - Worker group t2: 2 × m6i.xlarge behind `llt-cs-t2-nlb` (TCP 8081) — the
#       aggregator → aggregator hop (S2/S4), in-VPC only.
#   Self-hosted Cribl (Cribl Free per PLAN §4.3). The leader's UI (:9000) and
#   the worker→leader control channel (:4200) are reachable ONLY inside the VPC;
#   the UI is accessed via SSM port-forward (no ingress), so a dedicated leader
#   SG opens 4200 (workers→leader) and 9000 (from within VPC) but nothing public.
########################################################################

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  worker_indexes = toset(["0", "1"]) # 2 workers per group (PLAN §4.3)
}

# ===========================================================================
# LEADER SECURITY GROUP (control plane: 4200 from workers, 9000 UI in-VPC)
# ===========================================================================
# WHY a dedicated SG: the leader has control-plane ports the data-plane
# aggregator SG must NOT expose. 4200 must accept worker registration/config;
# 9000 (UI) is opened to the VPC CIDR only and actually reached via SSM
# port-forward — never publicly (PLAN §4.3).
resource "aws_security_group" "leader" {
  name        = "${var.name_prefix}-cs-leader-sg"
  description = "Cribl leader: 4200 from workers (control), 9000 UI in-VPC (SSM port-forward only), SSM-only mgmt."
  vpc_id      = var.vpc_id

  ingress {
    description     = "Cribl worker -> leader control channel (config/registration)"
    from_port       = 4200
    to_port         = 4200
    protocol        = "tcp"
    security_groups = [var.aggregator_sg_id] # only worker instances (shared agg SG)
  }

  ingress {
    description = "Cribl UI (reached via SSM port-forward from within the VPC only)"
    from_port   = 9000
    to_port     = 9000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "All egress (S3, SSM, package install via NAT)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.common_tags, {
    Name  = "${var.name_prefix}-cs-leader-sg"
    Role  = "leader"
    Stack = "cribl"
  })
}

# ===========================================================================
# LEADER instance (1 ×). Control plane only — not fronted by any NLB.
# ===========================================================================
resource "aws_instance" "leader" {
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.leader_instance_type
  subnet_id                   = var.private_subnet_ids[0]
  iam_instance_profile        = var.instance_profile_name
  associate_public_ip_address = false

  # Leader SG only: the leader is control plane (4200/9000) and serves neither
  # data port; attaching the aggregator SG would open 8080/8081 ingress on a
  # node that never listens there. The 4200 rule keys on the aggregator SG as
  # SOURCE, which the workers hold — the leader doesn't need it attached.
  vpc_security_group_ids = [aws_security_group.leader.id]

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
    Name  = "${var.name_prefix}-cs-leader"
    Role  = "leader"
    Stack = "cribl"
    Os    = "linux"
  })
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
