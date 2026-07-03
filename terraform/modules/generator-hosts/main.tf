########################################################################
# module: generator-hosts
#
# ROLE IN THE EXPERIMENT:
#   The event SOURCES of the entire latency test (PLAN §4.2). Four hosts in the
#   sender VPC private subnets:
#     - 2 × Amazon Linux 2023 (m6i.large)
#     - 2 × Windows Server 2022 (m6i.large)
#   One Linux + one Windows host runs the Vector agent (Stack=vector); the other
#   Linux+Windows pair runs Cribl Edge (Stack=cribl). During any given run only
#   the pair matching that run's `agent` dimension generates (the SSM-driven
#   orchestrator starts/stops generators). Both OSes run concurrently and are
#   separated at analysis time via the Os tag / host_os field.
#
#   Hardening required by PLAN §4.2:
#     - IMDSv2 required (token-only metadata) — no legacy metadata surface.
#     - No public IPs.
#     - SSM-only management (instance profile from the iam module).
#   AMIs are resolved from SSM public parameters at apply time (PLAN §7) so the
#   exact AMI IDs land in state/evidence and results are reproducible.
########################################################################

# ---------------------------------------------------------------------------
# AMI resolution via SSM public parameters (PLAN §7: AMIs via SSM at apply time).
# WHY SSM parameters (not a filtered ami data source): AWS-published, pinned by
# alias, and the resolved ImageId is recorded in state as run evidence.
# ---------------------------------------------------------------------------

# Amazon Linux 2023, x86_64 (matches m6i intel instance family).
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# Windows Server 2022 English full base.
data "aws_ssm_parameter" "windows2022" {
  name = "/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base"
}

# ---------------------------------------------------------------------------
# Host definition table. Each host declares OS, agent Stack, and which AMI to
# use. This drives a single for_each so the four hosts are defined uniformly and
# tagged exactly per PLAN §7 (Role=generator, Stack, Os).
# ---------------------------------------------------------------------------
locals {
  # subnet_idx spreads each AGENT PAIR across both AZs (lin-vec/win-ce in AZ0,
  # win-vec/lin-ce in AZ1): during a run only one pair generates, so pinning a
  # pair to a single AZ would correlate stack with AZ — a placement confound
  # PLAN §4.6(7) forbids. (Index-by-sorted-key put each pair in one AZ.)
  hosts = {
    # Vector agent pair
    "lin-vec" = { os = "linux", stack = "vector", subnet_idx = 0, ami = data.aws_ssm_parameter.al2023.value }
    "win-vec" = { os = "windows", stack = "vector", subnet_idx = 1, ami = data.aws_ssm_parameter.windows2022.value }
    # Cribl Edge pair
    "lin-ce" = { os = "linux", stack = "cribl", subnet_idx = 1, ami = data.aws_ssm_parameter.al2023.value }
    "win-ce" = { os = "windows", stack = "cribl", subnet_idx = 0, ami = data.aws_ssm_parameter.windows2022.value }
  }
}

# ---------------------------------------------------------------------------
# Generator EC2 instances.
# WHY IMDSv2 required + no public IP: PLAN §4.2 hardening; the data path and
# management path are both fully private (SSM). metadata_options enforces
# token-only IMDS.
# ---------------------------------------------------------------------------
resource "aws_instance" "generator" {
  for_each = local.hosts

  ami                    = each.value.ami
  instance_type          = var.instance_type
  subnet_id              = var.private_subnet_ids[each.value.subnet_idx % length(var.private_subnet_ids)]
  vpc_security_group_ids = [var.generator_sg_id]
  iam_instance_profile   = var.instance_profile_name

  associate_public_ip_address = false # PLAN §4.2: no public IPs

  # IMDSv2 required — token-only metadata (PLAN §4.2 hardening).
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 mandatory
    http_put_response_hop_limit = 1          # metadata not reachable from containers/proxies
  }

  # Encrypt root volume at rest (secure-by-default; low cost for a test bed).
  root_block_device {
    encrypted   = true
    volume_size = 30
    volume_type = "gp3"
  }

  tags = merge(var.common_tags, {
    # PLAN §7 tag contract: Role/Stack/Os drive the Ansible dynamic inventory
    # groups and the analysis host_os dimension.
    # Name carries the "-01" instance ordinal: it must equal the host_id used
    # in events and in harness/orchestrator/scenarios.yaml (PLAN §5.1 example
    # "llt-lin-vec-01") — run_matrix resolves instances by tag:Name equality.
    Name  = "${var.name_prefix}-${each.key}-01"
    Role  = "generator"
    Stack = each.value.stack
    Os    = each.value.os
  })
}
