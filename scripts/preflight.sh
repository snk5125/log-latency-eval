#!/usr/bin/env bash
# =============================================================================
# preflight.sh — verify the local toolchain + AWS credentials before deploy/run.
#
# Checks (PLAN §8 Deploy prerequisites):
#   * terraform, ansible / ansible-playbook, python3 + boto3, session-manager-plugin
#   * jq (hard dependency of scripts/gen-ansible-vars.sh, which setup.sh runs at
#     step 5 immediately after `terraform apply` — every `tfo`/`tfo_epdns`
#     lookup in that script pipes `terraform output -json` through jq, so a
#     missing jq fails setup partway through, after infra is already applied)
#   * terraform/terraform.tfvars exists (setup.sh's `terraform apply` runs with
#     -input=false, so a missing tfvars file is not an interactive prompt —
#     it's a hard failure with no values to fall back to)
#   * both AWS named profiles resolvable via `sts get-caller-identity`
#   * an AWS region is set
#
# Emits a clear per-check PASS/FAIL line and exits non-zero if ANY check fails so
# setup.sh can gate on it. No AWS mutations are performed.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ------------------------------------------------------------------------------
# Config — overridable via environment. Defaults match PLAN §4.1 / harness defaults.
# ------------------------------------------------------------------------------
LLT_SENDER_PROFILE="${LLT_SENDER_PROFILE:-llt-sender}"
LLT_LOGGING_PROFILE="${LLT_LOGGING_PROFILE:-llt-logging}"
AWS_REGION="${AWS_REGION:-us-east-2}"

# ------------------------------------------------------------------------------
# Tiny check framework. Each check increments FAILS on failure but does NOT abort,
# so the operator sees the full picture in one run.
# ------------------------------------------------------------------------------
FAILS=0

pass() { printf '  [PASS] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAILS=$((FAILS + 1)); }

# check_cmd <display-name> <command-to-probe> [version-flag]
check_cmd() {
  local name="$1" cmd="$2" vflag="${3:-}"
  if command -v "$cmd" >/dev/null 2>&1; then
    if [ -n "$vflag" ]; then
      local ver
      ver="$("$cmd" "$vflag" 2>&1 | head -n1 || true)"
      pass "$name found: $ver"
    else
      pass "$name found: $(command -v "$cmd")"
    fi
  else
    fail "$name NOT found on PATH"
  fi
}

echo "== llt preflight =="
echo "region=$AWS_REGION sender_profile=$LLT_SENDER_PROFILE logging_profile=$LLT_LOGGING_PROFILE"
echo

# ------------------------------------------------------------------------------
# 1. Required binaries.
# ------------------------------------------------------------------------------
echo "-- toolchain --"
check_cmd "terraform" terraform -version
check_cmd "ansible" ansible --version
check_cmd "ansible-playbook" ansible-playbook --version
check_cmd "aws CLI" aws --version
check_cmd "python3" python3 --version
check_cmd "session-manager-plugin" session-manager-plugin --version
# jq: hard dependency of scripts/gen-ansible-vars.sh (every `tfo`/`tfo_epdns`
# lookup pipes `terraform output -json` through it). setup.sh runs that script
# at step 5, right after `terraform apply` — a missing jq is discovered only
# AFTER infra is already stood up, so catching it here (before apply) avoids
# an awkward apply-succeeded-but-vars-bridge-failed half-deployed state.
if command -v jq >/dev/null 2>&1; then
  pass "jq found: $(jq --version 2>&1 | head -n1)"
else
  fail "jq NOT found on PATH (required by scripts/gen-ansible-vars.sh — install via your package manager, e.g. 'brew install jq' / 'apt-get install jq')"
fi

# ------------------------------------------------------------------------------
# 2. Python boto3 module (required by the orchestrator + analysis harness).
# ------------------------------------------------------------------------------
echo "-- python modules --"
if python3 -c "import boto3" >/dev/null 2>&1; then
  BOTO_VER="$(python3 -c "import boto3; print(boto3.__version__)" 2>/dev/null || echo '?')"
  pass "python3 boto3 importable (v$BOTO_VER)"
else
  fail "python3 boto3 NOT importable (pip install boto3)"
fi
# PyYAML is used by run_matrix.py to parse scenarios.yaml (ships with Ansible).
if python3 -c "import yaml" >/dev/null 2>&1; then
  pass "python3 PyYAML importable"
else
  fail "python3 PyYAML NOT importable (pip install pyyaml)"
fi

# ------------------------------------------------------------------------------
# 3. terraform/terraform.tfvars exists. setup.sh's `terraform apply` runs with
#    -input=false (PLAN §7 / scripts/setup.sh), so a missing tfvars file is NOT
#    an interactive prompt for the missing values — it's an immediate apply
#    failure. Checking here, before setup.sh ever invokes terraform, gives an
#    actionable message pointing at the committed .example shape (PLAN §6/§7:
#    "*.example files show shape") instead of a raw terraform error surfacing
#    mid-apply.
# ------------------------------------------------------------------------------
echo "-- terraform vars file --"
TF_DIR="$REPO_ROOT/terraform"
TFVARS_PATH="$TF_DIR/terraform.tfvars"
if [ -f "$TFVARS_PATH" ]; then
  pass "terraform.tfvars found: $TFVARS_PATH"
else
  fail "terraform.tfvars NOT found at $TFVARS_PATH (required — 'terraform apply' runs with -input=false and will fail without it; copy terraform/terraform.tfvars.example to terraform/terraform.tfvars and fill in your two AWS profile names / account details)"
fi

# ------------------------------------------------------------------------------
# 4. AWS region present (env or CLI default).
# ------------------------------------------------------------------------------
echo "-- aws region --"
if [ -n "${AWS_REGION:-}" ]; then
  pass "AWS region set: $AWS_REGION"
else
  fail "AWS region not set (export AWS_REGION or set in profile)"
fi

# ------------------------------------------------------------------------------
# 5. Both AWS profiles resolvable. sts get-caller-identity proves credentials work
#    AND that the profile exists. We print the resolved account for the operator.
# ------------------------------------------------------------------------------
echo "-- aws credentials --"
check_profile() {
  local profile="$1"
  local out
  if out="$(aws sts get-caller-identity \
              --profile "$profile" \
              --region "$AWS_REGION" \
              --query 'Account' --output text 2>/dev/null)"; then
    pass "profile '$profile' resolves to account $out"
  else
    fail "profile '$profile' could NOT authenticate (aws sts get-caller-identity)"
  fi
}
check_profile "$LLT_SENDER_PROFILE"
check_profile "$LLT_LOGGING_PROFILE"

# ------------------------------------------------------------------------------
# Verdict.
# ------------------------------------------------------------------------------
echo
if [ "$FAILS" -eq 0 ]; then
  echo "PREFLIGHT: PASS (all checks green)"
  exit 0
fi
echo "PREFLIGHT: FAIL ($FAILS check(s) failed) — resolve the above before setup.sh"
exit 1
