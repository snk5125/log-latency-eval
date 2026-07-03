# HANDOFF — llt Hop-Latency Experiment

**Purpose of this file:** context/plan hand-off so a new session (or agent)
can resume this project without re-deriving state. Read this, then PLAN.md
(the binding spec), then the referenced files as needed.

**Last updated:** 2026-07-03
**Repo state:** local git, branch `main`, HEAD `fc9655e`, 123 tracked files.
**Phase:** BUILD (PLAN §8 phase 1) — ⏸ paused by user. **No AWS deployment
has occurred. No costs incurred. No results exist yet — REPORT.md findings
are all [PENDING].**

---

## 1. What this project is

Peer-review-grade AWS experiment answering: *how much average latency is
added per additional hop in a logging pipeline (4 scenarios: agg / agg→agg /
S3→agg / S3→agg→agg), and does event volume (1k/5k/10k EPS) change it?*
Two agents (Vector, Cribl Edge) × two aggregator stacks (Vector, Cribl
Stream free license) × 4 scenarios × 3 volumes = 48 runs; Linux + Windows
generators concurrent in every run. Two AWS accounts (sender/logging),
PrivateLink to Tier-1 NLBs, S3 landing buckets via S3 endpoints, per-hop
timestamps embedded in events. Deliverable: formal markdown report
(report/REPORT.md) + fully reproducible terraform/ansible/harness repo.

Authoritative spec: **PLAN.md** — §4 architecture, §4.4 HTTP/NDJSON parity
rule, §4.5 batch constants (5 s S3 / 1 s inter-tier), §4.6 low-latency
tuning profile + required tuning table, §5 measurement methodology, §5A
diagram deliverables, §7 binding conventions, §9 build work split.

## 2. Session decisions (user-confirmed, do not re-ask)

- Build artifacts first; **pause before any deployment/execution**. User will
  authorize deployment explicitly and supply two AWS profiles.
- Volume tiers: **1k / 5k / 10k EPS per host** (not the originally proposed 100/1k/10k).
- Cribl: **self-hosted Cribl Stream, Free license** (not Cribl.Cloud).
- Git: **local repo only** for now; push to remote after testing concludes.
  .gitignore blocks tfstate/tfvars/keys/generated vars/raw evidence.
- Build executed by **multiple Opus subagents** (user requirement) — same
  pattern expected for remaining build tasks.

## 3. Build status

### Done (committed)
| Commit | Content |
|--------|---------|
| `857bfd0` | Full build: PLAN.md, terraform/ (41 files, `terraform validate` PASS on 1.9.8/aws 5.100), ansible/ (ansible-lint PASS, 0 warnings), harness/ (eventgen + 48-cell orchestrator + analyzer, offline self-test 33 assertions PASS), scripts/ (preflight/setup/teardown/gen-ansible-vars), README, docs/ARCHITECTURE.md, docs/RUNBOOK.md, report/REPORT.md template |
| `7631f86` | Spec additions: §5A diagrams, §4.6 tuning profile |
| `fc9655e` | Spec addition: §4.6 tuning reference table requirement |

### Integration bugs found and FIXED during verification (don't reintroduce)
1. Ansible `llt_*`/role infra vars had no source → added
   `scripts/gen-ansible-vars.sh` (terraform outputs → gitignored
   `ansible/inventories/group_vars/all/generated_infra.yml`; committed
   `.example` shows shape). Also added terraform output
   `cribl_leader_private_ip` (root + cribl-stream module).
2. setup.sh uploaded eventgen.py to `harness/eventgen.py` but the
   event-generator role fetches `harness/generator/eventgen.py`, and did so
   AFTER site.yml needed it → key fixed, step reordered (now 8 steps).
3. setup.sh referenced nonexistent `inventories/aws_ec2.yml` → now uses the
   `inventories/` directory (sender + logging aws_ec2 files union).
4. Hosts span two accounts but SSM connection used one profile → per-host
   `llt_ssm_profile` composed in each inventory file from
   `LLT_AWS_PROFILE_SENDER`/`LLT_AWS_PROFILE_LOGGING`; run_matrix.py
   exports these env vars (setdefault) from its --profile args.

### Pending build tasks (user-approved, NOT started — next work)
- **#8 Diagrams** (PLAN §5A): Mermaid + SVG in `docs/diagrams/` — topology,
  scenario-s1..s4 with timestamp capture points, measurement derivation.
  Must match deployed names/ports/buckets exactly.
- **#9 Low-latency tuning** (PLAN §4.6 items 1–8): apply parity-preserving
  profile to Ansible roles. Constraints that may NOT change: HTTP/NDJSON
  transport, §4.5 batch constants, instance types, topology, equal treatment
  of both stacks.
- **#10 Tuning reference table** (PLAN §4.6 table spec): `docs/TUNING.md`
  (source of truth) + mirror in REPORT.md — setting/default/tuned/why/gain;
  gain column = [Unverified] vendor-doc expectation until measured.
  #10 depends on #9. Suggested split: one subagent for #8, one for #9+#10.

### After build tasks complete → still ⏸ before phase 2 (deploy)

## 4. Execution plan after user authorizes deploy (PLAN §8)

1. User provides two AWS named profiles (defaults assumed: `llt-sender`,
   `llt-logging`), region default `us-east-2`; create `terraform/terraform.tfvars`
   from the `.example` (gitignored).
2. `scripts/setup.sh` — preflight → terraform apply (13 instances: 4
   generators, 4 vector agg, 1 cribl leader + 4 workers) → SSM wait →
   var bridge → eventgen upload → ansible site.yml.
3. `python3 harness/orchestrator/run_matrix.py` — 48 cells, each: configure
   scenario → assert clocks (abort cell on failure) → SSM-start generators →
   2 min warmup + 10 min measure + 120 s drain → collect manifests.
   ~12 h sequential, ~6 h with `--parallel-stacks`. Resumable via state file.
4. `harness/analysis/analyze.py` — per-hop stats (mean/p50/p90/p99/max/
   stddev/count/loss), raw + batch-adjusted, → report/evidence/.
5. Finalize report/REPORT.md: fill [PENDING] tables, record deployed
   versions (Vector 0.49.0 / Cribl 4.13.0 pinned in ansible defaults),
   populate tuning-table gain column from evidence.
6. `scripts/teardown.sh` (empties buckets, destroys). Cost while deployed
   ~$3–6/hr (README estimate, labeled unverified).

## 5. Known caveats / traps for the next operator

- **Cost/licensing:** verify Cribl Free current terms at deploy; Windows
  license cost baked into EC2 pricing; NAT gateway toggle exists for package
  install (var `enable_nat`) — consider disabling after converge.
- **Methodology invariants:** batch constants and transport parity are part
  of the measurement definition — changing them invalidates cross-run
  comparability. hop_ts field names (`hop_ts.agent/agg1/agg2`) are
  load-bearing across ansible templates AND analyze.py.
- **S3 timestamp precision:** LastModified is second-precision; analyzer
  prefers `x-amz-meta-llt-put-ms` object metadata if a sink can set it
  (currently neither sink sets it — fallback path is the operative one) and
  the 5 s flush dominates →S3 hops; report both raw and batch-adjusted.
- **Windows:** w32time is coarser than chrony (5 ms vs 1 ms assertion
  gates); Windows deltas get wider error bars in the report.
- **Free-tier Cribl leader auth:** admin password generated on the node,
  retrieved via SSM only — never committed.
- **Subagent boundary rule used throughout:** A=terraform, B=ansible,
  C=harness+scripts, D=README/report/docs. Verification pass at the end is
  the integrator's job (cross-component naming, then commit).

## 6. Key file index

| File | Role |
|------|------|
| PLAN.md | Binding spec — read before touching anything |
| terraform/ | Two-account infra; root outputs feed gen-ansible-vars.sh |
| ansible/playbooks/{site,configure-scenario,assert-clocks}.yml | Converge / per-cell reconfig / clock gate |
| ansible/roles/* | time-sync, event-generator, vector-agent, cribl-edge, vector-aggregator, cribl-leader, cribl-worker, common |
| harness/orchestrator/run_matrix.py | 48-cell driver (dry-run works offline) |
| harness/analysis/analyze.py | Stats engine (`--self-test` runs offline) |
| scripts/setup.sh / teardown.sh / gen-ansible-vars.sh / preflight.sh | Lifecycle |
| report/REPORT.md | Formal report template, findings [PENDING] |
| docs/{ARCHITECTURE,RUNBOOK}.md | Peer-review detail + ops steps |

## 7. Task list snapshot (session task tool)

#1–#7 completed (spec, skeleton, 4 build subagents, verification+commit).
#8 diagrams — pending. #9 tuning profile — pending. #10 tuning table —
pending (depends on #9). All three are build-phase; deployment remains
gated on explicit user authorization.
