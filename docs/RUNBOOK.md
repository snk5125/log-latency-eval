# Runbook — Logging Pipeline Hop-Latency Evaluation (`llt`)

Operational step-by-step for deploying, running, and tearing down the
experiment. Read [`PLAN.md`](../PLAN.md) and [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)
first. Deployment is **user-authorized** (`PLAN.md` §8) and provisions **billed
AWS infrastructure across two accounts** — see the cost warning in the
[README](../README.md).

All management is **SSM-only** (no SSH/WinRM ingress). Commands below assume AWS
CLI v2 with the Session Manager plugin and both named profiles configured. Where
a command targets a specific account, use the matching profile
(`--profile <sender|logging>`).

---

## 1. Deploy

```bash
# 1.1 Populate variables (two AWS profiles; account IDs). Never commit this file.
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
$EDITOR terraform/terraform.tfvars

# 1.2 Preflight, provision, configure. setup.sh chains:
#     preflight.sh → terraform apply → ansible site.yml
scripts/setup.sh
```

What `setup.sh` does (`PLAN.md` §6, §8):

1. **`preflight.sh`** — verifies tool versions (Terraform, Ansible, AWS CLI,
   Session Manager plugin, Python) and that both AWS profiles authenticate.
2. **`terraform apply`** — root module with `aws.sender` / `aws.logging`
   providers builds both VPCs, PrivateLink, NLBs, generator hosts, aggregator
   stacks, S3 buckets, SQS notifications, IAM. AMIs resolve via SSM parameters
   at apply time and are recorded in state/evidence (`PLAN.md` §7).
3. **`ansible site.yml`** — configures time-sync, generators, agents, and
   aggregators via the `aws_ssm` connection plugin. Scenario switching is
   config-only thereafter; infrastructure is deployed once for all 48 runs
   (`PLAN.md` §7).

Record exact **Vector** and **Cribl** versions now (pinned in Ansible defaults)
and the resolved **AMI IDs** into `report/REPORT.md` §4 and `report/evidence/`.

---

## 2. Verify Clock Sync (blocking gate)

Accurate cross-host timestamps are load-bearing for every hop delta. The
`assert-clocks.yml` playbook enforces the bounds from `PLAN.md` §5.3 and records
values as run evidence.

```bash
ansible-playbook ansible/playbooks/assert-clocks.yml
```

Expected bounds (`PLAN.md` §5.3):

- **Linux (chrony → AWS Time Sync `169.254.169.123`):** tracking offset **< 1 ms**.
- **Windows (w32time → AWS Time Sync, 64 s poll floor):** `w32tm /query /status`
  stripchart bound **< 5 ms**.

Manual spot-check over SSM:

```bash
# Linux host
aws ssm send-command --profile sender \
  --document-name "AWS-RunShellScript" \
  --targets "Key=tag:Role,Values=generator" "Key=tag:Os,Values=linux" \
  --parameters 'commands=["chronyc tracking"]'

# Windows host
aws ssm send-command --profile sender \
  --document-name "AWS-RunPowerShellScript" \
  --targets "Key=tag:Os,Values=windows" \
  --parameters 'commands=["w32tm /query /status"]'
```

If a host is out of bounds, do **not** start the matrix — resync and re-assert.
Windows deltas legitimately carry wider error bars (`PLAN.md` §5.4 item 4); that
is a reported constraint, not a reason to loosen the assertion.

---

## 3. Execute the Matrix

48 orchestrated runs = 4 scenarios × 2 agents × 2 aggregators × 3 volume tiers,
Linux + Windows concurrent within each run and split at analysis time
(`PLAN.md` §3). Each run = **2 min warm-up (excluded) + 10 min measurement**.
Run IDs follow `s{1-4}-{vec|ce}-{vagg|cs}-{1k|5k|10k}-{YYYYMMDDTHHMMSSZ}`.

```bash
# Sequential (~12 h): safest, one stack active at a time.
python harness/orchestrator/run_matrix.py

# Parallel stacks (~6 h): Vector and Cribl stacks run concurrently.
python harness/orchestrator/run_matrix.py --parallel-stacks
```

The orchestrator re-templates agent/aggregator configs per run (Ansible),
starts only the generator pair matching the run's `agent` dimension via SSM,
holds for warm-up + measurement, drains, then stops generators. Choose
`--parallel-stacks` only if account quotas and NLB/LCU headroom comfortably
cover both stacks emitting at once.

---

## 4. Monitor a Run

### 4.1 Service health (over SSM)

```bash
# Vector agent / aggregator status (Linux)
aws ssm send-command --profile <sender|logging> \
  --document-name "AWS-RunShellScript" \
  --targets "Key=tag:Stack,Values=vector" \
  --parameters 'commands=["systemctl is-active vector && vector top --url http://127.0.0.1:8686 || journalctl -u vector -n 50 --no-pager"]'

# Cribl worker status (Linux)
aws ssm send-command --profile logging \
  --document-name "AWS-RunShellScript" \
  --targets "Key=tag:Stack,Values=cribl" "Key=tag:Role,Values=agg-t1" \
  --parameters 'commands=["systemctl is-active cribl || /opt/cribl/bin/cribl status"]'

# Windows agent (Vector or Cribl Edge)
aws ssm send-command --profile sender \
  --document-name "AWS-RunPowerShellScript" \
  --targets "Key=tag:Os,Values=windows" \
  --parameters 'commands=["Get-Service vector,cribl* | Format-Table -Auto"]'
```

### 4.2 NLB target health

```bash
# List llt NLB target groups, then check health per group.
aws elbv2 describe-target-groups --profile logging \
  --query "TargetGroups[?starts_with(TargetGroupName,'llt')].TargetGroupArn" --output text
aws elbv2 describe-target-health --profile logging --target-group-arn <arn> \
  --query "TargetHealthDescriptions[].{T:Target.Id,S:TargetHealthState.State}"
```

All Tier-1/Tier-2 targets should be `healthy` before and during a run. An NLB
with unhealthy targets silently drops the hop and inflates loss rate.

### 4.3 SQS depth (S3/S4 event-driven pickup)

```bash
for q in llt-landing-vagg-q llt-landing-cs-q; do
  url=$(aws sqs get-queue-url --profile logging --queue-name "$q" --query QueueUrl --output text)
  aws sqs get-queue-attributes --profile logging --queue-url "$url" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
done
```

For S3/S4, a steadily **rising** `ApproximateNumberOfMessages` means the
aggregator S3 source is not keeping up (backlog) — landing→aggregator deltas
will be inflated. A flat, near-zero depth is healthy.

---

## 5. Collect and Analyze

```bash
python harness/analysis/analyze.py     # pulls final/landing objects, computes stats
```

The analyzer pulls final and landing objects, derives per-hop latencies
(`PLAN.md` §5.2), computes mean/p50/p90/p99/max/stddev/count/loss-rate per
run × OS × hop, and writes outputs plus run manifests into `report/evidence/`.
Raw per-event data (`report/evidence/raw/`, `*.jsonl`, `*.csv.gz`) is gitignored;
summarized stats and manifests are retained. Populate `report/REPORT.md` §7
Findings from the analyzer output and cite the manifests per §8 Evidence Index.

---

## 6. Common Failure Modes

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| SSM target not found / command stuck | **SSM agent not registered** (endpoints missing, IAM instance profile lacks `AmazonSSMManagedInstanceCore`, or agent not started) | Confirm the three interface endpoints (`ssm`, `ssmmessages`, `ec2messages`) exist and the instance profile is attached; restart `amazon-ssm-agent` (Linux) / `AmazonSSMAgent` service (Windows). |
| Cribl workers show no data / not in worker group | **Cribl worker not joining leader** (leader unreachable, bad auth token, wrong group tag) | Check worker→leader connectivity and the join config; verify auth artifact (gitignored) is present; confirm `Role`/`Stack` tags map the worker to the correct group. |
| Windows host emits late / service flaps | **Windows service quirks** (service start ordering, w32time not converged, PowerShell execution policy) | Verify the agent service is `Running` and set to auto-start; re-run `assert-clocks.yml`; allow the 64 s w32time poll floor to settle before measuring. |
| S3/S4 landing→aggregator latency climbing; loss rising | **SQS backlog** — aggregator S3 source not keeping up | Check SQS depth (§4.3); confirm event notifications are configured on the landing bucket; verify the S3 source consumer is running; if sustained, lower EPS tier or add worker headroom (note as constraint). |
| →S3 hops all ~5 s regardless of scenario | Expected: **batch-flush dominance** (`PLAN.md` §4.5) | Not a failure. Report S3 deltas raw **and** batch-adjusted (§5.4 item 1). |
| Clock assertion fails | Time sync drifted / not converged | Resync chrony / w32time; re-run `assert-clocks.yml`; do not start the matrix until within bounds (§2). |

---

## 7. Teardown Checklist

```bash
scripts/teardown.sh     # empties buckets → terraform destroy
```

- [ ] Confirm all analyzer output and manifests you need are already in
      `report/evidence/` (teardown deletes bucket data).
- [ ] **Empty all S3 buckets** — `llt-landing-vagg-*`, `llt-landing-cs-*`,
      `llt-final-*`, `llt-artifacts-*`. `terraform destroy` will not remove
      non-empty buckets; `teardown.sh` empties them first.
- [ ] Purge/verify SQS queues drained (avoid lingering in-flight messages).
- [ ] `terraform destroy` for **both** providers (sender and logging accounts).
- [ ] Confirm the NAT gateway (if it was re-enabled for package installs) is
      destroyed — it bills hourly.
- [ ] Verify no orphaned NLBs, VPC endpoints, or VPC endpoint services remain in
      either account (these bill hourly).
- [ ] Confirm no residual EC2 instances (especially Windows — additional OS
      license cost) are running in either account.
