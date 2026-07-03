#!/usr/bin/env bash
# =============================================================================
# setup.sh — one-shot deploy for the llt hop-latency experiment (PLAN §8 phase 2).
#
# Sequence:
#   1. preflight.sh (gate — abort if toolchain/creds missing).
#   2. terraform init + plan, then apply (interactive confirm unless -y / --auto-approve).
#   3. Wait for all generator/aggregator hosts to register as SSM managed instances
#      (Ansible uses the aws_ssm connection plugin — no host is usable until then).
#   4. ansible-galaxy install -r requirements.yml (collections/roles).
#   5. gen-ansible-vars.sh — render group_vars/all/generated_infra.yml from
#      Terraform outputs (buckets, endpoint DNS, SQS URLs).
#   6. Upload harness/generator/eventgen.py to the artifacts bucket (agents pull it).
#   7. ansible-playbook site.yml (installs agents, aggregators, generator, time-sync).
#   8. Print next-steps (run_matrix.py invocation).
#
# Design notes:
#   * set -euo pipefail: any un-handled failure aborts the deploy.
#   * Terraform uses two aliased providers driven by named profiles in a gitignored
#     terraform.tfvars (PLAN §4.1); this script does not manufacture credentials.
#   * Idempotent where the underlying tools are (terraform apply / ansible converge).
# =============================================================================
set -euo pipefail

# ------------------------------------------------------------------------------
# Resolve repo paths relative to this script so setup.sh works from any CWD.
# ------------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$REPO_ROOT/terraform"
ANSIBLE_DIR="$REPO_ROOT/ansible"
EVENTGEN="$REPO_ROOT/harness/generator/eventgen.py"

# ------------------------------------------------------------------------------
# Config (env-overridable). Mirrors preflight.sh / PLAN §4.1.
# ------------------------------------------------------------------------------
LLT_SENDER_PROFILE="${LLT_SENDER_PROFILE:-llt-sender}"
LLT_LOGGING_PROFILE="${LLT_LOGGING_PROFILE:-llt-logging}"
AWS_REGION="${AWS_REGION:-us-east-2}"

# Expected managed-instance count = 4 generators (2 lin + 2 win) + aggregator hosts.
# Vector stack: 4 agg (t1x2 + t2x2). Cribl stack: 4 standalone nodes (t1x2 + t2x2;
# no leader). Total hosts expecting SSM registration = 4 + 4 + 4 = 12. Overridable
# if the matrix changes.
LLT_EXPECTED_SSM_INSTANCES="${LLT_EXPECTED_SSM_INSTANCES:-12}"
LLT_SSM_WAIT_SECS="${LLT_SSM_WAIT_SECS:-600}"

# ------------------------------------------------------------------------------
# Flag parsing: -y / --auto-approve passes through to terraform apply.
# ------------------------------------------------------------------------------
AUTO_APPROVE=""
for arg in "$@"; do
  case "$arg" in
    -y|--auto-approve) AUTO_APPROVE="1" ;;
    -h|--help)
      echo "Usage: setup.sh [-y|--auto-approve]"
      echo "  -y  skip the interactive apply confirmation (passes -auto-approve to terraform)"
      exit 0
      ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n=== %s ===\n' "$1"; }

# ------------------------------------------------------------------------------
# 1. Preflight gate.
# ------------------------------------------------------------------------------
log "1/8 preflight"
bash "$SCRIPT_DIR/preflight.sh"

# ------------------------------------------------------------------------------
# 2. Terraform init + plan + apply.
# ------------------------------------------------------------------------------
log "2/8 terraform init"
terraform -chdir="$TF_DIR" init -input=false

log "2/8 terraform plan"
terraform -chdir="$TF_DIR" plan -input=false -out=llt.tfplan

if [ -n "$AUTO_APPROVE" ]; then
  log "2/8 terraform apply (auto-approve)"
  terraform -chdir="$TF_DIR" apply -input=false llt.tfplan
else
  # Interactive confirmation — deploying real AWS infra across two accounts.
  printf '\nReview the plan above. Apply this Terraform plan? [y/N] '
  read -r reply
  case "$reply" in
    y|Y|yes|YES)
      log "2/8 terraform apply"
      terraform -chdir="$TF_DIR" apply -input=false llt.tfplan
      ;;
    *)
      echo "Aborted at terraform apply. No infrastructure changes made."
      exit 1
      ;;
  esac
fi

# ------------------------------------------------------------------------------
# 3. Wait for SSM managed-instance registration. Hosts have no SSH/WinRM (PLAN §4.2)
#    so Ansible (aws_ssm plugin) cannot reach them until the SSM agent checks in.
#    We poll the SENDER account (generators) AND the LOGGING account (aggregators);
#    both must reach their expected online counts. We count Project=llt instances.
# ------------------------------------------------------------------------------
log "3/8 wait for SSM managed-instance registration (up to ${LLT_SSM_WAIT_SECS}s)"
count_online() {
  local profile="$1"
  aws ssm describe-instance-information \
    --profile "$profile" --region "$AWS_REGION" \
    --filters "Key=tag:Project,Values=llt" \
    --query "length(InstanceInformationList[?PingStatus=='Online'])" \
    --output text 2>/dev/null || echo 0
}

deadline=$(( $(date +%s) + LLT_SSM_WAIT_SECS ))
while :; do
  sender_n="$(count_online "$LLT_SENDER_PROFILE")"
  logging_n="$(count_online "$LLT_LOGGING_PROFILE")"
  total=$(( sender_n + logging_n ))
  echo "  online: sender=$sender_n logging=$logging_n total=$total / expected $LLT_EXPECTED_SSM_INSTANCES"
  if [ "$total" -ge "$LLT_EXPECTED_SSM_INSTANCES" ]; then
    echo "  all expected instances online."
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "  WARNING: timed out waiting for SSM registration; proceeding anyway." >&2
    echo "  (Ansible will fail fast on any host that is still unreachable.)" >&2
    break
  fi
  sleep 15
done

# ------------------------------------------------------------------------------
# 4. Ansible dependencies.
# ------------------------------------------------------------------------------
log "4/8 ansible-galaxy install -r requirements.yml"
if [ -f "$ANSIBLE_DIR/requirements.yml" ]; then
  ansible-galaxy install -r "$ANSIBLE_DIR/requirements.yml"
else
  echo "  no requirements.yml found — skipping (collections may already be present)."
fi

# ------------------------------------------------------------------------------
# 5. Terraform → Ansible variable bridge (gen-ansible-vars.sh) renders the
#    gitignored group_vars/all/generated_infra.yml with live bucket names,
#    PrivateLink endpoint DNS, and SQS URLs. site.yml and configure-scenario.yml
#    consume these vars, so this MUST precede step 7.
# ------------------------------------------------------------------------------
log "5/8 generate Ansible infra vars from Terraform outputs"
LLT_SENDER_PROFILE="$LLT_SENDER_PROFILE" LLT_LOGGING_PROFILE="$LLT_LOGGING_PROFILE" \
  AWS_REGION="$AWS_REGION" "$SCRIPT_DIR/gen-ansible-vars.sh"

# ------------------------------------------------------------------------------
# 6. Upload the event generator to the artifacts bucket BEFORE site.yml: the
#    event-generator role fetches it from
#    s3://<artifacts>/harness/generator/eventgen.py (role default eventgen_s3_uri),
#    so the object must exist before the converge. Key path mirrors the repo path.
# ------------------------------------------------------------------------------
log "6/8 upload eventgen.py to artifacts bucket"
ARTIFACTS_BUCKET="$(terraform -chdir="$TF_DIR" output -raw artifacts_bucket 2>/dev/null || true)"
if [ -z "$ARTIFACTS_BUCKET" ]; then
  echo "  WARNING: could not read 'artifacts_bucket' Terraform output; skipping upload." >&2
  echo "  Upload manually: aws s3 cp $EVENTGEN s3://<artifacts-bucket>/harness/generator/eventgen.py" >&2
else
  aws s3 cp "$EVENTGEN" "s3://$ARTIFACTS_BUCKET/harness/generator/eventgen.py" \
    --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION"
  echo "  uploaded to s3://$ARTIFACTS_BUCKET/harness/generator/eventgen.py"
fi

# ------------------------------------------------------------------------------
# 7. Base configuration converge (site.yml): common, time-sync, agents,
#    aggregators, standalone Cribl Stream nodes, event-generator role (PLAN §6).
#    Inventory is the inventories/ DIRECTORY (sender + logging aws_ec2 files
#    union into one host set); the env vars below feed the plugin's profile
#    lookups and the per-host llt_ssm_profile compose expressions.
# ------------------------------------------------------------------------------
log "7/8 ansible-playbook site.yml"
export LLT_AWS_PROFILE_SENDER="$LLT_SENDER_PROFILE"
export LLT_AWS_PROFILE_LOGGING="$LLT_LOGGING_PROFILE"
export LLT_AWS_REGION="$AWS_REGION"
ansible-playbook -i "$ANSIBLE_DIR/inventories/" \
  "$ANSIBLE_DIR/playbooks/site.yml"

# ------------------------------------------------------------------------------
# 7. Next steps.
# ------------------------------------------------------------------------------
log "8/8 setup complete"
cat <<EOF

Infrastructure deployed and base-configured. Next steps:

  # Dry-run the full 48-run matrix (no AWS calls):
  python3 harness/orchestrator/run_matrix.py --dry-run

  # Run a single cell:
  python3 harness/orchestrator/run_matrix.py --only s1-vec-vagg-1k

  # Run the whole matrix, Vector and Cribl stacks in parallel:
  python3 harness/orchestrator/run_matrix.py --parallel-stacks

  # Analyze results after runs complete:
  python3 harness/analysis/analyze.py

When finished, tear everything down with:  scripts/teardown.sh
EOF
