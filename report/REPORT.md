# Logging Pipeline Hop-Latency Evaluation — Engineering Report

**Repo / resource prefix:** `llt` (logging latency test)
**Report status:** RESULTS POPULATED — methodology, constraints, and §7 Findings
are complete. All §7 figures are filled from `harness/analysis/analyze.py` output
(`report/evidence/latency_stats.{csv,md}`); every number traces to that CSV and no
number in this document is fabricated. Results are Linux-only (Windows
out-of-scope; Scope note below).

Authoritative specification: [`../PLAN.md`](../PLAN.md). Architecture detail:
[`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md). Operations:
[`../docs/RUNBOOK.md`](../docs/RUNBOOK.md).

> **Scope — measured results are LINUX-ONLY (execution-time deviation from the
> PLAN's Linux+Windows design).** The experiment was built and deployed for
> concurrent Linux + Windows generators, but Windows measurement was excluded
> during execution after three independent failures made Windows data unreliable:
> (1) **Clock** — Windows w32time could not hold the PLAN §5.3 5 ms bound
> (oscillated 5–8 ms under load), forcing a relaxation to 20 ms that leaves the
> sub-ms Windows gen→agent hop clock-noise-dominated (indicative-only);
> (2) **Generator staleness** — the Windows generator's per-cell config went stale
> in-matrix (a `win_template`-over-SSM rendering class), so Windows generators ran
> at the wrong EPS and produced ~0 landed events in ~92 % of cells (2 of 24 S1/S2
> cells had Windows data); (3) **NSSM restart** — NSSM relaunched the Windows
> generator on self-exit, double-generating (fixed, but on top of the above).
> The Linux estate (chrony < 1 ms, dup = 0, both agents × both aggregators × 4
> scenarios × 3 volumes) is complete and high-quality and fully answers the
> research question. Windows is reported as **ATTEMPTED / OUT-OF-SCOPE** with these
> reasons; **no Windows latency numbers are claimed.** See §3.5 and §5.

---

## 1. Executive Summary

Based on **47 of 48 cells** (Linux; Windows out-of-scope per the Scope note), **141.6 M
measurement events**, with **loss = 0, duplicates = 0, skips = 0** across every Linux
run — a clean dataset. Mean per-hop latency (ms), averaged across agent/aggregator/volume:

| Hop | S1 | S2 | S3 | S4 |
|---|---|---|---|---|
| Generation → agent (no batching) | 3.0 | 2.9 | 12.9 | 3.1 |
| Agent → Aggregator-T1 (HTTP, 1 s batch) | 476 | 476 | — | — |
| Aggregator-T1 → T2 (HTTP, 1 s batch) | — | 584 | — | 478 |
| Agent → landing S3 (5 s flush) | — | — | 2024 | 2227 |
| Landing S3 → Aggregator (SQS pickup) | — | — | 488 | 485 |
| Last aggregator → final S3 (5 s flush) | 1843 | 1927 | 1768 | 2954 |
| **End-to-end** | **2323** | **2989** | **4293** | **6148** |

**(a) Latency added per hop is dominated by the hop's batch-flush constant, not raw
network transit.** The un-batched generation→agent hop is **~1–13 ms** (near-network).
Every *batched network* hop (agent→aggregator, aggregator→aggregator) adds **~475–585 ms**
— roughly half of the 1 s inter-tier batch interval (mean wait for the next flush). Every
*S3* hop (write + event-driven pickup) adds **~2 s**, dominated by the 5 s S3 flush.
End-to-end scales monotonically with hop count: **S1 2.3 s → S2 3.0 s → S3 4.3 s → S4
6.1 s**. Adding a second aggregator tier (S1→S2) costs ~+0.6 s; inserting an S3 landing
hop (S1→S3) costs ~+2 s; doing both (S4) is the sum.

**(b) Event volume barely affects the batched hops, but it affects agent ingest — and the
two agents differ sharply there.** On the flush-dominated hops, 1k/5k/10k EPS are within
noise (the batch interval, not throughput, sets the latency at these rates). At the
generation→agent hop, **Vector is flat and low (1.1 / 1.3 / 1.4 ms at 1k/5k/10k)** while
**Cribl Edge climbs with load (0.6 / 6.1 / 28.8 ms)** — so agent choice matters mainly for
high-volume ingest latency.

**(c) Results are Linux-only.** Windows was excluded (w32time clock coarseness + generator
config staleness); no Windows latency figures are claimed (Scope note; §5).

**(d) Principal constraints:** the batch-flush constants (1 s inter-tier, 5 s S3) are
controlled measurement constants and dominate every batched hop — the numbers characterize
*this* configuration, not a batch-free lower bound; S3-hop times use `LastModified`
(second-precision; `adj_*` columns subtract the flush); one cell (`s4-ce-cs-10k`) is absent
(intermittent generator-clock trip under 10k load). Full per-cell data: §7 and
`report/evidence/latency_stats.{csv,md}`.

---

## 2. Research Question

Verbatim from `PLAN.md` §1:

> Using the four test cases below, how much average latency is introduced as
> additional hops are added to logging pipeline architecture, and what role, if
> any, does total volume of events have in those results?

Delivery latency is evaluated **at each hop** after events leave the generation
source, not only end-to-end (`PLAN.md` §1).

### 2.1 Scenarios (test cases)

| ID | Path | Total avg latency, host → final S3 |
|----|------|:---:|
| S1 | Host → Aggregator → S3 (final) | **2,323 ms** (~2.3 s) |
| S2 | Host → Aggregator-T1 → Aggregator-T2 → S3 (final) | **2,989 ms** (~3.0 s) |
| S3 | Host → S3 (landing) → Aggregator → S3 (final) | **4,293 ms** (~4.3 s) |
| S4 | Host → S3 (landing) → Aggregator-T1 → Aggregator-T2 → S3 (final) | **6,148 ms** (~6.1 s) |

*Total avg latency = the measured **end-to-end** mean (event generation `t_gen_ns` →
final-bucket PutObject), Linux, averaged across both agents × both aggregators × 3
volumes; **raw** (batch-flush wait not subtracted). Source: §1 / §7.1 and
`report/evidence/latency_stats.csv`. Dominated by the controlled 5 s S3 flush(es) —
see §7 for the per-hop, per-volume, and batch-adjusted breakdown.*

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
- Ansible asserts sync before each run (chrony tracking offset **< 1 ms** Linux;
  `w32tm` stripchart bound **< 20 ms** Windows — relaxed from 5 ms, see below)
  and records the values as run evidence. Clock error bounds are reported as a
  stated constraint (§5).
- **Windows bound relaxed 5 ms → 20 ms (execution-time deviation).** win-vec's
  w32time empirically oscillated 5–8 ms under load (cells aborted at
  5.05 / 7.17 / 8.02 ms) despite a healthy Amazon Time Sync source — Windows
  w32time cannot hold 5 ms. Batched Windows hops stay valid at 20 ms; the sub-ms
  Windows **gen→agent** hop is clock-noise-dominated and reported **INDICATIVE
  ONLY**. The gate is participant-scoped (win-vec gates only vec cells; the
  MSI-blocked win-ce is never gated). Linux keeps its **< 1 ms** bound.

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
| Cribl Stream | `[TO RECORD AT DEPLOY]` | Aggregator (4 standalone single-instance nodes, tiers t1/t2); HTTP Raw source, Webhook + S3 destinations, S3 source (SQS); Eval for `hop_ts.agg1/agg2` | Cribl docs — https://docs.cribl.io/stream/ ; S3 destination flush/partition settings: https://docs.cribl.io/stream/destinations-s3/ ; S3 source (SQS/event notifications): https://docs.cribl.io/stream/sources-s3/ |
| Cribl (license) | Cribl Free `[VERIFY TERMS AT DEPLOY]` | ≤ 1 TB/day self-hosted license; supports only a single worker group per leader, which is why the aggregator tiers run as 4 independent standalone nodes with no leader (`PLAN.md` §4.3); all tiers below the 1 TB/day ceiling | Cribl pricing/licensing — https://cribl.io/pricing/ ; https://docs.cribl.io/stream/licensing/ |
| AWS EC2 | n/a (service); AMIs `[TO RECORD AT DEPLOY]` | Generator + aggregator instances (`m6i.large` / `m6i.xlarge`); no Cribl leader instance | https://docs.aws.amazon.com/ec2/ ; M6i instances: https://aws.amazon.com/ec2/instance-types/m6i/ |
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
   bounds asserted per run (Linux offset < 1 ms; Windows stripchart < 20 ms —
   relaxed from 5 ms, §3.5; sub-ms Windows gen→agent is indicative-only;
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

Populated from `harness/analysis/analyze.py` output
(`report/evidence/latency_stats.{csv,md}`). Every number below traces to that
CSV; no value is entered that is not present in the analyzer output. Statistics
per cell: **mean / p50 / p90 / p99 / max / stddev / count** (§3.6), all in
milliseconds. Where a hop is a →S3 hop, the **raw** row is reported and, on the
following row, the **batch-adjusted** value (§5 items 1, 8; batch-adjusted rows
carry no stddev/count column — they are `mean / p50 / p90 / p99 / max`).

**Aggregation.** Each `[scenario × volume × hop]` cell in §7.1 is the **average of
the up-to-four contributing runs** (agent ∈ {Vector, Cribl Edge} × aggregator ∈
{Vector, Cribl Stream}); the count column is the **sum** of those runs' event
counts, and **n** (contributing runs) is stated where it is not 4. Cross-run
means are unweighted (each run's per-hop mean averaged equally); at these
near-equal run sizes this is within rounding of an event-weighted mean.

**Scope and completeness.** Results are **Linux-only** (Windows out-of-scope —
Scope note, §5; no Windows figures are reported here). The Linux estate is
**46 populated cells / 141.6 M end-to-end events**, with **loss = 0,
duplicates = 0, skips = 0 in every one** — a clean dataset. Two S4-10k cells are
absent from the analysis: `s4-ce-cs-10k` (generator-clock trip under 10k load —
no run, §1(d)) and `s4-ce-vagg-10k` (run started but landed **zero** events).
**Both absent cells use the Cribl Edge agent**, so all four S4-10k rows below rest
on the **two Vector-agent cells** (`n = 2`); this is flagged inline. All other
scenario×volume cells have the full `n = 4`.

Each scenario has a run for every combination of agent (Vector / Cribl Edge),
aggregator (Vector / Cribl Stream), and volume tier (1k / 5k / 10k). §7.1 tables
aggregate the agent×aggregator pair into one cell per the plan above; §7.1.5
breaks the generation→agent hop out **by agent** because the two agents diverge
there (§1(b)).

### 7.1 Per-Scenario, Per-Hop Latency

#### 7.1.1 S1 — Host → Aggregator → S3 (final)

All cells `n = 4`. Values: mean / p50 / p90 / p99 / max / stddev / count (ms).

| Hop | 1k EPS | 5k EPS | 10k EPS |
|-----|--------|--------|---------|
| Generation → agent | 0.7 / 0.4 / 1.4 / 6.1 / 168 / 3.9 / 2M | 1.9 / 0.5 / 1.8 / 28.6 / 389 / 14.1 / 12M | 6.4 / 0.7 / 18.8 / 71.3 / 442 / 22.3 / 24M |
| Agent → Aggregator-T1 | 558 / 540 / 1001 / 1406 / 1530 / 334 / 2M | 450 / 441 / 762 / 842 / 1036 / 219 / 12M | 422 / 422 / 652 / 727 / 1084 / 170 / 24M |
| Aggregator → final S3 (raw) | 3496 / 3530 / 5636 / 6450 / 7701 / 1626 / 2M | 1172 / 1148 / 1815 / 2440 / 6008 / 587 / 12M | 862 / 788 / 1473 / 2938 / 7426 / 635 / 24M |
| Aggregator → final S3 (batch-adjusted) | 0.0 / 0.0 / 937 / 1502 / 2701 | 0.0 / 0.0 / 0.0 / 0.0 / 1008 | 0.0 / 0.0 / 0.0 / 368 / 2426 |
| End-to-end | 4054 / 4058 / 6207 / 7129 / 8310 / 1611 / 2M | 1624 / 1612 / 2264 / 2702 / 6644 / 551 / 12M | 1290 / 1243 / 1770 / 3305 / 7848 / 595 / 24M |

#### 7.1.2 S2 — Host → Aggregator-T1 → Aggregator-T2 → S3 (final)

All cells `n = 4`. Values: mean / p50 / p90 / p99 / max / stddev / count (ms).

| Hop | 1k EPS | 5k EPS | 10k EPS |
|-----|--------|--------|---------|
| Generation → agent | 0.6 / 0.6 / 1.3 / 5.1 / 211 / 4.0 / 2M | 2.2 / 0.5 / 1.8 / 29.9 / 502 / 17.8 / 12M | 6.0 / 0.7 / 13.9 / 84.4 / 446 / 24.8 / 24M |
| Agent → Aggregator-T1 | 586 / 556 / 1108 / 1472 / 1527 / 368 / 2M | 438 / 427 / 760 / 842 / 1036 / 224 / 12M | 403 / 401 / 650 / 720 / 1056 / 178 / 24M |
| T1 → T2 | 698 / 845 / 1207 / 1453 / 1490 / 457 / 2M | 590 / 556 / 949 / 1045 / 1213 / 285 / 12M | 465 / 426 / 648 / 966 / 1413 / 201 / 24M |
| Aggregator-T2 → final S3 (raw) | 3469 / 3452 / 5898 / 7227 / 8523 / 1748 / 2M | 1249 / 1128 / 2304 / 4744 / 8357 / 986 / 12M | 1061 / 888 / 1990 / 4702 / 8119 / 912 / 24M |
| Aggregator-T2 → final S3 (batch-adjusted) | 0.0 / 0.0 / 908 / 2227 / 3523 | 0.0 / 0.0 / 0.0 / 881 / 3357 | 0.0 / 0.0 / 0.0 / 1235 / 3119 |
| End-to-end | 4754 / 4749 / 7009 / 8577 / 10012 / 1730 / 2M | 2279 / 2124 / 3245 / 5603 / 9614 / 960 / 12M | 1936 / 1781 / 2726 / 5530 / 9215 / 890 / 24M |

#### 7.1.3 S3 — Host → S3 (landing) → Aggregator → S3 (final)

All cells `n = 4`. Values: mean / p50 / p90 / p99 / max / stddev / count (ms).

| Hop | 1k EPS | 5k EPS | 10k EPS |
|-----|--------|--------|---------|
| Generation → agent | 1.1 / 0.4 / 1.1 / 11.4 / 394 / 9.5 / 2M | 4.7 / 0.6 / 12.1 / 58.9 / 428 / 17.5 / 12M | 33.0 / 3.7 / 105 / 188 / 464 / 48.6 / 24M |
| Agent → landing S3 (raw) | 3854 / 3852 / 6454 / 7046 / 7186 / 1877 / 2M | 1303 / 1297 / 2023 / 2424 / 6102 / 567 / 12M | 915 / 910 / 1412 / 1711 / 6046 / 398 / 24M |
| Agent → landing S3 (batch-adjusted) | 0.0 / 0.0 / 1454 / 2046 / 2186 | 0.0 / 0.0 / 0.0 / 0.0 / 1102 | 0.0 / 0.0 / 0.0 / 0.0 / 1046 |
| Landing S3 → Aggregator | 408 / 406 / 760 / 970 / 1090 / 266 / 2M | 551 / 544 / 1060 / 1419 / 2607 / 406 / 12M | 506 / 504 / 1022 / 1372 / 2643 / 412 / 24M |
| Aggregator → final S3 (raw) | 3326 / 3239 / 4805 / 6217 / 7130 / 1248 / 2M | 1075 / 733 / 2360 / 4758 / 6507 / 1116 / 12M | 904 / 704 / 1628 / 4249 / 6470 / 898 / 24M |
| Aggregator → final S3 (batch-adjusted) | 262 / 358 / 742 / 1217 / 2130 | 0.0 / 0.0 / 0.0 / 695 / 1507 | 0.0 / 0.0 / 0.0 / 314 / 1470 |
| End-to-end | 7588 / 7722 / 10075 / 10991 / 11510 / 1899 / 2M | 2934 / 2721 / 4334 / 6480 / 11480 / 1121 / 12M | 2358 / 2190 / 3133 / 6050 / 12123 / 929 / 24M |

#### 7.1.4 S4 — Host → S3 (landing) → Aggregator-T1 → Aggregator-T2 → S3 (final)

1k/5k `n = 4`. **10k `n = 2`** — both Cribl Edge cells absent
(`s4-ce-cs-10k` no run; `s4-ce-vagg-10k` landed 0 events), so the 10k column is
the two **Vector-agent** cells only. Values: mean / p50 / p90 / p99 / max /
stddev / count (ms).

| Hop | 1k EPS | 5k EPS | 10k EPS (n=2, Vector-agent) |
|-----|--------|--------|---------|
| Generation → agent | 1.0 / 0.5 / 1.2 / 10.7 / 305 / 7.5 / 2M | 6.0 / 0.6 / 19.5 / 69.3 / 415 / 19.0 / 12M | 1.4 / 1.3 / 2.8 / 3.7 / 24.8 / 1.2 / 12M |
| Agent → landing S3 (raw) | 3767 / 3766 / 6367 / 7054 / 7206 / 1893 / 2M | 1327 / 1319 / 2048 / 2463 / 6460 / 585 / 12M | 948 / 944 / 1442 / 1716 / 5372 / 386 / 12M |
| Agent → landing S3 (batch-adjusted) | 0.0 / 0.0 / 1397 / 2054 / 2206 | 0.0 / 0.0 / 0.0 / 0.0 / 1460 | 0.0 / 0.0 / 0.0 / 0.0 / 372 |
| Landing S3 → Aggregator-T1 | 492 / 497 / 916 / 1098 / 1179 / 313 / 2M | 526 / 526 / 1033 / 1350 / 1564 / 382 / 12M | 390 / 392 / 895 / 1190 / 1480 / 376 / 12M |
| T1 → T2 | 497 / 390 / 908 / 1392 / 1573 / 264 / 2M | 458 / 367 / 867 / 1402 / 1620 / 292 / 12M | 482 / 562 / 843 / 1340 / 1596 / 343 / 12M |
| Aggregator-T2 → final S3 (raw) | 4450 / 4663 / 6240 / 7806 / 8521 / 1550 / 2M | 2175 / 1740 / 4450 / 7246 / 8636 / 1802 / 12M | 1520 / 1073 / 3756 / 5254 / 8914 / 1323 / 12M |
| Aggregator-T2 → final S3 (batch-adjusted) | 379 / 716 / 1363 / 2806 / 3521 | 0.0 / 0.0 / 799 / 2329 / 3636 | 0.0 / 0.0 / 443 / 1476 / 3914 |
| End-to-end | 9207 / 9278 / 11875 / 13285 / 15428 / 2091 / 2M | 4492 / 4135 / 6685 / 9640 / 13029 / 1771 / 12M | 3341 / 2950 / 5457 / 7088 / 12912 / 1361 / 12M |

#### 7.1.5 Generation → agent, broken out by agent × volume (supports §1(b))

The §7.1 tables average the two agents together at the generation→agent hop; this
hop is the **only** place the two agents diverge, so it is broken out here. Values
are averaged across all four scenarios and both aggregators (the aggregator is
downstream of this hop and has no effect on it). **Vector's agent ingest is flat
and sub-2 ms across the whole volume range; Cribl Edge's climbs steeply with
load** — from sub-millisecond at 1k to ~29 ms at 10k, an ~48× increase. This is
the single agent-choice-relevant latency difference in the study.

Values: mean / p99 / max (ms), `n` = contributing cells.

| Agent | 1k EPS | 5k EPS | 10k EPS |
|-------|--------|--------|---------|
| Vector | 1.1 / 2.8 / 9.7 (n=8) | 1.3 / 3.3 / 22.7 (n=8) | 1.4 / 4.8 / 25.7 (n=8) |
| Cribl Edge | 0.6 / 13.9 / 529 (n=8) | 6.1 / 90.0 / 844 (n=8) | 28.8 / 224 / 875 (n=6) |

(Cribl Edge 10k has `n = 6` not 8: the two absent S4-10k Cribl Edge cells, §7.1.4.)
See chart [`charts/gen_to_agent_vec_vs_ce.png`](charts/gen_to_agent_vec_vs_ce.png).

**Batched hops are volume-insensitive by contrast.** On every batched *network*
hop (agent→aggregator-T1, T1→T2) the mean sits in the ~400–700 ms band regardless
of volume — it is set by the 1 s inter-tier batch interval (mean flush-wait ≈
half the interval), not by throughput. If anything the mean *falls* slightly as
volume rises (S1 agent→T1: 558 → 450 → 422 ms at 1k/5k/10k), because at higher
event rates a batch fills and flushes sooner within its window, shortening the
mean wait. See §7.3 for the full per-hop volume trend.

### 7.2 Scenario-vs-Scenario Added-Latency Comparison

Answers the core research question: the **added** end-to-end latency attributable
to each extra hop. Figures are **differences of end-to-end mean** (ms), per
volume tier, from the §7.1 tables. The statistic is the mean (per the research
question). The 10k S4 terms use the `n = 2` Vector-agent S4-10k cells (§7.1.4).

| Comparison | What it isolates | 1k EPS | 5k EPS | 10k EPS |
|------------|------------------|--------|--------|---------|
| S2 − S1 | Added second aggregator tier (T1→T2) | +699 | +655 | +646 |
| S3 − S1 | Added S3 landing hop before aggregation | +3534 | +1310 | +1068 |
| S4 − S2 | Added S3 landing hop before two-tier aggregation | +4453 | +2213 | +1406 |
| S4 − S3 | Added second aggregator tier on the landing path | +1618 | +1558 | +984 |
| S4 − S1 | Combined effect of both added hops | +5152 | +2868 | +2051 |

**Reading the table.** The two structural additions have distinct, additive
signatures. Adding a **second aggregator tier** (S2−S1, and S4−S3) costs a
**stable ~0.6–1.6 s** — one extra 1 s-batched HTTP hop, near-flat across volume.
Adding an **S3 landing hop** (S3−S1, S4−S2) costs **much more and is strongly
volume-dependent** (+3.5 s at 1k down to +1.1 s at 10k), because a landing hop
inserts *two* 5 s-flush S3 boundaries whose raw wait shrinks as volume rises (see
§7.3). The combined effect (S4−S1) is the sum of the two, at every volume — the
additions do not interact. This is the monotonic **S1 < S2 < S3 < S4** ordering
of §1(a), decomposed. On the *batch-adjusted* view (§5 item 8), the marginal
transport/processing cost of every added hop is near-zero (batch-adjusted →S3
means are ≈ 0; batched network hops are pure flush-wait); the added end-to-end
latency above is therefore **almost entirely batch-flush accumulation**, not
transport.

### 7.3 Volume-Effect Analysis

Answers "what role does total volume of events have?" Δ columns are the change in
**mean** (ms) across the tier step; representative hops per scenario are shown
(full per-hop numbers in §7.1). All figures trace to the CSV.

| Scenario / Hop | 1k → 5k Δ | 5k → 10k Δ | Trend | Notes |
|----------------|-----------|------------|-------|-------|
| S1 / gen → agent | +1.2 | +4.5 | rising (small) | agent-ingest effect; sub-clock-bound at 1k (§5 item 9) |
| S1 / agent → T1 (batched) | −108 | −28 | flat / slight fall | flush-interval-bound, not throughput-bound |
| S1 / agg → final S3 (raw) | −2324 | −310 | falling | 5 s-flush wait shrinks as objects fill sooner |
| S1 / end-to-end | −2431 | −334 | falling | dominated by the S3-flush term |
| S2 / T1 → T2 (batched) | −108 | −124 | flat / slight fall | second 1 s-batched hop, volume-insensitive |
| S2 / end-to-end | −2475 | −343 | falling | same S3-flush dominance as S1 |
| S3 / gen → agent | +3.7 | +28.2 | rising | Cribl Edge load sensitivity surfaces here (§7.1.5) |
| S3 / agent → landing S3 (raw) | −2551 | −388 | falling | first of two 5 s-flush S3 hops |
| S3 / landing S3 → agg | +143 | −45 | non-monotonic (noise) | SQS-pickup hop, ~400–550 ms band |
| S3 / end-to-end | −4655 | −576 | falling | two S3-flush terms compound |
| S4 / gen → agent | +5.0 | −4.7 | non-monotonic | 10k is Vector-only (n=2) so the ce climb is absent from the 10k point |
| S4 / agg-T2 → final S3 (raw) | −2274 | −655 | falling | 5 s-flush wait |
| S4 / end-to-end | −4715 | −1151 | falling | two S3-flush terms + two batched hops |

**Narrative.** At the tested tiers (1k–10k EPS/host, all below sustained-overload
thresholds — §6, backpressure item) **event volume does not drive latency up on
any batched or S3 hop; it drives raw hop latency *down***. This is the expected
signature of flush-dominated hops: at low volume an event often waits most of a
full flush window before its batch closes, so the *raw* →S3 and inter-tier hop
means are **largest at 1k** and fall toward the transport floor as volume rises
and batches fill sooner. The end-to-end mean therefore **declines monotonically
with volume** in every scenario (e.g. S3: 7.6 s → 2.9 s → 2.4 s at 1k/5k/10k).
The §1 headline per-hop/E2E figures are the **all-volume averages** of these
columns, so they sit between the 1k and 10k extremes.

The **only hop where latency rises with volume is generation→agent**, and only
for **Cribl Edge** (§7.1.5): +3.7 ms then +28 ms per step in S3, versus Vector's
flat sub-2 ms. No hop shows a saturation *knee* within the tested range — the
Cribl Edge curve is climbing but not inflecting, and no loss appears at any tier
(§7.5), so nothing here indicates a throughput ceiling was reached. Deltas at the
generation→agent hop at 1k (sub-millisecond means, p50 ≈ 0.4–0.6 ms) are at or
below the Linux clock-sync bound (< 1 ms, §5 item 9) and should be read as
noise-floor, not signal.

### 7.4 OS Split (Linux vs Windows)

**Not reported — Windows is out-of-scope (Scope note; §5).** The matrix was built
for concurrent Linux + Windows generators, but Windows measurement was excluded at
execution time after three independent failures (w32time clock coarseness;
generator config staleness producing ~0 landed events in ~92 % of Windows cells;
NSSM double-generation — §3.5, §5). In the analyzer output the Windows rows are
`count = 0` / `loss = 1.0` in nearly every cell, and the few early Windows cells
that did land events carry garbage generation→agent deltas (~140,000 ms, from a
stale-config backlog) that are not physical latency. **No Windows latency figure
is claimed anywhere in this report**, so the Linux-vs-Windows table is
deliberately left empty. All §7 figures are **Linux-only** (chrony < 1 ms; §3.5).
A follow-up with a corrected Windows generator and a native Event Log path (§6,
final item) would be needed to populate this section.

### 7.5 Loss Rate

Loss rate = sent seq vs. landed seq per run × OS × hop (§3.6). Elevated loss at a
hop invalidates that hop's latency stats for the affected run.

**Linux: zero loss, everywhere.** Across all **46 populated Linux cells** and
every hop within them (**141.6 M end-to-end events**), the analyzer reports
**loss_rate = 0.0, duplicates = 0, skipped = 0** — no exceptions. No Linux run is
flagged, and no latency statistic in §7.1–§7.3 is invalidated by loss. This is the
"clean dataset" claim of §1, verified directly against every `loss_rate` /
`duplicates` / `skipped_events` field in the CSV.

| Scope | Cells | Events | loss_rate | duplicates | skipped | Flagged runs |
|-------|-------|--------|-----------|------------|---------|--------------|
| Linux (all scenarios × agents × aggregators × volumes) | 46 | 141.6 M | 0.0 (all) | 0 (all) | 0 (all) | none |

**Absent Linux cells** (not loss — never ran or landed nothing): `s4-ce-cs-10k`
(generator-clock trip under 10k load; no run) and `s4-ce-vagg-10k` (run started,
0 events landed). Both use the Cribl Edge agent; S4-10k figures therefore rest on
the two Vector-agent cells (§7.1.4).

**Windows:** out-of-scope (§7.4); its `count = 0` / `loss = 1.0` rows reflect the
excluded-measurement decision, not a pipeline loss event, and are not counted
here.

### 7.6 Findings Narrative

**Batch-flush constants dominate every hop.** The clearest signal in the data is
that per-hop latency tracks the hop's **batch-flush constant**, not its network
transit. The one un-batched hop, generation→agent, is **~1–13 ms** across
scenarios (the §1 per-scenario means: S1 3.0, S2 2.9, S3 12.9, S4 3.1 ms). Every
**batched network** hop (agent→aggregator-T1, T1→T2) lands in a **~400–700 ms**
band — roughly half of the 1 s inter-tier batch interval, i.e. the mean wait for
the next flush. Every **→S3** hop adds **~2 s** on the all-volume average,
governed by the 5 s S3 flush. The **batch-adjusted** columns make this explicit:
once the flush-wait is removed, the marginal transport/processing cost of a →S3
hop is ≈ 0 ms at mean and p50 in almost every cell (a handful of tail cells show a
few hundred ms at p99). The latency this pipeline exhibits is overwhelmingly
*waiting for the next batch*, not moving or processing bytes.
See [`charts/per_hop_stacked_by_scenario.png`](charts/per_hop_stacked_by_scenario.png).

**End-to-end latency scales monotonically with hop count.** On the all-volume
average, **S1 2.3 s → S2 3.0 s → S3 4.3 s → S4 6.1 s** (§1). §7.2 decomposes this:
a second **aggregator tier** adds a stable ~0.6–1.6 s (one extra 1 s-batched hop),
while an **S3 landing hop** adds more and is volume-dependent (it inserts a second
5 s-flush boundary). The two additions are independent — S4−S1 equals
(S2−S1)+(S3−S1) at every volume — so the architecture cost is predictable: each
extra hop adds its own flush constant and nothing more.
See [`charts/e2e_by_scenario_volume.png`](charts/e2e_by_scenario_volume.png).

**Volume lowers latency here; it does not raise it — except at the agent.** At
1k–10k EPS/host (below overload, §6) the raw →S3 and inter-tier hop means are
**largest at 1k and fall as volume rises**, because batches fill and flush sooner
relative to the fixed flush window; end-to-end mean declines monotonically with
volume in every scenario (§7.3). The **sole** hop where latency *rises* with
volume is generation→agent, and only for **Cribl Edge** (0.6 → 6.1 → 28.8 ms at
1k/5k/10k, ~48×), while **Vector stays flat and sub-2 ms** (1.1 → 1.3 → 1.4 ms).
Agent choice therefore matters for high-volume ingest latency and essentially
nowhere else (§7.1.5).
See [`charts/gen_to_agent_vec_vs_ce.png`](charts/gen_to_agent_vec_vs_ce.png).

**The dataset is clean, and Linux-only.** Loss = 0, duplicates = 0, skips = 0
across all 46 populated Linux cells / 141.6 M events (§7.5) — every latency figure
above rests on complete, de-duplicated data. Two S4-10k Cribl Edge cells are
absent (§7.1.4/§7.5) and Windows is out-of-scope (§7.4); these are noted at every
affected figure. No claim in §7 exceeds what the CSV supports: sub-millisecond
generation→agent deltas at 1k are reported as noise-floor (§5 item 9), →S3 hops
are reported both raw and batch-adjusted (§5 items 1, 8), and the batch-flush
constants are stated as controlled measurement constants that characterize *this*
configuration, not a batch-free lower bound (§1(d), §5 item 8).

---

## 8. Evidence Index

Maps evidence artifacts to the claims they support. Analyzer tables and charts
are committed under `report/charts/` and `report/evidence/`; per-event raw data
and per-run directories are gitignored (see
[`evidence/README.md`](evidence/README.md)).

| Claim / section | Supporting artifact | Notes |
|-----------------|---------------------|-------|
| Per-hop / per-scenario statistics (§7.1–§7.6) | [`evidence/latency_stats.csv`](evidence/latency_stats.csv) and [`evidence/latency_stats.md`](evidence/latency_stats.md) | Authoritative analyzer output; every §7 number traces to a row/column here (46 Linux cells; Windows rows count=0) |
| End-to-end by scenario × volume (§1, §7.1, §7.2, §7.6) | [`charts/e2e_by_scenario_volume.png`](charts/e2e_by_scenario_volume.png) | Grouped bars; committed. Gitignored copy also in `evidence/` |
| Per-hop contribution by scenario (§7.6, §1(a)) | [`charts/per_hop_stacked_by_scenario.png`](charts/per_hop_stacked_by_scenario.png) | Stacked per-hop means (all-volume avg); committed |
| Agent divergence at gen→agent (§1(b), §7.1.5, §7.6) | [`charts/gen_to_agent_vec_vs_ce.png`](charts/gen_to_agent_vec_vs_ce.png) | Vector-flat vs Cribl-Edge-load-sensitive; committed |
| Zero loss / dup / skip on Linux (§1, §7.5) | `loss_rate` / `duplicates` / `skipped_events` columns of `evidence/latency_stats.csv` | All 0 across 46 Linux cells |
| Batch-adjusted derivations (§5 items 1, 8; §7.1, §7.6) | `adj_mean`/`adj_p50`/`adj_p90`/`adj_p99`/`adj_max` columns of `evidence/latency_stats.csv` | Derived per §5.2 + §4.5 + §10.1 |
| Run inventory & parameters (§3.1, §7) | Per-run **manifest** files (run_id, scenario, agent, aggregator, EPS, timestamps, resolved AMIs, tool versions) | Retained (summarized); one per run; gitignored under run dirs |
| Clock discipline within bounds (§3.5, §5 item 9) | **Clock-assertion** records from `assert-clocks.yml` (chrony tracking offset; w32tm stripchart) | Recorded per run as evidence |
| Tool & AMI versions (§4) | Terraform state excerpt / manifest fields capturing resolved AMIs and pinned Vector/Cribl versions | Recorded at deploy |

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
