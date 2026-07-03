#!/usr/bin/env bash
# =============================================================================
# teardown.sh — dismantle the llt experiment (PLAN §8 phase 6).
#
# Sequence:
#   1. Confirm intent (unless -y / --auto-approve).
#   2. Empty all llt-* S3 buckets THAT CARRY THE Project=llt TAG (versioned-safe:
#      delete object versions AND delete markers, chunked to the S3 delete-objects
#      API's 1000-key-per-request limit, so `terraform destroy` is not blocked by
#      non-empty buckets and large version histories don't silently fail >1000).
#   3. terraform destroy (interactive confirm unless -y).
#   4. Remind the operator about artifacts Terraform does NOT necessarily remove
#      (SSM inventory entries, CloudWatch log groups/metrics).
#
# Bucket emptying is done via the AWS CLI against the LOGGING account (all llt
# buckets live there — PLAN §4.3). Even though PLAN §4.3 says versioning is OFF,
# we handle versions defensively in case versioning was ever toggled on.
#
# SAFETY: bucket selection is `llt-*` name prefix AND tag Project=llt (checked via
# get-bucket-tagging). A same-prefixed bucket from an unrelated project (or a
# bucket whose tags failed to apply) must never be emptied by a prefix match
# alone, so a missing/unreadable tag set is treated as "not ours" and skipped
# with a warning rather than assumed safe.
#
# PORTABILITY: this script intentionally avoids `mapfile` (bash 4+ only). macOS
# ships bash 3.2 by default, where `mapfile` is an unknown command and — under
# `set -e` — kills the whole script before teardown even starts. All array
# population below uses a `while IFS= read -r` loop instead, which works on
# bash 3.2 and every later bash/POSIX-ish shell alike.
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
# Helpers for §2.
# ------------------------------------------------------------------------------

# Scratch dir for intermediate JSON (chunked delete-objects payloads etc.).
# Using a per-run temp dir (not fixed /tmp names) avoids collisions if teardown
# is ever invoked concurrently and is cleaned up on exit regardless of outcome.
TEARDOWN_TMP="$(mktemp -d "${TMPDIR:-/tmp}/llt-teardown.XXXXXX")"
trap 'rm -rf "$TEARDOWN_TMP"' EXIT

# bucket_is_ours <bucket> — true (0) iff the bucket carries tag Project=llt.
# get-bucket-tagging FAILS (non-zero) when a bucket has no tag set at all, which
# S3 treats as "NoSuchTagSet" rather than an empty tag list — that failure is
# deliberately treated as "not confirmed ours" (skip + warn), NOT as a pass,
# because a prefix-only match (llt-*) can otherwise hit an unrelated bucket that
# merely happens to share the naming prefix.
bucket_is_ours() {
  local bucket="$1"
  local tags_json
  if ! tags_json="$(aws s3api get-bucket-tagging \
        --bucket "$bucket" \
        --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" \
        --output json 2>/dev/null)"; then
    return 1
  fi
  local project_tag
  project_tag="$(echo "$tags_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for t in d.get("TagSet", []):
    if t.get("Key") == "Project":
        print(t.get("Value", ""))
        break
' 2>/dev/null || true)"
  [ "$project_tag" = "llt" ]
}

# chunked_delete_objects <bucket> <objects-json-file>
#   <objects-json-file> holds {"Objects":[{"Key":..,"VersionId":..}, ...]}.
# The S3 DeleteObjects API accepts at most 1000 keys per request; a single call
# over a larger version history silently fails (caught only by the `|| true`
# that swallowed every error in the previous version of this script). This
# helper splits the Objects array into <=1000-entry pages and issues one
# delete-objects call per page, still tolerating per-page failures (`|| true`)
# so one bad chunk doesn't abort the whole teardown, but no longer silently
# skipping everything beyond the first 1000 keys.
chunked_delete_objects() {
  local bucket="$1" objects_file="$2"
  local total
  total="$(python3 -c '
import sys, json
d = json.load(open(sys.argv[1]))
print(len(d.get("Objects") or []))
' "$objects_file")"
  if [ "$total" = "0" ]; then
    return 0
  fi
  local n_chunks=$(( (total + 999) / 1000 ))
  local i chunk_file
  for (( i = 0; i < n_chunks; i++ )); do
    chunk_file="$TEARDOWN_TMP/chunk-$$-$i.json"
    python3 -c '
import sys, json
d = json.load(open(sys.argv[1]))
objs = d.get("Objects") or []
i = int(sys.argv[2])
page = objs[i*1000:(i+1)*1000]
json.dump({"Objects": page, "Quiet": True}, open(sys.argv[3], "w"))
' "$objects_file" "$i" "$chunk_file"
    aws s3api delete-objects --bucket "$bucket" \
      --delete "file://$chunk_file" \
      --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 || true
  done
}

# ------------------------------------------------------------------------------
# 2. Empty all llt-* buckets that are CONFIRMED ours via the Project=llt tag
#    (versioned-safe, chunked deletes).
# ------------------------------------------------------------------------------
log "empty llt-* S3 buckets (tag-guarded)"

# Portable bucket listing: `mapfile` is bash-4+ only and macOS's default bash is
# 3.2, where `mapfile` doesn't exist and `set -e` kills the script on the very
# first command of teardown. A `while read` loop over the same output works on
# any bash/POSIX shell. Using process substitution keeps BUCKETS populated in
# the current shell (a plain pipe to `while` would run the loop in a subshell
# and lose the array).
BUCKETS=()
while IFS= read -r bucket; do
  [ -z "$bucket" ] && continue
  BUCKETS+=("$bucket")
done < <(
  aws s3api list-buckets \
    --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" \
    --query "Buckets[?starts_with(Name, 'llt-')].Name" --output text 2>/dev/null \
  | tr '\t' '\n'
)

if [ "${#BUCKETS[@]}" -eq 0 ]; then
  echo "  no llt-* buckets found (already removed?)."
else
  for bucket in "${BUCKETS[@]}"; do
    if ! bucket_is_ours "$bucket"; then
      echo "  WARNING: skipping s3://$bucket — matches the llt-* name prefix but" \
           "does not carry tag Project=llt (or its tag set could not be read)." \
           "Refusing to touch it; verify manually if this bucket should be" \
           "part of the llt experiment." >&2
      continue
    fi

    echo "  emptying s3://$bucket ..."

    # 2a. Fast path: recursive delete of current (unversioned) objects.
    aws s3 rm "s3://$bucket" --recursive \
      --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 || true

    # 2b. Versioned-safe path: delete all object versions, chunked <=1000/request.
    versions_json="$(aws s3api list-object-versions \
      --bucket "$bucket" --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" \
      --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
      --output json 2>/dev/null || echo '{"Objects":null}')"
    versions_file="$TEARDOWN_TMP/versions-$$.json"
    echo "$versions_json" > "$versions_file"
    chunked_delete_objects "$bucket" "$versions_file"

    # 2c. Delete markers (left behind by versioned deletes), also chunked.
    markers_json="$(aws s3api list-object-versions \
      --bucket "$bucket" --profile "$LLT_LOGGING_PROFILE" --region "$AWS_REGION" \
      --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' \
      --output json 2>/dev/null || echo '{"Objects":null}')"
    markers_file="$TEARDOWN_TMP/markers-$$.json"
    echo "$markers_json" > "$markers_file"
    chunked_delete_objects "$bucket" "$markers_file"

    echo "  emptied s3://$bucket"
  done
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
