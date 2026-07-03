#!/usr/bin/env bash
# =============================================================================
# teardown.sh — dismantle the llt experiment (PLAN §8 phase 6).
#
# Sequence:
#   1. Confirm intent (unless -y / --auto-approve).
#   2. Empty all llt-* S3 buckets (versioned-safe: delete object versions AND
#      delete markers so `terraform destroy` is not blocked by non-empty buckets).
#   3. terraform destroy (interactive confirm unless -y).
#   4. Remind the operator about artifacts Terraform does NOT necessarily remove
#      (SSM inventory entries, CloudWatch log groups/metrics).
#
# Bucket emptying is done via the AWS CLI against the LOGGING account (all llt
# buckets live there — PLAN §4.3). Even though PLAN §4.3 says versioning is OFF,
# we handle versions defensively in case versioning was ever toggled on.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$REPO_ROOT/terraform"

LLT_LOGGING_PROFILE="${LLT_LOGGING_PROFILE:-llt-logging}"
AWS_REGION="${AWS_REGION:-us-east-2}"

AUTO_APPROVE=""
for arg in "$@"; do
  case "$arg" in
    -y|--auto-approve) AUTO_APPROVE="1" ;;
    -h|--help)
      echo "Usage: teardown.sh [-y|--auto-approve]"
      echo "  -y  skip confirmations (empties buckets and destroys infra unattended)"
      exit 0
      ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n=== %s ===\n' "$1"; }

# ------------------------------------------------------------------------------
# 1. Confirm.
# ------------------------------------------------------------------------------
if [ -z "$AUTO_APPROVE" ]; then
  cat <<EOF
This will PERMANENTLY:
  * empty every S3 bucket named llt-* in account (profile: $LLT_LOGGING_PROFILE)
  * run 'terraform destroy' on all llt infrastructure in BOTH accounts

EOF
  printf 'Proceed with teardown? [y/N] '
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "Aborted. Nothing changed."; exit 1 ;;
  esac
fi

# ------------------------------------------------------------------------------
# 2. Empty all llt-* buckets (versioned-safe).
# ------------------------------------------------------------------------------
log "empty llt-* S3 buckets"
mapfile -t BUCKETS < <(
  aws s3api list-buckets \
    --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" \
    --query "Buckets[?starts_with(Name, 'llt-')].Name" --output text 2>/dev/null \
  | tr '\t' '\n'
)

if [ "${#BUCKETS[@]}" -eq 0 ] || [ -z "${BUCKETS[0]:-}" ]; then
  echo "  no llt-* buckets found (already removed?)."
else
  for bucket in "${BUCKETS[@]}"; do
    [ -z "$bucket" ] && continue
    echo "  emptying s3://$bucket ..."

    # 2a. Fast path: recursive delete of current (unversioned) objects.
    aws s3 rm "s3://$bucket" --recursive \
      --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 || true

    # 2b. Versioned-safe path: delete all object versions.
    versions_json="$(aws s3api list-object-versions \
      --bucket "$bucket" --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" \
      --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
      --output json 2>/dev/null || echo '{"Objects":null}')"
    if [ "$(echo "$versions_json" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(0 if not d.get("Objects") else len(d["Objects"]))')" != "0" ]; then
      echo "$versions_json" > /tmp/llt-del-versions.json
      aws s3api delete-objects --bucket "$bucket" \
        --delete file:///tmp/llt-del-versions.json \
        --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 || true
    fi

    # 2c. Delete markers (left behind by versioned deletes).
    markers_json="$(aws s3api list-object-versions \
      --bucket "$bucket" --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" \
      --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' \
      --output json 2>/dev/null || echo '{"Objects":null}')"
    if [ "$(echo "$markers_json" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(0 if not d.get("Objects") else len(d["Objects"]))')" != "0" ]; then
      echo "$markers_json" > /tmp/llt-del-markers.json
      aws s3api delete-objects --bucket "$bucket" \
        --delete file:///tmp/llt-del-markers.json \
        --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 || true
    fi
    echo "  emptied s3://$bucket"
  done
  rm -f /tmp/llt-del-versions.json /tmp/llt-del-markers.json
fi

# ------------------------------------------------------------------------------
# 3. terraform destroy.
# ------------------------------------------------------------------------------
log "terraform destroy"
if [ -n "$AUTO_APPROVE" ]; then
  terraform -chdir="$TF_DIR" destroy -input=false -auto-approve
else
  terraform -chdir="$TF_DIR" destroy -input=false
fi

# ------------------------------------------------------------------------------
# 4. Reminders — things Terraform destroy may leave behind.
# ------------------------------------------------------------------------------
log "teardown complete — manual cleanup reminders"
cat <<EOF

Terraform-managed infrastructure destroyed and llt-* buckets emptied.

Check for lingering, possibly non-Terraform-managed artifacts:
  * SSM: deregistered managed instances usually age out, but hybrid-activation
    registrations (if any) may need manual 'aws ssm deregister-managed-instance'.
  * CloudWatch: log groups / custom metrics created by agents may persist and
    incur retention cost — review /aws/llt* log groups and delete if unwanted.
  * S3 access logs or replicated copies outside the llt-* naming prefix, if any.
  * Local state: harness/orchestrator run-state and report/evidence/ data are
    retained locally (gitignored) — remove manually if you want a clean slate.
EOF
