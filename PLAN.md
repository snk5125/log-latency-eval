# Logging Pipeline Hop-Latency Evaluation — Engineering Plan (SPEC)

**Status:** Build phase (artifacts only — no deployment authorized yet)
**Repo codename / resource prefix:** `llt` (logging latency test)
**Date:** 2026-07-03

This document is the single source of truth for the build. All Terraform, Ansible,
harness, and documentation components MUST conform to the conventions defined here.

---

## 1. Research Question

> Using the four test cases below, how much average latency is introduced as
> additional hops are added to logging pipeline architecture, and what role, if
> any, does total volume of events have in those results?

Delivery latency is evaluated **at each hop** after events leave the generation
source, not only end-to-end.

## 2. Test Cases (Scenarios)

| ID | Path |
|----|------|
| S1 | Host → Aggregator → S3 (final) |
| S2 | Host → Aggregator-T1 → Aggregator-T2 → S3 (final) |
| S3 | Host → S3 (landing) → Aggregator → S3 (final) |
| S4 | Host → S3 (landing) → Aggregator-T1 → Aggregator-T2 → S3 (final) |

## 3. Experiment Matrix

| Dimension | Values | Count |
|-----------|--------|-------|
| Scenario | S1, S2, S3, S4 | 4 |
| Forwarding agent (on host) | Vector, Cribl Edge | 2 |
| Aggregator stack | Vector, Cribl Stream | 2 |
| Volume tier (per generating host) | 1,000 / 5,000 / 10,000 EPS | 3 |
| Host OS (concurrent within each run, analyzed as a dimension) | Linux, Windows | 2 |

Total orchestrated runs: 4 × 2 × 2 × 3 = **48 runs**. Linux and Windows hosts
run concurrently within each run and are separated at analysis time via the
`host_os` field. Each run: **2 min warm-up (excluded) + 10 min measurement**.

**Run ID convention:** `s{1-4}-{vec|ce}-{vagg|cs}-{1k|5k|10k}-{YYYYMMDDTHHMMSSZ}`
(agent: `vec`=Vector, `ce`=Cribl Edge; aggregator: `vagg`=Vector, `cs`=Cribl Stream).

## 4. Architecture

### 4.1 Accounts and region

- **Sender account** — generator hosts (Linux + Windows) with forwarding agents.
- **Logging account** — aggregator tiers, Cribl leader, landing + final S3 buckets.
- Single region for both accounts (default `us-east-2`, variable `aws_region`).
- Terraform uses two aliased providers: `aws.sender`, `aws.logging`, driven by
  named CLI profiles supplied in a gitignored `terraform.tfvars`.

### 4.2 Sender account

- VPC `llt-sender-vpc` 10.10.0.0/16, 2 AZs, private subnets only. No IGW/NAT for
  data path; a NAT gateway is permitted solely for package installation and may
  be disabled via variable after provisioning.
- Hosts (all in private subnets, no public IPs):
  - 2 × Linux generator hosts (Amazon Linux 2023, `m6i.large`)
  - 2 × Windows generator hosts (Windows Server 2022, `m6i.large`)
  - One Linux + one Windows host runs Vector agent; the other pair runs
    Cribl Edge. Only the pair matching the run's `agent` dimension generates
    during a run (orchestrator starts/stops generators via SSM).
- Management: **SSM only** (VPC interface endpoints: `ssm`, `ssmmessages`,
  `ec2messages`). No SSH/WinRM ingress. Ansible uses the `aws_ssm` connection
  plugin with an artifacts S3 bucket for file transfer.
- **S3 gateway endpoint** in the sender VPC — data path for scenarios S3/S4
  (agent writes directly to the landing buckets in the logging account) and for
  SSM file transfer.
- **Interface endpoints (PrivateLink)** to the two aggregator endpoint services
  in the logging account (one per aggregator technology).

### 4.3 Logging account

- VPC `llt-logging-vpc` 10.20.0.0/16, 2 AZs.
- **Vector aggregator stack:** Tier-1: 2 × `m6i.xlarge` behind internal NLB
  `llt-vagg-t1-nlb`; Tier-2: 2 × `m6i.xlarge` behind internal NLB `llt-vagg-t2-nlb`.
- **Cribl Stream stack:** Leader 1 × `m6i.large`; worker group `t1`: 2 ×
  `m6i.xlarge` behind `llt-cs-t1-nlb`; worker group `t2`: 2 × `m6i.xlarge`
  behind `llt-cs-t2-nlb`. Self-hosted, **Cribl Free license** (≤1 TB/day —
  verify current terms at deploy time; all tiers are well below this).
- **VPC endpoint services** (PrivateLink) front the two Tier-1 NLBs only;
  Tier-1 → Tier-2 traffic stays inside the logging VPC via the Tier-2 NLBs.
- **S3 buckets** (all logging account, SSE-S3, versioning off, lifecycle 7-day expiry):
  - `llt-landing-vagg-<acct>` and `llt-landing-cs-<acct>` — S3/S4 landing;
    bucket policies allow `s3:PutObject` from sender-account host roles.
  - `llt-final-<acct>` — final destination, keyed
    `final/{run_id}/{host_os}/...`.
  - `llt-artifacts-<acct>` — SSM/Ansible transfer, harness distribution, results.
- **SQS queues** `llt-landing-vagg-q`, `llt-landing-cs-q` fed by S3 event
  notifications on the landing buckets; aggregator S3 sources consume via SQS
  (Vector `aws_s3` source; Cribl Stream S3 source), giving event-driven (not
  poll-interval-driven) pickup.
- S3 gateway endpoint in the logging VPC.

### 4.4 Wire protocol — parity rule

All agent→aggregator and aggregator→aggregator hops use **HTTP/1.1 POST of
NDJSON over the NLBs** (Vector `http` source/sink; Cribl HTTP Raw source /
Webhook destination). Rationale: a single vendor-neutral transport keeps the
full 2×2 agent×aggregator matrix valid and isolates *hop count* as the variable
rather than vendor wire-protocol differences. Native protocols (Vector gRPC,
Cribl TCP-JSON) are explicitly documented as **not assessed / future work**.

Ports: Tier-1 listens `8080`, Tier-2 listens `8081` (both via their NLBs).
TLS is disabled on internal hops for this experiment (documented constraint;
traffic never leaves AWS private networking).

### 4.5 Sink batching — controlled constant

S3 sink flushing dominates latency at any →S3 hop. Both tools MUST use
identical batch settings everywhere: **batch timeout 5 s, max batch size
10 MB** (Vector `batch.timeout_secs=5`; Cribl S3 destination equivalents).
Inter-aggregator HTTP sinks: batch timeout 1 s. These constants are part of
the methodology and must be reported.

## 5. Latency Measurement Methodology

### 5.1 Event schema (NDJSON, ~512 bytes padded)

```json
{
  "run_id": "s2-vec-vagg-5k-20260710T140000Z",
  "seq": 123456,
  "host_id": "llt-lin-vec-01",
  "host_os": "linux",
  "t_gen_ns": 1760000000000000000,
  "hop_ts": {},
  "pad": "…fixed-length filler to normalize event size…"
}
```

- `t_gen_ns`: wall-clock nanoseconds at generation (`CLOCK_REALTIME` /
  .NET `DateTime.UtcNow` ticks converted), written by the generator.
- Each processing hop appends a receive timestamp into `hop_ts` via the tool's
  transform layer (Vector VRL `remap`; Cribl Eval function):
  `hop_ts.agent`, `hop_ts.agg1`, `hop_ts.agg2` (ms precision — tool-native).
- S3 hops are timestamped from the S3 object's `LastModified`/PutObject time
  (landing) and the final object's PutObject time, retrieved at analysis time.

### 5.2 Per-hop latency derivation

| Hop | Derivation |
|-----|-----------|
| Generation → agent | `hop_ts.agent − t_gen` |
| Agent → Aggregator-T1 (S1/S2) | `hop_ts.agg1 − hop_ts.agent` |
| Agent → landing S3 (S3/S4) | landing object PutObject − `hop_ts.agent` |
| Landing S3 → Aggregator (S3/S4) | `hop_ts.agg1` − landing PutObject |
| T1 → T2 (S2/S4) | `hop_ts.agg2 − hop_ts.agg1` |
| Last aggregator → final S3 | final object PutObject − last `hop_ts` |
| End-to-end | final PutObject − `t_gen` |

Statistics per run × OS × hop: **mean, p50, p90, p99, max, stddev, count,
loss rate** (sent seq vs. landed seq). Averages answer the research question;
percentiles defend it under peer review.

### 5.3 Clock discipline

- Linux: chrony → AWS Time Sync (`169.254.169.123`).
- Windows: w32time → AWS Time Sync, 64 s poll floor.
- Ansible asserts sync before each run (chrony tracking offset < 1 ms; w32tm
  /query /status stripchart bound < 5 ms) and records the values as run
  evidence. Clock error bounds are reported as a stated constraint.

### 5.4 Known caveats to document in the report

1. S3-hop latency includes the fixed 5 s batch flush — hop deltas are reported
   both raw and batch-adjusted.
2. `hop_ts.*` are ms-precision (tool-native); sub-ms effects unresolvable there.
3. PutObject `LastModified` is second-precision; analysis uses SQS/S3 event
   notification `eventTime` (ms) where available.
4. Windows time sync is coarser than chrony; Windows deltas carry wider error bars.
5. Variability areas NOT assessed (report section): native wire protocols, TLS
   overhead, compression, aggregator processing pipelines beyond timestamping,
   instance-type sensitivity, cross-AZ vs same-AZ placement, cross-region,
   Kinesis/Kafka intermediaries, backpressure/disk-buffer behavior under
   sustained overload, S3 request-rate partitioning, Windows Event Log channel
   ingestion (file-based generation used for OS parity).

## 6. Repository Layout

```
latency-testing/
├── PLAN.md                  # this spec
├── README.md                # replication instructions (entry point)
├── .gitignore
├── terraform/
│   ├── main.tf …            # root module, two aliased providers
│   ├── terraform.tfvars.example
│   └── modules/
│       ├── sender-network/  ├── logging-network/  ├── privatelink/
│       ├── generator-hosts/ ├── vector-aggregator/├── cribl-stream/
│       ├── s3-buckets/      ├── sqs-notify/       └── iam/
├── ansible/
│   ├── ansible.cfg
│   ├── inventories/aws_ec2.yml         # dynamic inventory (tag-based)
│   ├── playbooks/            # site.yml, configure-scenario.yml, assert-clocks.yml
│   └── roles/
│       ├── common/  ├── time-sync/  ├── event-generator/
│       ├── vector-agent/  ├── cribl-edge/
│       ├── vector-aggregator/  ├── cribl-leader/  └── cribl-worker/
├── harness/
│   ├── generator/eventgen.py           # cross-platform (Linux + Windows py)
│   ├── orchestrator/run_matrix.py      # drives 48 runs via SSM
│   ├── orchestrator/scenarios.yaml
│   └── analysis/analyze.py             # pulls final/landing objects, stats
├── scripts/
│   ├── setup.sh              # preflight → terraform apply → ansible site.yml
│   ├── teardown.sh           # empty buckets → terraform destroy
│   └── preflight.sh          # tool/credential checks
├── report/
│   ├── REPORT.md             # formal engineering report (template until results)
│   └── evidence/             # run manifests, clock assertions, raw stats (gitignored data)
└── docs/
    ├── ARCHITECTURE.md       # diagrams + component detail
    └── RUNBOOK.md            # operational step-by-step
```

## 7. Conventions (binding on all components)

- Resource names/tags: prefix `llt-`; tags `Project=llt`,
  `Role=<generator|agg-t1|agg-t2|leader>`, `Stack=<vector|cribl>`, `Os=<linux|windows>`.
  Ansible dynamic inventory groups derive from these tags.
- All Terraform variables have descriptions; every resource/module has a
  comment explaining *why it exists in the experiment*, not just what it is.
- Ansible: every task named; role defaults documented in `defaults/main.yml`.
- No secrets in the repo. `terraform.tfvars`, state, keys, retrieved evidence
  data, and Cribl auth artifacts are gitignored. `*.example` files show shape.
- Scenario switching is config-only (Ansible re-templates agent/aggregator
  configs per run); infrastructure is deployed once for all 48 runs.
- Version pinning (record exact versions in report): Vector and Cribl versions
  pinned in Ansible defaults; Terraform AWS provider `~> 5.x`; instance AMIs
  resolved via SSM parameters at apply time and recorded in state/evidence.

## 8. Execution Phases

1. **Build (this session):** all artifacts authored, verified, committed. ⏸ PAUSE.
2. **Deploy:** `scripts/setup.sh` (user-authorized; two AWS profiles required).
3. **Run:** `harness/orchestrator/run_matrix.py` executes 48 runs (~12 h
   sequential; Vector and Cribl stacks may run in parallel to halve wall time).
4. **Analyze:** `harness/analysis/analyze.py` → stats tables + charts into `report/evidence/`.
5. **Report:** finalize `report/REPORT.md` with findings + citations.
6. **Teardown:** `scripts/teardown.sh`.

## 9. Build Work Split (subagents)

| Agent | Owns | Must not touch |
|-------|------|----------------|
| A | `terraform/` | everything else |
| B | `ansible/` | everything else |
| C | `harness/`, `scripts/` | everything else |
| D | `README.md`, `report/`, `docs/` | everything else |

All four read this PLAN.md first and conform to §4–§7 exactly.
