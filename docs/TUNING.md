# Tuning Reference — Low-Latency Profile (`llt`)

This document is the **source of truth** for the `PLAN.md` §4.6 low-latency
tuning profile. It is mirrored (not duplicated in full detail) in
[`../report/REPORT.md`](../report/REPORT.md) (Methodology → Tuning Profile).
Where this document and `PLAN.md` disagree, `PLAN.md` governs. Where this
document and `report/REPORT.md`'s mirrored table disagree, **this document
governs** (`PLAN.md` §4.6: "maintained in `docs/TUNING.md` (source of
truth)").

---

## 1. Hard constraints (may NOT be changed by tuning)

Per `PLAN.md` §4.6, the following are fixed as part of the experiment's
methodology and are explicitly **out of scope** for every row in this table:

1. **HTTP/1.1 NDJSON transport parity** (`PLAN.md` §4.4) — every agent→
   aggregator and aggregator→aggregator hop stays HTTP/1.1 POST of NDJSON.
   Native protocols (Vector gRPC, Cribl TCP-JSON) are not assessed.
2. **§4.5 batch constants** — S3 sinks: batch timeout **5 s**, max batch size
   **10 MB**. Inter-aggregator HTTP sinks: batch timeout **1 s**. No tuning
   row in this table touches a `timeout_secs` / `max_bytes` / `flushPeriodSec`
   / `maxFileOpenTimeSec` / `maxFileIdleTimeSec` / `maxFileSizeMB` value tied
   to these constants — see §5 below for the grep evidence that they are
   unchanged.
3. **Instance types/counts** — `m6i.large` (generators),
   `m6i.xlarge` (all aggregator tiers, including the standalone Cribl Stream
   nodes — no leader), fixed counts per `PLAN.md` §4.
4. **Two-account PrivateLink topology** — sender/logging account split,
   PrivateLink endpoint services fronting only the Tier-1 NLBs.
5. **Ports** — Tier-1 `:8080`, Tier-2 `:8081` (both stacks).
6. **`hop_ts.agent` / `hop_ts.agg1` / `hop_ts.agg2` field names** —
   load-bearing for `harness/analysis/analyze.py`.

Any tuning knob that would touch one of the above is **not applied** and is
instead recorded here as a constraint (see §4 "Record-only" rows).

## 2. Parity rule

Per `PLAN.md` §4.6: any knob turned on **one** stack must have its documented
equivalent turned on the **other** stack, or be explicitly recorded as having
**no equivalent**. Both stacks are tuned equally for lowest latency within
the constraints in §1. The table in §4 has one row **per setting per stack**
so parity (or its absence) is directly auditable.

## 3. Parity ledger — knobs with no equivalent on the other stack

| # | Item | Stack lacking the equivalent | Why |
|---|------|-------------------------------|-----|
| 2 | HTTP client keep-alive / connection-reuse **config key** | Vector (both `vector-agent` and `vector-aggregator`) | Vector's `http` sink has no explicit keep-alive/connection-reuse setting in 0.49 — its underlying HTTP client reuses connections automatically and this is not exposed as a tunable. Cribl's Webhook destination has an explicit "Keep alive" toggle (default ON, holds up to 120 s). Vector is not deficient — the behavior (connection reuse) is present on both stacks — but there is no Vector config key to point to in this table, so it is recorded as a documented asymmetry rather than a missing row. |
| 2 | Request-concurrency **mechanism** (adaptive algorithm vs. static cap) | Cribl (both `cribl-edge` and `cribl-stream` node t1→t2) | Vector's `request.concurrency: adaptive` is a live-adaptive algorithm that scales up/down with observed latency/throughput. Cribl's Webhook destination "Request concurrency" is a fixed integer ceiling (raised from its default 5 to the documented max 32 in this profile). Both are "concurrency allowed to scale" in the PLAN §4.6.2 sense (raised toward maximum vs. left at a low default), but the underlying mechanism differs — recorded as an asymmetry, not a gap. |
| 1 | File-open/poll-interval **minimum** value | Cribl Edge (File Monitor source) vs. Vector (`file` source) | Vector's `glob_minimum_cooldown_ms` is documented down to a 1000 ms default with no stated floor; floored to 10 ms in this profile. Cribl's File Monitor "Polling interval" is documented with a 10 s default and no stated minimum either; floored to 1 s. The two tools' minimum-floor headroom below their respective defaults could not be confirmed as identical (Vector's is pushed 100x below default, Cribl's only 10x) — recorded as a possible residual asymmetry rather than asserted as exact parity. |
| 5 | Thread/process-count **mechanism** (implicit all-cores vs. explicit worker-process count) | N/A — both stacks reach "one execution unit per vCPU," but by different named settings | Vector has no separate "process count" concept — a single Vector process uses all cores via internal async-task scheduling (`--threads` default = core count). Cribl's architecture instead runs N separate OS processes per node (`workers.count` set to 4 = vCPU count on `m6i.xlarge`, node-local on each standalone node — no leader). Both reach full-core utilization; recorded here because the settings are not directly comparable line items even though the *outcome* (full vCPU utilization) is symmetric. **If Cribl Free caps this value below 4 on a standalone node, that cap is a separate, additional documented asymmetry (`PLAN.md` §4.6.5) — record the actual deployed value rather than assuming the requested 4 was honored.** |

## 4. Tuning table

One row per setting per stack. "Tuned value" cites the **file + line** where
the Ansible role sets it. "Performance gain" is the vendor-documented
expectation **[Unverified]** at build time; it is populated with measured
A/B evidence during the analysis phase (`PLAN.md` §8 phase 4 / §4.6) and the
`[Unverified]` marker is removed only once real run data supports the
figure — see [`../report/REPORT.md`](../report/REPORT.md) §8 Evidence Index
for where that evidence will be indexed.

### Item 1 — Agent file pickup

| Component | Setting | Default | Tuned value | Why | Performance gain |
|---|---|---|---|---|---|
| Vector agent | `sources.gen_file.read_from` | `beginning` | `beginning` — `ansible/roles/vector-agent/templates/vector.yaml.j2:59` | **Decision, not a violation of §4.6.1's "read-from-end" language**: `harness/generator/eventgen.py`'s `RotatingWriter.__init__()` calls `_open_new(mode="w")` (default arg) every time the generator process starts, truncating the output file to empty at the start of every run; the agent is already installed/running before the orchestrator starts the generator per-run (`harness/orchestrator/run_matrix.py` `execute_cell` phase 3 runs after phases 1–2 configure/restart the agent). "Beginning" and "end" of a just-truncated file are the same byte offset (0) — latency-neutral, and beginning is kept because it also captures warm-up events for windowing (`PLAN.md` §3/§5). | N/A — this is a decision record, not a latency-affecting change. |
| Vector agent | `sources.gen_file.glob_minimum_cooldown_ms` | `1000` ms | `10` ms — `ansible/roles/vector-agent/templates/vector.yaml.j2:70` (value from `vector_file_glob_cooldown_ms`, `ansible/roles/vector-agent/defaults/main.yml`) | Delay between file-discovery calls; flooring minimizes the window between the generator's first write (post-truncate) and Vector noticing the (re)created file. | **[Unverified]** Vendor doc only states the default (1000 ms); no vendor-quantified latency-reduction figure found for lowering it. https://vector.dev/docs/reference/configuration/sources/file/ |
| Vector agent | `sources.gen_file.multiline` | not configured (line-based) | not configured (unchanged) — `ansible/roles/vector-agent/templates/vector.yaml.j2:71-75` (comment) | Each NDJSON event is one line (`PLAN.md` §5.1); multiline aggregation would add artificial join-latency with no benefit here. Vector 0.49 requires explicit `multiline.start_pattern`/`condition_pattern`/`mode`/`timeout_ms` to enable it — omission = off. | N/A — absence of a feature, not a tuned value. |
| Cribl Edge | File Monitor "Polling interval" (`servicePeriodSecs` — **TODO(verify)**, see §6) | `10` s | `1` s — `ansible/roles/cribl-edge/templates/inputs.yml.j2:46` (value from `cribl_edge_poll_interval_secs`, `ansible/roles/cribl-edge/defaults/main.yml`) | Parity counterpart to Vector's `glob_minimum_cooldown_ms` floor — minimizes file-discovery latency after the generator truncates/recreates the tailed file each run. | **[Unverified]** Vendor doc only states the default (10 s); no vendor-quantified figure found. https://docs.cribl.io/stream/sources-file-monitor/ |
| Cribl Edge | File input `mode` (read-from-start decision) | n/a (Cribl-specific) | `manual` (`ansible/roles/cribl-edge/templates/inputs.yml.j2:29`) + `idleTimeout: 30` (`:30`) — unchanged from pre-tuning config; decision comment added at `:18-28` | Same decision/rationale as the Vector row above — file is truncated fresh every run by `eventgen.py`, and this Edge agent is already running before the orchestrator starts the generator. Parity with Vector's `read_from: beginning` decision. | N/A — decision record. |
| Cribl Edge | Multiline / Event Breaker | Default `System Default Rule` (line-based unless overridden) | Explicit `"JSON Newline Delimited"` breaker (unchanged from pre-tuning config; line-based, one event per NDJSON line) — `ansible/roles/cribl-edge/templates/inputs.yml.j2:59-60` | No multiline join layered on top of line-based breaking; parity with Vector's "no multiline configured." | N/A — absence of a feature. |

### Item 2 — HTTP sinks (agent→T1, T1→T2)

| Component | Setting | Default | Tuned value | Why | Performance gain |
|---|---|---|---|---|---|
| Vector agent | `sinks.to_agg_t1.compression` | `none` | `none` (kept explicit) — `ansible/roles/vector-agent/templates/vector.yaml.j2:128` | CPU-for-latency trade on the private NLB link; bandwidth is not the constraint, encode/decode adds CPU-bound latency per request. Also Vector's own default. | **[Unverified]** No vendor-quantified figure for compression-off vs. on latency delta found; general CPU-tradeoff rationale only. https://vector.dev/docs/reference/configuration/sinks/http/ |
| Vector agent | `sinks.to_agg_t1.request.concurrency` | `adaptive` | `adaptive` (kept explicit) — `ansible/roles/vector-agent/templates/vector.yaml.j2:140` | Vector's Adaptive Request Concurrency scales concurrency to observed latency/throughput rather than a fixed cap. Also Vector's own default. | **[Unverified]** https://vector.dev/docs/reference/configuration/sinks/http/ |
| Vector agent | `sinks.to_agg_t1.request.retry_initial_backoff_secs` / `retry_max_duration_secs` | `1` / `30` | `1` / `30` (kept explicit) — `ansible/roles/vector-agent/templates/vector.yaml.j2:150-151` | "No retry backoff inflation at steady state" — Vector's own defaults already are the non-inflating behavior requested; recorded explicitly for the table rather than left implicit. | **[Unverified]** https://vector.dev/docs/reference/configuration/sinks/http/ |
| Vector agent | `sinks.to_agg_t1` keep-alive | n/a (no config key) | n/a — not configurable | See parity ledger §3 row 1: automatic, non-configurable in Vector 0.49. | N/A |
| Vector aggregator (T1→T2) | `sinks.to_t2.compression` | `none` | `none` (kept explicit) — `ansible/roles/vector-aggregator/templates/vector-agg.yaml.j2:136` | Same rationale as the agent row above, applied to the inter-tier hop. | **[Unverified]** https://vector.dev/docs/reference/configuration/sinks/http/ |
| Vector aggregator (T1→T2) | `sinks.to_t2.request.concurrency` | `adaptive` | `adaptive` (kept explicit) — `ansible/roles/vector-aggregator/templates/vector-agg.yaml.j2:145` | Same rationale as the agent row above. | **[Unverified]** https://vector.dev/docs/reference/configuration/sinks/http/ |
| Vector aggregator (T1→T2) | `sinks.to_t2.request.retry_initial_backoff_secs` / `retry_max_duration_secs` | `1` / `30` | `1` / `30` (kept explicit) — `ansible/roles/vector-aggregator/templates/vector-agg.yaml.j2:146-147` | Same rationale as the agent row above. | **[Unverified]** https://vector.dev/docs/reference/configuration/sinks/http/ |
| Cribl Edge | Webhook `compress` | ON (gzip) | `none` — **active change** — `ansible/roles/cribl-edge/templates/outputs.yml.j2:32` | Cribl's Webhook destination defaults compression ON, unlike Vector — this is an active change to reach parity with Vector's compression-off default. | **[Unverified]** No vendor-quantified figure for the latency delta; CPU-tradeoff rationale only. https://docs.cribl.io/stream/destinations-webhook/ |
| Cribl Edge | Webhook "Request concurrency" (`concurrency` — **TODO(verify)**, see §6) | `5` (range 1–32) | `32` — `ansible/roles/cribl-edge/templates/outputs.yml.j2:53` (value from `cribl_edge_concurrency`, `ansible/roles/cribl-edge/defaults/main.yml`) | Raised to the documented maximum — parity counterpart to Vector's adaptive concurrency (see parity ledger §3 row 2 for the mechanism-difference caveat). | **[Unverified]** No vendor-quantified throughput/latency figure for 5→32 found. https://docs.cribl.io/stream/destinations-webhook/ |
| Cribl Edge | Webhook "Keep alive" | ON (up to 120 s) | ON (kept at default; not set explicitly — exact JSON key unconfirmed) — `ansible/roles/cribl-edge/templates/outputs.yml.j2:34-41` (comment) | Connection reuse already on by default; recorded for the table. | **[Unverified]** https://docs.cribl.io/stream/destinations-webhook/ |
| Cribl node t1 (standalone, T1→T2 hop) | Webhook `compress` | ON (gzip) | `none` — **active change** — `ansible/roles/cribl-stream/templates/outputs.yml.j2:56` | Same active-change rationale as the Cribl Edge row above, applied to the T1→T2 inter-tier hop. | **[Unverified]** https://docs.cribl.io/stream/destinations-webhook/ |
| Cribl node t1 (standalone, T1→T2 hop) | Webhook "Request concurrency" (`concurrency` — **TODO(verify)**) | `5` (range 1–32) | `32` — `ansible/roles/cribl-stream/templates/outputs.yml.j2:65` (value from `cribl_t2_webhook_concurrency`, `ansible/roles/cribl-stream/defaults/main.yml:77`) | Parity counterpart to Vector's adaptive concurrency on the T1→T2 hop. | **[Unverified]** https://docs.cribl.io/stream/destinations-webhook/ |
| Cribl node t1 (standalone, T1→T2 hop) | Webhook "Keep alive" | ON (up to 120 s) | ON (kept at default; not set explicitly) — `ansible/roles/cribl-stream/templates/outputs.yml.j2:66-70` (comment) | Same rationale as the Cribl Edge row above. | **[Unverified]** https://docs.cribl.io/stream/destinations-webhook/ |

### Item 3 — Buffers

| Component | Setting | Default | Tuned value | Why | Performance gain |
|---|---|---|---|---|---|
| Vector agent | `sinks.to_agg_t1.buffer.type` | `memory` | `memory` (kept explicit) — `ansible/roles/vector-agent/templates/vector.yaml.j2:165` | No disk buffering on the measured path. Also Vector's own default. | **[Unverified]** https://vector.dev/docs/reference/configuration/sinks/http/ |
| Vector agent | `sinks.to_agg_t1.buffer.when_full` | `block` | `block` (kept explicit) — `ansible/roles/vector-agent/templates/vector.yaml.j2:167` | Overflow must block, not drop, so loss rate reflects real delivery failures, not a tuning artifact. Also Vector's own default. | **[Unverified]** https://vector.dev/docs/reference/configuration/sinks/http/ |
| Vector agent | `sinks.to_agg_t1.buffer.max_events` | `500` | `100000` — `ansible/roles/vector-agent/templates/vector.yaml.j2:166` (value from `vector_buffer_max_events`, `ansible/roles/vector-agent/defaults/main.yml`) | 500 events fills in ~50 ms at 10k EPS/host; sized so the heaviest volume tier does not invoke `when_full` behavior in normal operation. | **[Unverified]** No vendor-quantified figure for this specific sizing; sizing rationale is this experiment's own arithmetic (10k EPS × ~512 B events), not a vendor benchmark. https://vector.dev/docs/reference/configuration/sinks/http/ |
| Vector agent | `sinks.to_landing_s3.buffer.*` | same as above | same as above — `ansible/roles/vector-agent/templates/vector.yaml.j2:204-206` | Same rationale, applied to the S3/S4 scenario's landing sink. | **[Unverified]** Same citation. |
| Vector aggregator | `sinks.final_s3.buffer.*` (both tiers) | same as above | same as above — `ansible/roles/vector-aggregator/templates/vector-agg.yaml.j2:119-121` (t1), `:187-189` (t2) | Same rationale, applied to both tiers' terminal S3 sinks. | **[Unverified]** Same citation. |
| Vector aggregator | `sinks.to_t2.buffer.*` | same as above | same as above — `ansible/roles/vector-aggregator/templates/vector-agg.yaml.j2:157-159` | Same rationale, applied to the T1→T2 hop. | **[Unverified]** Same citation. |
| Cribl Edge | Webhook Persistent Queue / Backpressure behavior | OFF / `Block` | OFF / `Block` (unchanged; not set explicitly — already the wanted default) — `ansible/roles/cribl-edge/templates/outputs.yml.j2:54-62` (comment) | PQ off = in-memory only (no disk buffer on the measured path, parity with Vector's `buffer.type: memory`); default Backpressure behavior Block matches Vector's `when_full: block`. | **[Unverified]** https://docs.cribl.io/stream/persistent-queues-destinations/, https://docs.cribl.io/stream/destinations-backpressure-triggers/ |
| Cribl Edge | S3 destination local file staging | n/a | n/a — no separate buffer/PQ toggle applies to this destination type | Recorded as a "no additional knob" note, not a gap — `maxFileOpenTimeSec`/`maxFileIdleTimeSec` govern object assembly, not an event queue. | N/A |
| Cribl node t1 (standalone, T1→T2 hop) | Webhook Persistent Queue / Backpressure behavior (T1→T2) | OFF / `Block` | OFF / `Block` (unchanged) — `ansible/roles/cribl-stream/templates/outputs.yml.j2:72-76` (comment) | Same rationale as the Cribl Edge row above, applied to the T1→T2 hop. | **[Unverified]** Same citations. |
| Cribl node t1/t2 (standalone) | S3 destination (`final_s3`) local file staging | n/a | n/a — no separate buffer/PQ toggle applies | Same note as the Cribl Edge S3 row above, for both nodes' terminal S3 sinks — `ansible/roles/cribl-stream/templates/outputs.yml.j2:37` (t1), `:95` (t2). | N/A |

### Item 4 — S3-source pickup (S3/S4 scenarios)

| Component | Setting | Default | Tuned value | Why | Performance gain |
|---|---|---|---|---|---|
| Vector aggregator (T1, S3/S4) | `sources.s3_in.sqs.poll_secs` | `15` s | `20` s — `ansible/roles/vector-aggregator/templates/vector-agg.yaml.j2:52` (value from `vector_sqs_poll_secs`, `ansible/roles/vector-aggregator/defaults/main.yml`) | `poll_secs` IS Vector's SQS `ReceiveMessage` long-poll wait parameter (no separately named `wait_time_seconds` key exists in 0.49); 20 s is AWS SQS's own hard ceiling for long-poll wait, matching `PLAN.md` §4.6.4's literal `WaitTimeSeconds=20` target and minimizing empty-poll round trips. | **[Unverified]** Vendor doc explicitly says "generally should not be changed unless instructed to do so" and does not quantify the latency effect of 15→20 s. https://vector.dev/docs/reference/configuration/sources/aws_s3/ |
| Vector aggregator (T1, S3/S4) | `sources.s3_in.sqs` visibility timeout | `300` s | `300` s (unchanged) — not set explicitly | S3 event notifications are deleted from the queue immediately on successful consumption (`sqs.delete_message` default `true`), so visibility timeout does not sit on the measured latency path — no tuning needed. | N/A |
| Cribl node t1 (standalone, S3/S4) | S3 source "Poll timeout (secs)" (`pollTimeoutSecs` — **TODO(verify)**, see §6) | `10` s (min 1, max 20) | `20` s — `ansible/roles/cribl-stream/templates/inputs.yml.j2:44` (value from `cribl_s3_poll_timeout_secs`, `ansible/roles/cribl-stream/defaults/main.yml:84`) | 20 s is the documented maximum for this field, matching AWS SQS's own `WaitTimeSeconds` ceiling and `PLAN.md` §4.6.4's literal target. Parity counterpart to Vector's `poll_secs: 20`. | **[Unverified]** No vendor-quantified figure for the 10→20 s change found. https://docs.cribl.io/stream/sources-s3/ |
| Both stacks | S3→SQS notification path | event-driven | unchanged — event-driven (S3 bucket notification → SQS, `PLAN.md` §4.3) | No polling scheduler on the notification path itself; only the SQS consumer's own long-poll wait is tuned above. Terraform-side (SQS queues fed by S3 event notifications) — see `terraform/modules/sqs-notify/`. | N/A — architectural constant, not a tuned value. |

### Item 5 — Process/thread scaling

| Component | Setting | Default | Tuned value | Why | Performance gain |
|---|---|---|---|---|---|
| Vector agent | `--threads` / `VECTOR_THREADS` | all available cores | unchanged (no flag/env var set) — `ansible/roles/vector-agent/templates/vector.service.j2` | Vector already defaults to full-core parallelism; nothing to tune toward. Recorded per `PLAN.md` §4.6.5's "Vector defaults to all cores — record both." | **[Unverified]** No vendor-quantified figure; this is a record-only row (no change made). https://vector.dev/docs/reference/cli/ |
| Vector aggregator | `--threads` / `VECTOR_THREADS` | all available cores | unchanged (no flag/env var set) — `ansible/roles/vector-aggregator/templates/vector.service.j2` (comment added) | Same as the agent row — the `m6i.xlarge` aggregator/standalone-Cribl-node tiers' 4 vCPUs are already fully used by Vector's default scheduling. | **[Unverified]** Same citation. |
| Cribl node t1/t2 (standalone, no leader/group) | Node-level `cribl.yml` → `workers.count` (`workerProcesses` — **TODO(verify)**, see §6) | `-2` (vCPU count − 2, per the pre-redesign Group Settings UI prose, which no longer applies to a standalone node) OR `1` (per `cribl.yml` `workers.count` schema comment — **discrepancy in Cribl's own docs, not reconciled**) | `4` (= vCPU count on `m6i.xlarge`) — `ansible/roles/cribl-stream/templates/cribl.yml.j2:27` (value from `cribl_worker_processes`, `ansible/roles/cribl-stream/defaults/main.yml:100`) — **if the Free license caps worker processes per node below this value, the deployed cap is recorded here instead of 4 and flagged as a documented asymmetry vs. Vector's all-cores default (`PLAN.md` §4.6.5), not equalized.** | A positive integer sets an EXACT worker-process count (`docs.cribl.io/stream/scaling/`: "Positive numbers specify an absolute number of Workers"); 4 = one worker process per vCPU on `m6i.xlarge`, maximizing parallelism per `PLAN.md` §4.6.5's "Cribl worker processes set to vCPU count *if the Free license permits*." Unlike the pre-redesign leader/worker-group model, this is now a **node-level** setting with no leader-pushed per-group override — each of the 4 standalone nodes sets its own `workers.count` independently. | **[Unverified]** Vendor doc states a sizing rule of thumb (~400 GB/day per physical core, per Cribl's scaling guidance) but no direct before/after latency figure for changing the worker-process count from -2/1 to 4 on this exact workload, and no confirmation yet of whether Cribl Free caps this value on a standalone node — record actual deployed value at deploy time regardless of outcome. https://docs.cribl.io/stream/scaling/ |

### Items 6–8 — Record-only (already done in terraform, or no config surface)

| Component | Setting | Status | Citation | Notes |
|---|---|---|---|---|
| NLB (Vector t1) | `enable_cross_zone_load_balancing` | DONE in terraform | `terraform/modules/vector-aggregator/main.tf:110` | Removes AZ-affinity queuing asymmetry (`PLAN.md` §4.6.6). |
| NLB (Vector t1) | `deregistration_delay` | DONE in terraform (30 s, down from 300 s default) | `terraform/modules/vector-aggregator/main.tf:138` | Terraform comment already reads "PLAN §4.6(6)". |
| NLB (Vector t2) | `enable_cross_zone_load_balancing` | DONE in terraform | `terraform/modules/vector-aggregator/main.tf:172` | Same rationale, tier-2 NLB. |
| NLB (Vector t2) | `deregistration_delay` | DONE in terraform (30 s) | `terraform/modules/vector-aggregator/main.tf:197` | Same rationale, tier-2 NLB. |
| NLB (Cribl t1) | `enable_cross_zone_load_balancing` | DONE in terraform | `terraform/modules/cribl-stream/main.tf:180` | Parity with Vector — identical value. |
| NLB (Cribl t1) | `deregistration_delay` | DONE in terraform (30 s) | `terraform/modules/cribl-stream/main.tf:207` | Parity with Vector — identical value. |
| NLB (Cribl t2) | `enable_cross_zone_load_balancing` | DONE in terraform | `terraform/modules/cribl-stream/main.tf:241` | Parity with Vector — identical value. |
| NLB (Cribl t2) | `deregistration_delay` | DONE in terraform (30 s) | `terraform/modules/cribl-stream/main.tf:264` | Parity with Vector — identical value. |
| NLB (both stacks) | Client keep-alive vs. NLB idle timeout | Record-only — no client-side keep-alive INTERVAL config exists on either stack to set below the NLB's idle timeout | Idle timeout default: 350 s for TCP flows, configurable 60–6000 s (`https://docs.aws.amazon.com/elasticloadbalancing/latest/network/network-load-balancers.html#connection-idle-timeout`) | Not a practical risk at 10k EPS/host (connections stay continuously busy, far under any 350 s idle window); see `vector-agent/templates/vector.yaml.j2` `to_agg_t1` sink comment for the full note. |
| Placement | Generators / aggregator tiers / endpoints in the same 2 AZs | DONE in terraform (zone-ID selection, not zone-letter) | `terraform/modules/sender-network/main.tf:20-31`, `terraform/modules/logging-network/main.tf:14-28` | Constraint only, per `PLAN.md` §4.6.7 — no ansible config change. Single-AZ pinning is further-evaluation work (`PLAN.md` §5.4.5). |
| OS/network | ENA | Record-only — AWS default on `m6i`, no toggle needed | n/a (AWS default) | `PLAN.md` §4.6.8. |
| OS/network | Jumbo frames / MTU | Record-only — default 9001 MTU in-VPC, unchanged | n/a (AWS/VPC default) | `PLAN.md` §4.6.8 explicitly says "no jumbo-frame changes." |
| OS/network | Unattended upgrades disabled during runs | DONE — already in `common` role | `ansible/roles/common/defaults/main.yml:25` (`common_disable_auto_updates: true`); consumers: `ansible/roles/common/tasks/linux.yml` (`dnf-automatic.timer` disable) and `ansible/roles/common/tasks/windows.yml` (`wuauserv` disable) | Verified present and unmodified by this task (role `common` is out of scope for this task's edits). |
| OS/network | Generator output volume | DONE in terraform — gp3, default IOPS, NOT tmpfs | `terraform/modules/generator-hosts/main.tf:87` (`volume_type = "gp3"`) | Durability realism per `PLAN.md` §4.6.8; the event-generator role writes to the OS filesystem path (`/opt/llt/generator/out` Linux, `C:\llt\generator\out` Windows), which resolves to this gp3-backed root volume. |

## 5. Batch-constant-unchanged evidence (grep)

Run at build time immediately after all tuning edits, confirming the exact
numeric values in `PLAN.md` §4.5 are untouched:

```
$ grep -n "vector_s3_batch_timeout_secs\|vector_s3_batch_max_bytes" \
    ansible/roles/vector-agent/defaults/main.yml ansible/roles/vector-aggregator/defaults/main.yml
ansible/roles/vector-agent/defaults/main.yml:78:vector_s3_batch_timeout_secs: 5
ansible/roles/vector-agent/defaults/main.yml:79:vector_s3_batch_max_bytes: 10485760      # 10 MB
ansible/roles/vector-aggregator/defaults/main.yml:42:vector_s3_batch_timeout_secs: 5           # ->S3 hops: 5 s
ansible/roles/vector-aggregator/defaults/main.yml:43:vector_s3_batch_max_bytes: 10485760       # 10 MB

$ grep -n "cribl_edge_s3_flush_secs\|cribl_edge_s3_max_bytes\|cribl_s3_flush_secs\|cribl_s3_max_bytes_mb" \
    ansible/roles/cribl-edge/defaults/main.yml ansible/roles/cribl-stream/defaults/main.yml
ansible/roles/cribl-edge/defaults/main.yml:93:cribl_edge_s3_flush_secs: 5              # S3 flush 5 s (== Vector timeout_secs)
ansible/roles/cribl-edge/defaults/main.yml:94:cribl_edge_s3_max_bytes: 10485760       # 10 MB (== Vector max_bytes)
ansible/roles/cribl-stream/defaults/main.yml:57:cribl_s3_flush_secs: 5              # ->S3: 5 s  (== Vector timeout_secs)
ansible/roles/cribl-stream/defaults/main.yml:58:cribl_s3_max_bytes_mb: 10           # 10 MB      (== Vector max_bytes)

$ grep -n "timeout_secs: 1$\|flushPeriodSec: 1$" \
    ansible/roles/vector-agent/templates/vector.yaml.j2 ansible/roles/cribl-edge/templates/outputs.yml.j2
ansible/roles/vector-agent/templates/vector.yaml.j2:175:      timeout_secs: 1
ansible/roles/cribl-edge/templates/outputs.yml.j2:24:    flushPeriodSec: 1

$ grep -n "vector_http_batch_timeout_secs\|cribl_http_flush_secs" \
    ansible/roles/vector-aggregator/defaults/main.yml ansible/roles/cribl-stream/defaults/main.yml
ansible/roles/vector-aggregator/defaults/main.yml:44:vector_http_batch_timeout_secs: 1         # inter-aggregator HTTP: 1 s (PLAN §4.5)
ansible/roles/cribl-stream/defaults/main.yml:59:cribl_http_flush_secs: 1            # inter-tier HTTP: 1 s (== Vector http batch)
```

All values match `PLAN.md` §4.5 exactly (5 s / 10 MB for S3, 1 s for
inter-tier HTTP) and match the pre-tuning values recorded before the leader
→ standalone-node redesign and any §4.6 edits began. The Cribl role directory
is `ansible/roles/cribl-stream/` (4 independent standalone nodes, no leader);
it superseded the pre-redesign `cribl-leader` role referenced in earlier
drafts of this table.

## 6. Unverified exact config keys — explicit TODO list

Per this task's "leave a clearly-commented TODO in-config rather than guess
silently" instruction, the following config keys could **not** be confirmed
against a live Cribl 4.13 instance or an exposed JSON/YAML schema page (only
UI-label documentation was available in the pages fetched at build time).
Each has an in-config `TODO(verify)` comment at the cited location:

| # | Setting (UI label) | Best-guess key used | Location | What to check at deploy time |
|---|---|---|---|---|
| 1 | File Monitor "Polling interval" | `servicePeriodSecs` | `ansible/roles/cribl-edge/templates/inputs.yml.j2` | Compare against `$CRIBL_HOME/local/cribl/inputs.yml` on a live Edge node after setting the value via UI; correct the key if Cribl silently ignores it. |
| 2 | Webhook "Request concurrency" | `concurrency` | `ansible/roles/cribl-edge/templates/outputs.yml.j2`, `ansible/roles/cribl-stream/templates/outputs.yml.j2` | Compare against `$CRIBL_HOME/local/cribl/outputs.yml` on the standalone node (both Edge and Stream nodes use the same standalone file-config path — no leader, no `groups/<g>/` tree) after setting via UI. |
| 2 | Webhook "Keep alive" | not set (relying on documented default) | n/a — deliberately not templated to avoid guessing a wrong key | If parity requires making this explicit, confirm the key name first. |
| 4 | S3 source "Poll timeout (secs)" | `pollTimeoutSecs` | `ansible/roles/cribl-stream/templates/inputs.yml.j2` | Compare against `$CRIBL_HOME/local/cribl/inputs.yml` on the standalone node for the S3 source type. |
| 5 | Node-level `cribl.yml` "Worker Processes" | `workers.count` (`workerProcesses` in the pre-redesign Group Settings UI naming) | `ansible/roles/cribl-stream/templates/cribl.yml.j2` | Compare against `$CRIBL_HOME/local/cribl/cribl.yml` on the standalone node after setting via UI. Note the unreconciled discrepancy between the (now-inapplicable) Group Settings UI's documented default (`-2`) and the node-level `cribl.yml` `workers.count` schema comment's documented default (`1`) — since each of the 4 nodes is standalone (no leader, no per-group override), this key is purely node-local; confirm the effective value on a live node, and whether Cribl Free caps it, before relying on this row for the analysis phase. |

None of these TODOs affect the §4.5 batch constants, ports, or `hop_ts.*`
field names (verified separately, §5) — they are scoped entirely to the
§4.6 tuning knobs themselves. If any resolve to a different key at deploy
time, update this table's "Tuned value" file+line citation and the affected
config's inline comment together.
