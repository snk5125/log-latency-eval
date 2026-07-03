# Logging Pipeline Hop-Latency Evaluation (`llt`)

Reproducible AWS experiment measuring **per-hop delivery latency** introduced as
additional hops are added to a logging pipeline, and whether **event volume**
changes those results. Two forwarding agents (Vector, Cribl Edge) and two
aggregator stacks (Vector, Cribl Stream) are exercised across four pipeline
topologies at three volume tiers on both Linux and Windows hosts.

> **Status:** Build phase. Infrastructure artifacts are authored but **no
> deployment is authorized yet.** See [`PLAN.md`](PLAN.md) §8. `report/REPORT.md`
> is a template; all results-dependent statements are marked `[PENDING RESULTS]`.

## Research Question

> Using the four test cases below, how much average latency is introduced as
> additional hops are added to logging pipeline architecture, and what role, if
> any, does total volume of events have in those results?

Latency is measured **at each hop** after events leave the generator, not only
end-to-end. See [`PLAN.md`](PLAN.md) §1 for the authoritative statement.

### Scenarios (test cases)

| ID | Path |
|----|------|
| S1 | Host → Aggregator → S3 (final) |
| S2 | Host → Aggregator-T1 → Aggregator-T2 → S3 (final) |
| S3 | Host → S3 (landing) → Aggregator → S3 (final) |
| S4 | Host → S3 (landing) → Aggregator-T1 → Aggregator-T2 → S3 (final) |

## Architecture Summary

Two AWS accounts in a single region (`us-east-2` default). The **sender account**
holds the generator hosts and their forwarding agents; the **logging account**
holds the aggregator tiers, the Cribl leader, and the S3 buckets. Cross-account
agent→aggregator traffic traverses **PrivateLink** (interface endpoints → VPC
endpoint services → internal NLBs). S3/S4 landing traffic and final delivery go
to logging-account S3 buckets; landing objects trigger **S3 event notifications →
SQS**, which the aggregators' S3 sources consume for event-driven pickup.

```
 SENDER ACCOUNT  (VPC llt-sender-vpc 10.10.0.0/16, private subnets only)
 ┌───────────────────────────────────────────────────────────────────────┐
 │  Generators (m6i.large, no public IP):                                  │
 │   Linux+Vector   Linux+CriblEdge   Windows+Vector   Windows+CriblEdge   │
 │        │  (only the pair matching the run's agent dimension emits)      │
 │        │                                                                │
 │        ├── HTTP/1.1 POST NDJSON ─┐        ┌── S3 PutObject (S3/S4) ──┐   │
 │        │                         │        │                         │   │
 │   [Interface endpoints /         │   [S3 gateway endpoint] ─────────┼─► │
 │    PrivateLink: 1 per            │                                  │   │
 │    aggregator technology ]       │        [SSM interface endpoints: │   │
 │        │                         │         ssm, ssmmessages,        │   │
 └────────┼─────────────────────────┼─────────ec2messages ────────────┼───┘
          │  (PrivateLink)          │                                  │
 ═════════╪═════════════════════════╪══════════════════════════════════╪══
          ▼  cross-account boundary  ▼  (S3 data plane)                 ▼
 LOGGING ACCOUNT (VPC llt-logging-vpc 10.20.0.0/16, 2 AZs)
 ┌───────────────────────────────────────────────────────────────────────┐
 │  [VPC endpoint services front Tier-1 NLBs only]                         │
 │        │                                                                │
 │   ┌────▼───────────────┐          ┌─────────────────────┐               │
 │   │ Vector agg stack   │          │ Cribl Stream stack  │               │
 │   │  T1 NLB :8080      │          │  leader (m6i.large)  │               │
 │   │  2× m6i.xlarge     │          │  T1 NLB :8080        │               │
 │   │       │ (T2 NLB,   │          │  2× worker m6i.xlarge│               │
 │   │       ▼  internal) │          │       │ (T2 NLB,     │               │
 │   │  T2 NLB :8081      │          │       ▼  internal)   │               │
 │   │  2× m6i.xlarge     │          │  T2 NLB :8081        │               │
 │   └────┬───────────────┘          │  2× worker m6i.xlarge│               │
 │        │                          └────┬─────────────────┘               │
 │        │  final S3 PutObject           │                                 │
 │        ▼                               ▼                                 │
 │   ┌──────────────────────────────────────────────────────────┐          │
 │   │ S3: llt-landing-vagg-<acct>  llt-landing-cs-<acct>         │          │
 │   │     └─► S3 event notify ─► SQS (llt-landing-*-q) ──┐       │          │
 │   │ S3: llt-final-<acct>   final/{run_id}/{host_os}/…  │       │          │
 │   │ S3: llt-artifacts-<acct>                           │       │          │
 │   └────────────────────────────────────────────────────┼──────┘          │
 │   aggregator S3 sources consume landing objects ◄───────┘  (event-driven) │
 └───────────────────────────────────────────────────────────────────────┘
```

All agent→aggregator and aggregator→aggregator hops use **HTTP/1.1 POST of
NDJSON over the NLBs** for wire-protocol parity (`PLAN.md` §4.4). S3 sink
batching is a controlled constant: **5 s timeout / 10 MB max** at →S3 hops,
**1 s** on inter-aggregator HTTP sinks (`PLAN.md` §4.5). Full detail in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Prerequisites

- **AWS: two accounts, two named CLI profiles** — one for the sender account,
  one for the logging account. Supplied via a gitignored `terraform.tfvars`
  (`aws.sender` and `aws.logging` aliased providers; `PLAN.md` §4.1).
- **Terraform** ≥ 1.6, AWS provider `~> 5.x` (`PLAN.md` §7).
- **Ansible** ≥ 2.15 with `amazon.aws` and `community.aws` collections; the
  `aws_ssm` connection plugin and the AWS Session Manager plugin (SSM-only
  management, no SSH/WinRM ingress).
- **AWS CLI v2** configured with both profiles; **Session Manager plugin**.
- **Python** ≥ 3.11 for the harness (`boto3`; generator runs on Linux and
  Windows Python).
- Sufficient account quotas for ~14 EC2 instances plus 4 internal NLBs, VPC
  endpoints, and 2 VPC endpoint services across two accounts.

### Cost warning (ESTIMATE — verify against current AWS pricing)

> **This experiment provisions live, billed AWS infrastructure across two
> accounts.** The figures below are a **rough order-of-magnitude estimate only**
> and are **not a quote.** Verify every line against current AWS pricing for
> your region before deploying, and always run `scripts/teardown.sh` when done.

Instance inventory from `PLAN.md` §4 (14 EC2 instances):

| Count | Type | Role | Account |
|------:|------|------|---------|
| 4 | `m6i.large` | Generator hosts (2 Linux, 2 Windows) | sender |
| 4 | `m6i.xlarge` | Vector aggregator T1+T2 | logging |
| 1 | `m6i.large` | Cribl Stream leader | logging |
| 4 | `m6i.xlarge` | Cribl Stream workers T1+T2 | logging |
| 1 | `m6i.large` | (spare/leader accounting per §4.3) | logging |

[Unverified] Using representative on-demand `us-east-2` Linux rates in the
neighborhood of ~$0.096/hr (`m6i.large`) and ~$0.192/hr (`m6i.xlarge`), the EC2
compute alone is roughly:

- `m6i.large` × 6 ≈ **~$0.58/hr** (Windows instances carry an additional
  per-hour OS license fee not included here)
- `m6i.xlarge` × 8 ≈ **~$1.54/hr**
- **EC2 compute subtotal ≈ ~$2.1/hr** *(estimate; Linux-rate basis)*

[Unverified] **Not included in that subtotal** and materially additive: Windows
Server per-hour licensing on the 2 Windows generators, 4 internal Network Load
Balancers (hourly + LCU), VPC interface endpoints and 2 VPC endpoint services
(hourly + data processing), NAT gateway (if left enabled), S3 storage/requests,
SQS requests, and cross-AZ data transfer. A realistic all-in figure is
**plausibly ~$3–6/hr while running**; treat this only as a planning band and
confirm with the AWS Pricing Calculator. A full 48-run sequential pass is
budgeted at ~12 h (~6 h if the two stacks run in parallel; `PLAN.md` §8).

## Quick Start

```bash
# 0. Populate terraform/terraform.tfvars from the *.example (two AWS profiles).
# 1. Preflight + provision + configure (deploy is user-authorized; see PLAN §8):
scripts/setup.sh          # preflight.sh → terraform apply → ansible site.yml

# 2. Execute the 48-run matrix over SSM:
python harness/orchestrator/run_matrix.py            # sequential (~12 h)
python harness/orchestrator/run_matrix.py --parallel-stacks   # ~6 h

# 3. Pull final/landing objects and compute per-hop statistics:
python harness/analysis/analyze.py                   # → report/evidence/

# 4. Tear everything down (empties buckets first):
scripts/teardown.sh
```

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for the full operational procedure,
clock-sync verification, monitoring, and failure modes.

## Repository Map

```
latency-testing/
├── PLAN.md              # binding spec — read first
├── README.md            # this file (entry point)
├── .gitignore
├── terraform/           # infra: two aliased providers, per-concern modules
├── ansible/             # host config: time-sync, agents, aggregators, clocks
├── harness/             # eventgen.py, run_matrix.py, scenarios.yaml, analyze.py
├── scripts/             # setup.sh, teardown.sh, preflight.sh
├── report/
│   ├── REPORT.md        # formal engineering report (template until results)
│   └── evidence/        # run manifests, clock assertions, analyzer output
└── docs/
    ├── ARCHITECTURE.md  # component walkthrough + per-scenario data paths
    └── RUNBOOK.md       # operational step-by-step
```

## Documentation

- **[`PLAN.md`](PLAN.md)** — the binding specification (scenarios, matrix, EPS
  tiers, batch constants, measurement methodology, caveats). Source of truth.
- **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — detailed component
  walkthrough and the four scenario data paths with per-hop timestamp capture.
- **[`docs/RUNBOOK.md`](docs/RUNBOOK.md)** — deploy → verify → run → collect →
  analyze → teardown, with failure modes.
- **[`report/REPORT.md`](report/REPORT.md)** — the formal engineering report
  template; methodology and caveats are complete, findings are `[PENDING]`.
