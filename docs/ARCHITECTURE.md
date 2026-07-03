# Architecture — Logging Pipeline Hop-Latency Evaluation (`llt`)

This document is the detailed component walkthrough for a peer reviewer. It
elaborates the topology summarized in the [README](../README.md) and specified
authoritatively in [`PLAN.md`](../PLAN.md) §4. Where this document and `PLAN.md`
disagree, `PLAN.md` governs.

---

## 1. Two-Account Topology

> **Diagram:** [`docs/diagrams/topology.mermaid`](diagrams/topology.mermaid) /
> [`.svg`](diagrams/topology.svg) — rendered version of everything in this
> section (both VPCs/subnets/AZs, SSM + S3 endpoints, PrivateLink endpoint
> services ↔ interface endpoints, all four NLBs, aggregator fleets, leader, S3
> buckets, SQS queues).

The experiment deliberately splits the pipeline across **two AWS accounts** in a
**single region** (`us-east-2` default, `aws_region` variable; `PLAN.md` §4.1).
Terraform drives both with aliased providers `aws.sender` and `aws.logging`,
each bound to a named CLI profile supplied in a gitignored `terraform.tfvars`.
The two-account split mirrors a realistic producer/collector separation and
forces cross-account traffic onto **PrivateLink**, which is itself part of the
path under measurement.

### 1.1 Sender VPC (`llt-sender-vpc`, 10.10.0.0/16)

- Two AZs, **private subnets only**. No IGW/NAT on the data path. A NAT gateway
  is permitted **solely for package installation** and may be disabled via
  variable after provisioning (`PLAN.md` §4.2). Keeping the data path off the
  public internet removes internet-path jitter as a confound.
- **Generator hosts** (all private, no public IPs; `m6i.large`):
  - 2 × Linux (Amazon Linux 2023) — one running **Vector agent**, one **Cribl Edge**.
  - 2 × Windows (Windows Server 2022) — one **Vector agent**, one **Cribl Edge**.
  - Only the pair matching the run's `agent` dimension emits during a run; the
    orchestrator starts/stops generators via SSM.
- **Management: SSM only.** VPC interface endpoints `ssm`, `ssmmessages`,
  `ec2messages`. **No SSH/WinRM ingress.** Ansible uses the `aws_ssm` connection
  plugin with an artifacts S3 bucket for file transfer.
- **S3 gateway endpoint** — carries the S3/S4 data path (agents write directly
  to landing buckets in the logging account) and SSM file transfer.
- **Interface endpoints (PrivateLink)** — one per aggregator technology,
  pointing at the two VPC endpoint services in the logging account.

### 1.2 Logging VPC (`llt-logging-vpc`, 10.20.0.0/16)

- Two AZs.
- **Vector aggregator stack** — Tier-1: 2 × `m6i.xlarge` behind internal NLB
  `llt-vagg-t1-nlb`; Tier-2: 2 × `m6i.xlarge` behind internal NLB
  `llt-vagg-t2-nlb`.
- **Cribl Stream stack** — Leader 1 × `m6i.large`; worker group `t1`: 2 ×
  `m6i.xlarge` behind `llt-cs-t1-nlb`; worker group `t2`: 2 × `m6i.xlarge`
  behind `llt-cs-t2-nlb`. Self-hosted under the **Cribl Free license**
  (≤ 1 TB/day — verify current terms at deploy time; all tiers are well below
  this).
- **VPC endpoint services (PrivateLink)** front the **Tier-1 NLBs only**.
  Tier-1 → Tier-2 traffic stays **inside** the logging VPC via the Tier-2 NLBs;
  it never crosses the account boundary.
- **S3 gateway endpoint** in the logging VPC.

---

## 2. Aggregator Stacks (component detail)

### 2.1 Vector aggregator stack

Each tier is a homogeneous pair of `m6i.xlarge` instances behind an internal
NLB. Tier-1 receives from the PrivateLink-exposed endpoint service on port
**8080**; Tier-2 receives from Tier-1 on port **8081** via its internal NLB.
Each Vector instance runs an `http` **source** (NDJSON) and, per scenario, an
`http` **sink** (to the next tier) or an `aws_s3` **sink** (to a final bucket).
A VRL `remap` transform appends the tier's receive timestamp to `hop_ts`
(`hop_ts.agg1` on Tier-1, `hop_ts.agg2` on Tier-2). For S3/S4 the aggregator's
**input** is an `aws_s3` **source** consuming from SQS, not an HTTP source.

### 2.2 Cribl Stream stack

A single **leader** (`m6i.large`) manages two worker groups. Worker group `t1`
(2 × `m6i.xlarge`) sits behind `llt-cs-t1-nlb`; worker group `t2` behind
`llt-cs-t2-nlb`. Workers run an **HTTP Raw source** (parity with Vector's `http`
source) and a **Webhook destination** (to the next tier) or an **S3
destination** (to a bucket). A **Eval function** appends the receive timestamp
to `hop_ts`. For S3/S4 the worker input is an **S3 source** driven by SQS. The
leader is on the management path only and is not itself a data hop.

### 2.3 PrivateLink exposure

Only the two **Tier-1** NLBs are fronted by VPC endpoint services (one per
aggregator technology). The sender VPC reaches them through interface endpoints.
This is what makes the agent→aggregator hop a cross-account, PrivateLink-mediated
hop while keeping the tier-to-tier hop intra-VPC — isolating the added hop rather
than adding a second account crossing.

### 2.4 S3 / SQS event-driven pickup

Landing buckets (`llt-landing-vagg-<acct>`, `llt-landing-cs-<acct>`) emit **S3
event notifications** into SQS queues (`llt-landing-vagg-q`, `llt-landing-cs-q`).
The aggregator S3 sources (Vector `aws_s3` source; Cribl Stream S3 source)
**consume via SQS**, giving **event-driven** pickup rather than poll-interval
latency. This matters for S3/S4: the landing→aggregator hop latency reflects
notification delivery, not a scan cadence.

### 2.5 S3 buckets

All in the logging account, SSE-S3, versioning off, 7-day lifecycle expiry
(`PLAN.md` §4.3):

| Bucket | Purpose |
|--------|---------|
| `llt-landing-vagg-<acct>` / `llt-landing-cs-<acct>` | S3/S4 landing; policy allows `s3:PutObject` from sender-account host roles |
| `llt-final-<acct>` | Final destination, keyed `final/{run_id}/{host_os}/...` |
| `llt-artifacts-<acct>` | SSM/Ansible transfer, harness distribution, results |

---

## 3. Scenario Data Paths (per-hop timestamp capture)

> **Diagrams:** each scenario below also has a rendered Mermaid sequence
> diagram — [`docs/diagrams/scenario-s1.mermaid`](diagrams/scenario-s1.mermaid)
> … [`scenario-s4.mermaid`](diagrams/scenario-s4.mermaid) (+ matching `.svg`) —
> with every hop and its timestamp capture point labeled (`PLAN.md` §5A).

Each sequence marks the point at which each timestamp is captured. `t_gen` is
written by the generator; `hop_ts.agent`, `hop_ts.agg1`, `hop_ts.agg2` are
appended by the tool's transform on receive (ms precision); S3 hops are
timestamped from the object PutObject time at analysis time — the analyzer
prefers an `x-amz-meta-llt-put-ms` object-metadata header (ms) where a sink
sets it, and otherwise falls back to `LastModified` (second-precision). Neither
sink currently writes that metadata, so `LastModified` is the operative path
today (`PLAN.md` §5.1, §5.4 item 3). Per-hop derivations are in §4 below and
`PLAN.md` §5.2.

### S1 — Host → Aggregator → S3 (final)

```
 Generator          Agent            Aggregator            Final S3
    │                 │                   │                    │
    │ writes t_gen    │                   │                    │
    ├─ event file ───►│                   │                    │
    │        [hop_ts.agent captured on agent receive]          │
    │                 │ HTTP POST NDJSON  │                    │
    │                 ├──── :8080 ───────►│                    │
    │                 │      [hop_ts.agg1 captured on agg recv] │
    │                 │                   │ S3 sink (5s/10MB)  │
    │                 │                   ├─── PutObject ─────►│
    │                 │                   │   [final PutObject time captured]
```

### S2 — Host → Aggregator-T1 → Aggregator-T2 → S3 (final)

```
 Generator      Agent          Agg-T1           Agg-T2          Final S3
    │             │               │                │               │
    │ t_gen       │               │                │               │
    ├─ event ────►│               │                │               │
    │      [hop_ts.agent]         │                │               │
    │             │ POST :8080    │                │               │
    │             ├──────────────►│                │               │
    │             │        [hop_ts.agg1]           │               │
    │             │               │ POST :8081     │               │
    │             │               ├───────────────►│               │
    │             │               │         [hop_ts.agg2]          │
    │             │               │                │ S3 sink 5s/10MB│
    │             │               │                ├── PutObject ──►│
    │             │               │                │    [final PutObject time]
```

### S3 — Host → S3 (landing) → Aggregator → S3 (final)

```
 Generator    Agent       Landing S3          Aggregator        Final S3
    │           │             │                    │               │
    │ t_gen     │             │                    │               │
    ├─ event ──►│             │                    │               │
    │    [hop_ts.agent]       │                    │               │
    │           │ S3 sink 5s/10MB                  │               │
    │           ├── PutObject►│                    │               │
    │           │      [landing PutObject time captured]      │
    │           │             │ S3 event → SQS     │               │
    │           │             ├───────────────────►│ (event-driven)│
    │           │             │              [hop_ts.agg1]         │
    │           │             │                    │ S3 sink 5s/10MB│
    │           │             │                    ├── PutObject ──►│
    │           │             │                    │   [final PutObject time]
```

### S4 — Host → S3 (landing) → Aggregator-T1 → Aggregator-T2 → S3 (final)

```
 Generator  Agent    Landing S3       Agg-T1         Agg-T2        Final S3
    │         │          │               │              │             │
    │ t_gen   │          │               │              │             │
    ├─ event►│          │               │              │             │
    │  [hop_ts.agent]    │               │              │             │
    │         │ S3 sink 5s/10MB          │              │             │
    │         ├─PutObject►│               │              │             │
    │         │   [landing PutObject time]         │             │
    │         │          │ S3 event→SQS  │              │             │
    │         │          ├──────────────►│ (event-driven)             │
    │         │          │         [hop_ts.agg1]        │             │
    │         │          │               │ POST :8081   │             │
    │         │          │               ├─────────────►│             │
    │         │          │               │        [hop_ts.agg2]       │
    │         │          │               │              │ S3 sink 5s/10MB
    │         │          │               │              ├─ PutObject ►│
    │         │          │               │              │  [final PutObject time]
```

---

## 4. Per-Hop Latency Derivation

> **Diagram:** [`docs/diagrams/measurement.mermaid`](diagrams/measurement.mermaid)
> / [`.svg`](diagrams/measurement.svg) — visual form of the table below,
> including the raw-vs-batch-adjusted note (`PLAN.md` §5A, §5.4 item 1).

Reproduced from `PLAN.md` §5.2 for reviewer convenience:

| Hop | Derivation |
|-----|-----------|
| Generation → agent | `hop_ts.agent − t_gen` |
| Agent → Aggregator-T1 (S1/S2) | `hop_ts.agg1 − hop_ts.agent` |
| Agent → landing S3 (S3/S4) | landing object PutObject − `hop_ts.agent` |
| Landing S3 → Aggregator (S3/S4) | `hop_ts.agg1` − landing PutObject |
| T1 → T2 (S2/S4) | `hop_ts.agg2 − hop_ts.agg1` |
| Last aggregator → final S3 | final object PutObject − last `hop_ts` |
| End-to-end | final PutObject − `t_gen` |

Statistics per run × OS × hop: mean, p50, p90, p99, max, stddev, count, loss
rate (`PLAN.md` §5.2). Averages answer the research question; percentiles defend
it under peer review.

---

## 5. Wire-Protocol Parity Rationale (`PLAN.md` §4.4)

All agent→aggregator and aggregator→aggregator hops use **HTTP/1.1 POST of
NDJSON over the NLBs** — Vector `http` source/sink, Cribl HTTP Raw source /
Webhook destination. A single vendor-neutral transport keeps the full 2 × 2
agent × aggregator matrix valid and **isolates hop count as the variable** rather
than confounding it with vendor wire-protocol differences (e.g., Vector gRPC vs
Cribl TCP-JSON). Those native protocols are explicitly **not assessed / future
work**. Ports: Tier-1 `8080`, Tier-2 `8081`, each via its NLB. TLS is disabled
on internal hops for this experiment; traffic never leaves AWS private
networking. A peer reviewer should read every cross-tool latency comparison as a
comparison **under a common transport**, not a claim about each tool's fastest
native path.

## 6. Batch-Constant Rationale (`PLAN.md` §4.5)

**S3 sink flushing dominates latency at any →S3 hop.** If the two tools flushed
on different cadences, any →S3 hop delta would reflect batch tuning, not the
pipeline. To prevent that, both tools use **identical batch settings
everywhere**: batch **timeout 5 s, max batch size 10 MB** at →S3 hops (Vector
`batch.timeout_secs=5`; Cribl S3 destination equivalents), and **1 s** batch
timeout on inter-aggregator HTTP sinks. These constants are part of the
methodology and are reported. A consequence for the reviewer: raw →S3 hop deltas
are expected to be dominated by the ~5 s flush window; the report therefore
presents S3-hop deltas **both raw and batch-adjusted** (`PLAN.md` §5.4 item 1)
so that the *marginal* cost of an added hop remains legible under the flush
constant.
