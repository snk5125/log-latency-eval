# Logging Pipeline Hop-Latency Evaluation — Engineering Report

**Repo / resource prefix:** `llt` (logging latency test)
**Report status:** TEMPLATE — methodology and constraints are complete; all
results-dependent content is marked `[PENDING RESULTS]` / `[PENDING]` and must be
filled only from `harness/analysis/analyze.py` output after execution. No numbers
in this document are fabricated.

Authoritative specification: [`../PLAN.md`](../PLAN.md). Architecture detail:
[`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md). Operations:
[`../docs/RUNBOOK.md`](../docs/RUNBOOK.md).

---

## 1. Executive Summary

`[PENDING RESULTS]` — Complete after analysis. This section will state, in
plain terms: (a) how much average latency each additional hop adds, per scenario;
(b) whether event volume (1k / 5k / 10k EPS) measurably changes those additions;
(c) the Linux vs Windows split; and (d) the principal constraints that bound the
conclusions. All quantitative claims here must trace to §7 Findings and the
evidence in §8. Do not summarize beyond what the analyzer output supports.

---

## 2. Research Question

Verbatim from `PLAN.md` §1:

> Using the four test cases below, how much average latency is introduced as
> additional hops are added to logging pipeline architecture, and what role, if
> any, does total volume of events have in those results?

Delivery latency is evaluated **at each hop** after events leave the generation
source, not only end-to-end (`PLAN.md` §1).

### 2.1 Scenarios (test cases)

| ID | Path |
|----|------|
| S1 | Host → Aggregator → S3 (final) |
| S2 | Host → Aggregator-T1 → Aggregator-T2 → S3 (final) |
| S3 | Host → S3 (landing) → Aggregator → S3 (final) |
| S4 | Host → S3 (landing) → Aggregator-T1 → Aggregator-T2 → S3 (final) |

**Diagrams:** evidence-grade network topology, one sequence diagram per
scenario with timestamp capture points labeled, and the per-hop measurement
derivation are in [`../docs/diagrams/`](../docs/diagrams/) (Mermaid + SVG;
`PLAN.md` §5A).

---

## 3. Methodology

This section is complete and derived from `PLAN.md` §3–§5.

### 3.1 Experiment Matrix (`PLAN.md` §3)

| Dimension | Values | Count |
|-----------|--------|-------|
| Scenario | S1, S2, S3, S4 | 4 |
| Forwarding agent (on host) | Vector, Cribl Edge | 2 |
| Aggregator stack | Vector, Cribl Stream | 2 |
| Volume tier (per generating host) | 1,000 / 5,000 / 10,000 EPS | 3 |
| Host OS (analyzed as a dimension) | Linux, Windows | 2 |

Total orchestrated runs: 4 × 2 × 2 × 3 = **48 runs**. Linux and Windows hosts
run **concurrently** within each run and are separated at analysis time via the
`host_os` field (OS is an analysis dimension, not a separate run multiplier).

### 3.2 Run Protocol

Each run: **2 min warm-up (excluded) + 10 min measurement**, followed by a
drain. Scenario switching is config-only — Ansible re-templates agent/aggregator
configs per run; the infrastructure is deployed once for all 48 runs
(`PLAN.md` §7). The orchestrator (`harness/orchestrator/run_matrix.py`) starts
only the generator pair matching the run's `agent` dimension via SSM. Runs may be
executed sequentially (~12 h) or with `--parallel-stacks` (~6 h) (`PLAN.md` §8).

**Run ID convention** (`PLAN.md` §3):
`s{1-4}-{vec|ce}-{vagg|cs}-{1k|5k|10k}-{YYYYMMDDTHHMMSSZ}`
(agent: `vec`=Vector, `ce`=Cribl Edge; aggregator: `vagg`=Vector, `cs`=Cribl Stream).

### 3.3 Event Schema (`PLAN.md` §5.1)

NDJSON, ~512 bytes padded:

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

- `t_gen_ns` — wall-clock nanoseconds at generation (`CLOCK_REALTIME` / .NET
  `DateTime.UtcNow` ticks converted), written by the generator.
- Each processing hop appends a receive timestamp into `hop_ts` via the tool's
  transform layer (Vector VRL `remap`; Cribl Eval function): `hop_ts.agent`,
  `hop_ts.agg1`, `hop_ts.agg2` — **ms precision (tool-native)**.
- S3 hops are timestamped from the object's PutObject time (landing and final),
  retrieved at analysis time. The analyzer prefers an `x-amz-meta-llt-put-ms`
  object-metadata header (ms) where a sink sets it, and otherwise falls back to
  second-precision `LastModified`. **No sink currently writes that metadata, so
  `LastModified` is the operative source today** — see §7 caveats and the
  batch-adjusted deltas below.

### 3.4 Per-Hop Latency Derivation (`PLAN.md` §5.2)

| Hop | Derivation |
|-----|-----------|
| Generation → agent | `hop_ts.agent − t_gen` |
| Agent → Aggregator-T1 (S1/S2) | `hop_ts.agg1 − hop_ts.agent` |
| Agent → landing S3 (S3/S4) | landing object PutObject − `hop_ts.agent` |
| Landing S3 → Aggregator (S3/S4) | `hop_ts.agg1` − landing PutObject |
| T1 → T2 (S2/S4) | `hop_ts.agg2 − hop_ts.agg1` |
| Last aggregator → final S3 | final object PutObject − last `hop_ts` |
| End-to-end | final PutObject − `t_gen` |

### 3.5 Clock Discipline (`PLAN.md` §5.3)

- **Linux:** chrony → AWS Time Sync (`169.254.169.123`).
- **Windows:** w32time → AWS Time Sync, 64 s poll floor.
- Ansible asserts sync before each run (chrony tracking offset **< 1 ms**;
  `w32tm /query /status` stripchart bound **< 5 ms**) and records the values as
  run evidence. Clock error bounds are reported as a stated constraint (§5).

### 3.6 Statistics Reported (`PLAN.md` §5.2)

Per run × OS × hop: **mean, p50, p90, p99, max, stddev, count, loss rate**
(sent seq vs. landed seq). Averages answer the research question; percentiles
defend it under peer review.

### 3.7 Controlled Constants (`PLAN.md` §4.4–§4.5)

- **Wire protocol:** all agent→aggregator and aggregator→aggregator hops use
  HTTP/1.1 POST of NDJSON over the NLBs (Tier-1 `:8080`, Tier-2 `:8081`). TLS
  disabled on internal hops.
- **Batch settings (identical across both tools):** →S3 sinks batch **timeout
  5 s, max 10 MB**; inter-aggregator HTTP sinks batch **timeout 1 s**.

### 3.8 Tuning Profile (`PLAN.md` §4.6)

Both stacks are tuned for the **lowest achievable delivery latency within
the experiment's constraints** (§3.7's wire-protocol/batch constants,
instance types/counts, the two-account PrivateLink topology, and ports/
`hop_ts.*` field names are never touched by tuning). The **parity rule**
governs every tuning action: any knob turned on one stack must have its
documented equivalent turned on the other stack, or be explicitly recorded
as having no equivalent (see the parity ledger below). `docs/TUNING.md` is
the **source of truth** for this profile — this subsection is a mirror,
kept consistent with it at build time; if the two ever disagree,
`docs/TUNING.md` governs. The "Performance gain" column below is the
vendor-documented expectation **[Unverified]** at build time; it is
populated with measured A/B run evidence during the analysis phase (§8
phase 4) and the marker is removed only once real data supports the figure
— no unmeasured gain is stated as fact anywhere in this report.

**Parity ledger — knobs with no equivalent on the other stack:**

| # | Item | Stack lacking the equivalent | Why |
|---|------|-------------------------------|-----|
| 2 | HTTP client keep-alive / connection-reuse **config key** | Vector | No explicit config key in Vector 0.49's `http` sink (connection reuse is automatic/non-configurable); Cribl's Webhook destination has an explicit "Keep alive" toggle (default ON, 120 s). |
| 2 | Request-concurrency **mechanism** (adaptive algorithm vs. static cap) | Cribl | Vector's `request.concurrency: adaptive` is a live-adaptive algorithm; Cribl's Webhook "Request concurrency" is a fixed integer ceiling (raised to its documented max, 32). Both reach "concurrency allowed to scale" in intent, by different mechanisms. |
| 1 | File-open/poll-interval **minimum** value | Cribl Edge vs. Vector | Vector's `glob_minimum_cooldown_ms` floored 100x below its 1000 ms default (to 10 ms); Cribl's File Monitor "Polling interval" floored only 10x below its 10 s default (to 1 s) — no documented minimum was found for either, so exact floor-parity is not asserted. |
| 5 | Thread/process-count **mechanism** | N/A (symmetric outcome, asymmetric mechanism) | Vector has no "process count" — a single process uses all cores implicitly (`--threads` default = core count). Cribl runs N separate OS processes per node (`workerProcesses` set to 4 = vCPU count on `m6i.xlarge`). Both reach full vCPU utilization by different named settings. |

**Tuning table** — full detail (every row, per-stack, with file+line
citations and per-item rationale) lives in
[`../docs/TUNING.md`](../docs/TUNING.md) §4. Summary by item:

| Item | Vector setting(s) | Cribl setting(s) | Status |
|------|--------------------|--------------------|--------|
| 1. Agent file pickup | `glob_minimum_cooldown_ms: 10` (from 1000 default); `read_from: beginning` (latency-neutral decision — file is truncated fresh every run, see `docs/TUNING.md` §4 item 1 for the full eventgen.py/run_matrix.py evidence chain); no `multiline.*` configured | File Monitor poll floored to 1 s (from 10 s default); `mode: manual` read-from-start (same latency-neutral decision); default line-based Event Breaker, no multiline join | Tuned (both) |
| 2. HTTP sinks | `compression: none`, `request.concurrency: adaptive`, retry backoff 1 s/30 s — all also Vector's own defaults, kept explicit | `compress: none` (**active change** — Cribl Webhook defaults compression ON), `concurrency: 32` (from default 5) | Tuned (both); compression is an active change on Cribl only, record-of-default on Vector |
| 3. Buffers | `buffer.type: memory`, `buffer.when_full: block` (also Vector defaults), `max_events: 100000` (from 500 default) | Persistent Queue OFF / Backpressure `Block` (unchanged defaults — already in-memory-only, block-not-drop) | Tuned (Vector sizing is active; Cribl is a kept/verified default) |
| 4. S3-source pickup | `sqs.poll_secs: 20` (from 15 default) | S3 source "Poll timeout (secs)": `20` (from 10 default, max 20) | Tuned (both, symmetric — both raised to the 20 s SQS ceiling) |
| 5. Process/thread scaling | All cores (Vector default, unchanged — record only) | `workerProcesses: 4` (= vCPU count on `m6i.xlarge`, from Cribl's documented `-2` default) | Vector record-only; Cribl active change |
| 6. NLBs | Cross-zone LB ON + deregistration delay 30 s — **DONE IN TERRAFORM** (`terraform/modules/vector-aggregator/main.tf`) | Same, **DONE IN TERRAFORM** (`terraform/modules/cribl-stream/main.tf`), identical values | Record only (terraform); client-keep-alive-vs-NLB-idle-timeout has no config knob on either stack (NLB idle timeout default 350 s, not a practical risk at 10k EPS) |
| 7. Placement | Same 2 AZs, zone-ID selection — **DONE IN TERRAFORM** | Same | Constraint only; single-AZ pinning is further-evaluation work (`PLAN.md` §5.4.5) |
| 8. OS/network | ENA (AWS default), MTU 9001 (unchanged), unattended upgrades disabled (`common` role), gp3 generator volume (terraform) | Same (shared `common` role + shared terraform) | Record only — no per-stack asymmetry |

Several exact Cribl 4.13 JSON config keys could not be confirmed against a
live instance or an exposed schema page at build time (UI-label-only
documentation was available); each has an in-config `TODO(verify)` comment
and is listed in `docs/TUNING.md` §6 rather than silently guessed. This does
not affect the §3.7 controlled constants, ports, or `hop_ts.*` field names,
which were independently re-verified by grep after every tuning edit (see
`docs/TUNING.md` §5 for the grep evidence).

---

## 4. Technologies & Versions

Record exact versions at deploy time (Vector/Cribl pinned in Ansible defaults;
AMIs resolved via SSM parameters at apply and recorded in state/evidence —
`PLAN.md` §7). Documentation references cite the doc root plus the relevant
section where a deep link's stability is uncertain.

| Component | Version | Role | Documentation reference |
|-----------|---------|------|-------------------------|
| Vector (agent) | `[TO RECORD AT DEPLOY]` | Forwarding agent on host; `http` sink; VRL `remap` for `hop_ts.agent` | Vector docs — https://vector.dev/docs/ ; HTTP sink batching: https://vector.dev/docs/reference/configuration/sinks/http/ ("Buffers & batches" / `batch.timeout_secs`) |
| Vector (aggregator) | `[TO RECORD AT DEPLOY]` | Aggregator tiers; `http` source/sink, `aws_s3` source (SQS) + sink; VRL for `hop_ts.agg1/agg2` | Vector docs — https://vector.dev/docs/ ; `aws_s3` source (SQS notifications): https://vector.dev/docs/reference/configuration/sources/aws_s3/ ; `aws_s3` sink batching: https://vector.dev/docs/reference/configuration/sinks/aws_s3/ |
| Cribl Edge | `[TO RECORD AT DEPLOY]` | Forwarding agent on host; HTTP Raw / Webhook out; Eval for `hop_ts.agent` | Cribl docs — https://docs.cribl.io/edge/ |
| Cribl Stream | `[TO RECORD AT DEPLOY]` | Aggregator (leader + worker groups t1/t2); HTTP Raw source, Webhook + S3 destinations, S3 source (SQS); Eval for `hop_ts.agg1/agg2` | Cribl docs — https://docs.cribl.io/stream/ ; S3 destination flush/partition settings: https://docs.cribl.io/stream/destinations-s3/ ; S3 source (SQS/event notifications): https://docs.cribl.io/stream/sources-s3/ |
| Cribl (license) | Cribl Free `[VERIFY TERMS AT DEPLOY]` | ≤ 1 TB/day self-hosted license; all tiers below this | Cribl pricing/licensing — https://cribl.io/pricing/ ; https://docs.cribl.io/stream/licensing/ |
| AWS EC2 | n/a (service); AMIs `[TO RECORD AT DEPLOY]` | Generator + aggregator + leader instances (`m6i.large` / `m6i.xlarge`) | https://docs.aws.amazon.com/ec2/ ; M6i instances: https://aws.amazon.com/ec2/instance-types/m6i/ |
| AWS NLB (ELBv2) | n/a (service) | Internal load balancers fronting aggregator tiers | Network Load Balancer: https://docs.aws.amazon.com/elasticloadbalancing/latest/network/introduction.html |
| AWS PrivateLink | n/a (service) | VPC endpoint services + interface endpoints; NLB-backed cross-account exposure | https://docs.aws.amazon.com/vpc/latest/privatelink/ ; endpoint services (NLB): https://docs.aws.amazon.com/vpc/latest/privatelink/create-endpoint-service.html |
| AWS S3 | n/a (service) | Landing + final + artifacts buckets; PutObject timing | https://docs.aws.amazon.com/s3/ ; event notifications: https://docs.aws.amazon.com/AmazonS3/latest/userguide/NotificationHowTo.html |
| AWS SQS | n/a (service) | Queues fed by S3 event notifications for event-driven aggregator pickup | https://docs.aws.amazon.com/sqs/ ; S3→SQS notifications: https://docs.aws.amazon.com/AmazonS3/latest/userguide/ways-to-add-notification-config-to-bucket.html |
| AWS SSM | n/a (service) | Management plane (Run Command, Session Manager, file transfer) — no SSH/WinRM | https://docs.aws.amazon.com/systems-manager/ |
| AWS Time Sync | n/a (service) | Link-local NTP (`169.254.169.123`) for chrony / w32time | https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/set-time.html (Amazon Time Sync Service, accuracy claims) |

> [Unverified] Deep-link paths within vendor documentation sites change over
> time; if a specific URL 404s at deploy, use the cited doc root and the named
> section (e.g., "HTTP sink → batch" for Vector, "S3 destination → advanced
> settings" for Cribl) to locate the current page, and record the resolved URL
> here.

---

## 5. Caveats & Known Constraints

Written completely now from `PLAN.md` §5.4 items 1–4, plus the additional
constraints requested for peer review. These bound the interpretation of every
result in §7.

1. **Batch-flush inclusion at S3 hops** (`PLAN.md` §5.4 item 1). S3-hop latency
   includes the fixed **5 s batch flush**. Hop deltas at any →S3 hop are
   therefore reported **both raw and batch-adjusted**; the raw value reflects the
   operational latency an event actually experiences, while the batch-adjusted
   value isolates the marginal transport/processing cost of the hop.
2. **Millisecond timestamp precision** (`PLAN.md` §5.4 item 2). `hop_ts.*` are
   ms-precision (tool-native). Sub-millisecond effects are unresolvable at those
   hops; deltas smaller than the precision floor are not distinguishable from
   zero.
3. **S3 object time precision** (`PLAN.md` §5.4 item 3). `LastModified` is
   second-precision. The analyzer prefers a ms-precision `x-amz-meta-llt-put-ms`
   object-metadata header where a sink writes it, and otherwise uses
   `LastModified`. **No sink currently writes that header, so every S3-hop delta
   carries second-level quantization today**; this is why `→S3` hops are
   reported both raw and batch-adjusted (the 5 s flush dominates regardless). If
   a sink is later configured to stamp `x-amz-meta-llt-put-ms`, the analyzer
   upgrades to ms precision automatically with no code change.
4. **Coarser Windows time sync** (`PLAN.md` §5.4 item 4). Windows time sync
   (w32time, 64 s poll floor) is coarser than chrony. Windows deltas carry
   **wider error bars** than Linux and should not be compared to Linux deltas at
   sub-5 ms resolution.
5. **Cribl Free license limits.** The Cribl Stream stack runs under the **Cribl
   Free license** (≤ 1 TB/day; `PLAN.md` §4.3). All tiers are below this ceiling,
   but any feature or throughput behavior gated by license tier is out of scope,
   and license terms must be re-verified at deploy time.
6. **Single-region scope.** Both accounts are in a single region (`us-east-2`
   default; `PLAN.md` §4.1). Cross-region latency is not measured; results do not
   generalize to inter-region pipelines.
7. **No TLS on internal hops.** TLS is disabled on all internal HTTP hops
   (`PLAN.md` §4.4); traffic stays within AWS private networking. TLS handshake
   and encryption overhead is therefore **not** represented in the hop deltas.
8. **Batch-flush dominance at S3 hops.** Because the 5 s flush constant governs
   every →S3 sink (§3.7), raw →S3 hop deltas are expected to be dominated by the
   flush window rather than by network or processing time. Cross-scenario
   comparisons involving S3 hops must use the batch-adjusted figures to remain
   meaningful.
9. **Clock-sync error bounds.** Reported hop deltas inherit the clock-sync error
   bounds asserted per run (Linux offset < 1 ms; Windows stripchart < 5 ms;
   `PLAN.md` §5.3). Any delta of comparable magnitude to these bounds is within
   measurement noise and is reported as such rather than as a signal.

---

## 6. Variability Not Assessed / Opportunities for Further Evaluation

Expanded from `PLAN.md` §5.4 item 5. Each item below is deliberately **out of
scope** for this experiment; a short rationale explains why it could affect
results and therefore why a follow-up study might pursue it.

- **Native wire protocols.** All hops use HTTP/1.1 NDJSON for parity (§3.7). Each
  tool's native transport (Vector gRPC, Cribl TCP-JSON) may have materially
  different framing, connection-reuse, and serialization costs, so native-protocol
  latency could diverge from the HTTP-parity results measured here.
- **TLS overhead.** Internal hops run without TLS. Enabling TLS adds handshake and
  per-record encryption cost, which could raise per-hop latency, especially at
  high EPS where handshake amortization and CPU contention matter.
- **Compression.** No payload compression is applied. Compression trades CPU for
  bytes-on-wire; at higher volume it could reduce transfer time but add
  encode/decode latency, changing the volume-effect curve.
- **Aggregator processing pipelines beyond timestamping.** Aggregators only append
  a receive timestamp. Real deployments run parsing, enrichment, routing, and
  filtering, which add per-event CPU cost and could dominate the hop latency this
  study attributes largely to transport and batching.
- **Instance-type sensitivity.** Fixed `m6i.large` / `m6i.xlarge` sizing is used.
  Larger, smaller, or compute/memory-optimized instances would change CPU and
  network headroom, potentially shifting where volume-driven latency growth
  begins.
- **Cross-AZ vs same-AZ placement.** Placement across AZs is not controlled as a
  variable. Cross-AZ hops add propagation and cross-AZ transfer characteristics
  that could inflate inter-tier latency relative to same-AZ placement.
- **Cross-region.** Single-region only. Inter-region hops add tens of
  milliseconds of propagation and different failure/retry behavior, which would
  dominate the small intra-region deltas measured here.
- **Kinesis / Kafka intermediaries.** No streaming-bus intermediary is present.
  Inserting Kinesis or Kafka would add a durable buffering hop with its own
  batching and consumer-lag latency profile, changing both the hop count and the
  shape of the latency distribution.
- **Backpressure / disk-buffer behavior under sustained overload.** Volume tiers
  top out at 10k EPS/host, below sustained-overload thresholds. Under overload,
  disk buffering and backpressure engage, producing latency behavior (and loss)
  qualitatively different from the steady-state regime measured here.
- **S3 request-rate partitioning.** Key-prefix request-rate scaling in S3 is not
  stressed. At very high object-creation rates, S3 partitioning and throttling
  could add latency at the →S3 and landing→aggregator hops.
- **Windows Event Log channel ingestion.** File-based generation is used for OS
  parity. Native Windows Event Log channel ingestion has different read semantics
  and buffering, which could change the generation→agent hop on Windows relative
  to the file-based path measured here.

---

## 7. Findings

All cells are `[PENDING]`. Populate exclusively from
`harness/analysis/analyze.py` output. Do not enter any value that is not present
in the analyzer output or its evidence artifacts. Statistics per cell:
mean / p50 / p90 / p99 / max / stddev / count (§3.6). Where a hop is a →S3 hop,
report **raw** and, in the batch-adjusted subsection, the batch-adjusted value
(§5 items 1, 8).

Each scenario has a run for every combination of agent (Vector / Cribl Edge),
aggregator (Vector / Cribl Stream), and volume tier (1k / 5k / 10k), split by OS.
The tables below are keyed to the matrix; replicate a table per
agent×aggregator combination where the analysis distinguishes them, or aggregate
per the analysis plan and note which.

### 7.1 Per-Scenario, Per-Hop Latency

#### 7.1.1 S1 — Host → Aggregator → S3 (final)

| Hop | 1k EPS (mean / p50 / p90 / p99 / max / stddev / count) | 5k EPS | 10k EPS |
|-----|--------------------------------------------------------|--------|---------|
| Generation → agent | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Agent → Aggregator-T1 | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Aggregator → final S3 (raw) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Aggregator → final S3 (batch-adjusted) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| End-to-end | `[PENDING]` | `[PENDING]` | `[PENDING]` |

#### 7.1.2 S2 — Host → Aggregator-T1 → Aggregator-T2 → S3 (final)

| Hop | 1k EPS | 5k EPS | 10k EPS |
|-----|--------|--------|---------|
| Generation → agent | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Agent → Aggregator-T1 | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| T1 → T2 | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Aggregator-T2 → final S3 (raw) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Aggregator-T2 → final S3 (batch-adjusted) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| End-to-end | `[PENDING]` | `[PENDING]` | `[PENDING]` |

#### 7.1.3 S3 — Host → S3 (landing) → Aggregator → S3 (final)

| Hop | 1k EPS | 5k EPS | 10k EPS |
|-----|--------|--------|---------|
| Generation → agent | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Agent → landing S3 (raw) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Agent → landing S3 (batch-adjusted) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Landing S3 → Aggregator | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Aggregator → final S3 (raw) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Aggregator → final S3 (batch-adjusted) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| End-to-end | `[PENDING]` | `[PENDING]` | `[PENDING]` |

#### 7.1.4 S4 — Host → S3 (landing) → Aggregator-T1 → Aggregator-T2 → S3 (final)

| Hop | 1k EPS | 5k EPS | 10k EPS |
|-----|--------|--------|---------|
| Generation → agent | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Agent → landing S3 (raw) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Agent → landing S3 (batch-adjusted) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Landing S3 → Aggregator-T1 | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| T1 → T2 | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Aggregator-T2 → final S3 (raw) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| Aggregator-T2 → final S3 (batch-adjusted) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| End-to-end | `[PENDING]` | `[PENDING]` | `[PENDING]` |

### 7.2 Scenario-vs-Scenario Added-Latency Comparison

Answers the core research question: the **added** latency attributable to each
extra hop. Use batch-adjusted values where a →S3 hop is involved (§5 item 8).

| Comparison | What it isolates | 1k EPS | 5k EPS | 10k EPS |
|------------|------------------|--------|--------|---------|
| S2 − S1 | Added second aggregator tier (T1→T2) | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| S3 − S1 | Added S3 landing hop before aggregation | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| S4 − S2 | Added S3 landing hop before two-tier aggregation | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| S4 − S3 | Added second aggregator tier on the landing path | `[PENDING]` | `[PENDING]` | `[PENDING]` |
| S4 − S1 | Combined effect of both added hops | `[PENDING]` | `[PENDING]` | `[PENDING]` |

Each comparison should be reported on end-to-end and, where meaningful, on the
specific shared hops; state which statistic (mean per the research question,
with percentiles alongside).

### 7.3 Volume-Effect Analysis

Answers "what role does total volume of events have?" For each scenario and hop,
report the trend across 1k → 5k → 10k EPS.

| Scenario / Hop | 1k → 5k Δ | 5k → 10k Δ | Trend (flat / rising / non-monotonic) | Notes |
|----------------|-----------|------------|----------------------------------------|-------|
| `[PENDING]` | `[PENDING]` | `[PENDING]` | `[PENDING]` | `[PENDING]` |

`[PENDING RESULTS]` — Narrative: state whether latency is volume-sensitive at the
tested tiers, at which hops it appears first, and whether any tier approaches a
saturation knee. Bound the claim by §5 item 9 (deltas near clock-sync error are
noise) and the fact that 10k EPS/host is below sustained-overload thresholds
(§6, backpressure item).

### 7.4 OS Split (Linux vs Windows)

Report the same hops split by `host_os`. Windows deltas carry wider error bars
(§5 item 4) — do not over-interpret sub-5 ms Linux/Windows differences.

| Scenario / Hop | Linux (mean / p50 / p99) | Windows (mean / p50 / p99) | Delta | Within Windows error bar? |
|----------------|--------------------------|----------------------------|-------|---------------------------|
| `[PENDING]` | `[PENDING]` | `[PENDING]` | `[PENDING]` | `[PENDING]` |

### 7.5 Loss Rate

Loss rate = sent seq vs. landed seq per run × OS × hop (§3.6). Elevated loss at a
hop invalidates that hop's latency stats for the affected run — flag such runs.

| Run ID | Scenario | Agent | Aggregator | EPS | OS | Loss rate | Notes |
|--------|----------|-------|------------|-----|----|-----------|-------|
| `[PENDING]` | `[PENDING]` | `[PENDING]` | `[PENDING]` | `[PENDING]` | `[PENDING]` | `[PENDING]` | `[PENDING]` |

---

## 8. Evidence Index

Maps `report/evidence/` artifacts to the claims they support. See
[`evidence/README.md`](evidence/README.md) for what lands where and what is
gitignored.

| Claim / section | Supporting artifact(s) in `report/evidence/` | Notes |
|-----------------|----------------------------------------------|-------|
| Run inventory & parameters (§3.1, §7) | Per-run **manifest** files (run_id, scenario, agent, aggregator, EPS, timestamps, resolved AMIs, tool versions) | Retained (summarized); one per run |
| Clock discipline within bounds (§3.5, §5 item 9) | **Clock-assertion** records from `assert-clocks.yml` (chrony tracking offset; w32tm stripchart) | Recorded per run as evidence |
| Per-hop / per-scenario statistics (§7.1–§7.5) | **Analyzer output** from `analyze.py` (per run × OS × hop stats; loss rates) | Summary retained; raw per-event data gitignored |
| Tool & AMI versions (§4) | Terraform state excerpt / manifest fields capturing resolved AMIs and pinned Vector/Cribl versions | Recorded at deploy |
| Batch-adjusted derivations (§5 items 1, 8) | Analyzer output columns for raw vs batch-adjusted S3 hops | Derived per §5.2 + §4.5 |

`[PENDING]` — populate artifact filenames after the run; do not cite artifacts
that do not exist.

---

## 9. Reproduction

Full reproduction procedure is in the repository entry point and the operational
runbook:

- **Setup, prerequisites, cost, quick start:** [`../README.md`](../README.md).
- **Deploy → verify clocks → execute matrix → monitor → collect → analyze →
  teardown, with failure modes:** [`../docs/RUNBOOK.md`](../docs/RUNBOOK.md).
- **Binding specification:** [`../PLAN.md`](../PLAN.md).

Pipeline: `scripts/setup.sh` → `harness/orchestrator/run_matrix.py`
(`--parallel-stacks` optional) → `harness/analysis/analyze.py` →
`scripts/teardown.sh`.

---

## 10. Appendix

### 10.1 Batch-Adjustment Formula

Every →S3 sink flushes on a fixed cadence (batch timeout **T_flush = 5 s**, max
batch size 10 MB; `PLAN.md` §4.5). A given event waits in the sink buffer from
its arrival until the next flush boundary, so the **raw** →S3 hop delta includes
that wait. The **batch-adjusted** delta removes the flush-wait component to
expose the marginal transport/processing cost of the hop:

```
raw_s3_hop      = t_putobject − t_hop_prev                # includes flush wait
flush_wait      = t_putobject − t_last_arrival_in_batch   # <= T_flush (5 s)
batch_adjusted  = raw_s3_hop − flush_wait
```

Where the exact per-event `t_last_arrival_in_batch` is not recoverable from tool
telemetry, `flush_wait` is bounded by `[0, T_flush]` and the batch-adjusted value
is reported as a bound rather than a point estimate. [Unverified] The precise
attribution method (per-object batch reconstruction vs. uniform-arrival
assumption) is an analysis-implementation choice in `analyze.py` and must be
documented alongside the figures it produces. Inter-aggregator HTTP sinks use
**T_flush = 1 s** and the same formula applies at those hops.

### 10.2 Run-ID Convention

`s{1-4}-{vec|ce}-{vagg|cs}-{1k|5k|10k}-{YYYYMMDDTHHMMSSZ}` (`PLAN.md` §3):

| Field | Values | Meaning |
|-------|--------|---------|
| `s{1-4}` | `s1`–`s4` | Scenario |
| `{vec\|ce}` | `vec`, `ce` | Forwarding agent: Vector / Cribl Edge |
| `{vagg\|cs}` | `vagg`, `cs` | Aggregator: Vector / Cribl Stream |
| `{1k\|5k\|10k}` | `1k`, `5k`, `10k` | Volume tier (EPS per generating host) |
| `{YYYYMMDDTHHMMSSZ}` | ISO 8601 basic, UTC | Run start timestamp |

Example: `s2-vec-vagg-5k-20260710T140000Z` — Scenario 2, Vector agent, Vector
aggregator, 5,000 EPS, started 2026-07-10 14:00:00 UTC. Final-bucket keys follow
`final/{run_id}/{host_os}/...` (`PLAN.md` §4.3).
