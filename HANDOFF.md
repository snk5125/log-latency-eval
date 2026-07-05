# HANDOFF — llt Hop-Latency Experiment

**Purpose of this file:** context/plan hand-off so a new session (or agent)
can resume this project without re-deriving state. Read this, then PLAN.md
(the binding spec), then the referenced files as needed.

**Last updated:** 2026-07-03 (session 3: LIVE deploy + converge + smoke test;
2× duplication investigated & analyzer hardened; **instances STOPPED overnight**)
**Repo state:** local git, branch `main`, working tree clean, **nothing pushed**.
**Phase:** DEPLOYED (PLAN §8 phase 2). Infra is APPLIED and CONVERGED on real
AWS (two accounts, 12 EC2). Pipeline PROVEN end-to-end on the Linux Vector path
(s1-vec-vagg smoke). **The 12 EC2 instances are currently STOPPED** to save cost
overnight — see **§0 RESUME** below to pick up. Terraform state, converged host
software, VPCs/NLBs/endpoints, and S3 buckets all remain intact. **The 48-run
measurement matrix has NOT been run yet — REPORT.md findings are still [PENDING].**

---

## 0. RESUME (start here next session)

**Instances are STOPPED, not destroyed.** Everything else (network, buckets,
converged software) is intact, so resume is fast — no re-apply needed.

1. **Start the fleet** (both accounts, region us-east-2):
   ```
   for P in llt-sender llt-logging; do
     IDS=$(aws ec2 describe-instances --profile $P --region us-east-2 \
       --filters "Name=tag:Project,Values=llt" "Name=instance-state-name,Values=stopped" \
       --query "Reservations[].Instances[].InstanceId" --output text)
     [ -n "$IDS" ] && aws ec2 start-instances --profile $P --region us-east-2 --instance-ids $IDS
   done
   ```
   Wait ~2–3 min for SSM to come online (instance-ids are stable across stop/start;
   private IPs are stable; only public IPs, which we don't use, may change).
2. **(Recommended) Re-converge** `ansible/playbooks/site.yml` — all session-3 fixes
   are committed, so this should now be clean 11/12 (win-ce still blocked on the
   Cribl MSI, item 3). Confirms software survived the stop/start. Env recipe: isolated
   venv at `…/scratchpad/llt-venv`, `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`,
   `LLT_AWS_PROFILE_SENDER=llt-sender LLT_AWS_PROFILE_LOGGING=llt-logging`,
   `ansible_aws_ssm_plugin=~/bin/session-manager-plugin`, forks=5.
3. **Run the matrix:** `python3 harness/orchestrator/run_matrix.py --profile-sender
   llt-sender --profile-logging llt-logging` (start with `--dry-run`, then a single
   cell e.g. `s1-vec-vagg-1k` to sanity-check, then the full 48). **TRIPWIRE:** a
   correct run's `dup` column must be **~0%**; ~100% dup means the aggregator wasn't
   reconfigured with the run_id (see §8 — the 2× we already root-caused).
4. `harness/analysis/analyze.py` → `report/evidence/`, then fill REPORT.md.

**If you'd rather stop paying the residual fixed cost** (NAT/NLB/endpoints,
~$0.35–0.50/hr while instances are stopped): `scripts/teardown.sh` does a full
`terraform destroy` — but then resume requires a full re-apply (~15 min) +
re-converge, not just start-instances.

**Session-2 changes (all committed, offline-verified, NOT deployed):**
- Full spec-conformance review (4 review agents) → fixes across terraform,
  ansible, harness, scripts. Terraform: PrivateLink endpoint SG opened to :8080
  (S1/S2 were dead), artifacts bucket cross-account policy, IAM provider
  pinning, TCP health checks, AZ-by-zone-id (cross-account PrivateLink
  alignment), deregistration_delay, Name tags `…-01`. Ansible: `become` on all
  Linux plays (aws_ssm runs as ssm-user), `llt_role` hyphen→underscore, Vector
  aws_s3 JSON decode, Cribl workers made leader-managed (config in
  `cribl-leader/templates/groups/`, commit+deploy via REST; worker-local
  templates deleted), worker join port/token fixed, Windows Python+NSSM install,
  Edge MSI, JSON breakers, forward-slash Windows config paths, cross-pairing
  (agent targets the cell's aggregator). Harness: eventgen `--config`, EPS/
  duration plumbing, SSM manifest upload, `(host_os,seq)` analyzer keying,
  streaming aggregation, two-flush e2e adjustment, SAFE two-phase
  `--parallel-stacks` (old partition shared generator hosts). Scripts: teardown
  `mapfile`→portable + 1000-key chunk + `Project=llt` guard; preflight jq/tfvars.
- #8 diagrams: `docs/diagrams/` (topology, scenario-s1..s4, measurement) —
  Mermaid + rendered SVG, referenced from README/ARCHITECTURE/REPORT.
- #9/#10 tuning: PLAN §4.6 items 1–8 applied to ansible roles; `docs/TUNING.md`
  (source of truth) + REPORT §3.8 mirror, gain column `[Unverified]`.
- Offline verification (all PASS): `terraform validate`; ansible
  `--syntax-check` ×3; `analyze.py --self-test` (67 assertions); `run_matrix
  --dry-run` serial + `--parallel-stacks` (48 cells, safe partition, 0 leftover);
  `bash -n` ×4; 76 template renders; batch constants 5s/10MB/1s + ports
  8080/8081 + `hop_ts.*` confirmed unchanged by the tuning pass.

**Cribl licensing redesign (session 2, RESOLVED + committed):** the user
confirmed Cribl Free supports only a **single worker group per leader** — the
original leader + 2-worker-group design was invalid. The Cribl stack was
rebuilt as **4 independent single-instance standalone Cribl Stream nodes, no
leader** (2 behind `llt-cs-t1-nlb`, 2 behind `llt-cs-t2-nlb`), each locally
file-configured by the new `cribl-stream` role (the `cribl-leader` +
`cribl-worker` roles were deleted). PLAN §4.3/§4.6.5/§5A rewritten to match.
Instance count **13 → 12**. This removed the leader REST commit/deploy,
`users.json` seeding, and distributed-licensing TODOs entirely. Verified:
terraform validate, ansible syntax ×3, 40 cribl-stream renders, 48-cell
dry-run, ports/batch/`hop_ts` unchanged, `cribl-stream` var seam
(generated_infra + defaults) consistent, topology SVG re-rendered without the
leader.

**Session-3 changes (LIVE two-account deploy; 12 EC2 up, SSM-online):** the
stack was deployed and converged against real AWS. Fixed the remaining
generator-host converge failures and proved the pipeline end-to-end:
- **Issue A (lin-ce cribl-edge):** `boot-start enable` in Edge mode creates the
  unit `cribl-edge.service` (verified live), not `cribl` — fixed
  `cribl_edge_service_name` + added an explicit start-with-daemon_reload task
  (parity with cribl-stream). lin-ce now converges `failed=0`.
- **Issue B (win-vec generator):** python locate returned empty because the
  installer's PATH edit doesn't reach the open SSM session; made the locate
  refresh the machine PATH + glob known dirs. Follow-ons surfaced by re-converge:
  win_nssm couldn't find `nssm.exe` (same session-PATH staleness) → pin
  `executable:` to the absolute path in event-generator + vector-agent; and that
  var (`common_win_nssm_exe`) was out of scope in those plays → added it as a
  role default in both.
- **Issue C part 1 (win-ce facts):** the Windows `setup` module emits a stray
  `\r` in `ansible_date_time.hour` over SSM, corrupting JSON framing →
  "MODULE FAILURE" despite complete facts. Excluded `date_time` via
  `gather_subset: [all, "!date_time"]` on all Windows-touching plays. Both
  Windows hosts now pass fact-gather.
- **Issue C part 2 (win-ce cribl-edge MSI):** EXTERNAL BLOCKER — see item 3
  below. win-ce is the only host that cannot converge; it is not needed for the
  smoke test or the Linux path.
- **Converge result:** **11/12 hosts `failed=0`** (confirmed after re-converge:
  win-vec is now green; all 8 aggregators + both Linux generators green). The sole
  remaining failure is **win-ce** at the Cribl Edge MSI download — external blocker
  only (item 3). 8 fix commits `83c922c`..`b7b3ade` on `main`.
- **SMOKE TEST — PIPELINE PROVEN (s1-vec-vagg, Linux):** ran the generator on
  llt-lin-vec-01 (eps 1000, 10 s warmup + 60 s), Vector agent → vagg Tier-1 NLB
  :8080 → final S3. The `final/` prefix of `llt-final-<logging-acct>` went from
  0 → 28 objects (~3.2 MB each). A real measurement event carried
  `t_gen_ns: 1783132703549826294`, `hop_ts.agent: 1783132703552`,
  `hop_ts.agg1: 1783132704563` (deltas: gen→agent 2.17 ms, agent→agg1 1011 ms).
  `analyze.py` produced per-hop stats (gen→agent mean 1.15 ms; agent→agg1 mean
  510 ms; agg_last→final adj_mean 0 ms after the 5 s flush subtraction;
  end-to-end mean 3924 ms). Analysis + generator + agent + aggregator + S3 sink
  all confirmed working on live infra.
- **2× duplication in the smoke stats — INVESTIGATED & RESOLVED (not a bug).**
  The smoke `analyze.py` showed `dup≈60014` on 60000 unique measurement events.
  Root-caused from the raw objects: the 28 final objects split into two ~70 s
  write bursts, and each `seq`'s two copies carried **different `t_gen_ns` (~125 s
  apart)** — i.e. the generator RAN TWICE under one `run_id="smoke"`, not a
  transport duplicate (a retry carries identical `t_gen_ns`). The smoke path set
  run_id only in generator.json and started the service directly, **bypassing
  `configure-scenario.yml`**, so the aggregators kept converge-time `run_id=""`
  and wrote both runs to the unpartitioned prefix `final//linux/`; analyze (empty
  run_id) swept both. A real `run_matrix` cell will NOT do this: unique run_id →
  `configure-scenario.yml` **Play D** re-templates the AGGREGATOR sink to
  `final/{run_id}/{host_os}/` → generator starts once (`eventgen.py` opens output
  `mode="w"`, truncate) → analyze reads exactly `final/{run_id}/`. The pipeline
  delivered each generated event exactly once (only ~0.03% true at-least-once
  boundary dups, which analyze already dedups). **Hardening applied** (commit this
  session): `analyze.py iter_final_events` now also cross-checks the event body's
  run_id (`_event_in_run`) as defense-in-depth against cell-boundary bleed;
  self-test now **72 assertions** (was 67). **Cleanup:** the 28 orphan smoke
  objects at `final//linux/` were deleted — `final/` is empty again.

**⚠ DEPLOY-TIME TODOs (verify before first apply — could not be validated
offline):**
1. **Standalone Cribl Stream — CONVERGE VALIDATED, data-path NOT yet smoke-tested.**
   All 4 Cribl Stream aggregator nodes converged `failed=0` live this session
   (tarball install, Stream mode by default with no `mode-*` command, file config
   under `$CRIBL_HOME/local/cribl/` read without a leader/commit — all confirmed at
   converge). ⚠ REMAINING: the smoke test exercised only the **Vector** data path
   (s1-vec-vagg). The Cribl aggregator RECEIVE→forward→S3-write path (any `*-cs-*`
   cell) has not yet had events pushed through it — run one `s1-ce-cs-1k` (or
   `s1-vec-cs-1k`) cell early in the matrix to confirm the Cribl sink writes to
   `final/{run_id}/` before trusting all 24 Cribl-aggregator cells.
2. **3 Cribl 4.13 config keys still best-guess** (marked TODO in-config +
   `docs/TUNING.md` §6): File Monitor `servicePeriodSecs`, Webhook `concurrency`,
   S3 source `pollTimeoutSecs`. (`workerProcesses` is superseded by the
   documented `cribl.yml workers.count` above.) Wrong keys are ignored/error at
   converge; deploy is gated anyway.
3. **Cribl Edge Windows install — EXTERNAL BLOCKER (win-ce host only).**
   Investigated live (2026-07): the pinned MSI URL in `cribl-edge/defaults`
   (`https://cdn.cribl.io/dl/4.18.2/cribl-4.18.2-windows-x64.msi`) 404s, AND
   **no MSI exists for 4.18.2 on the public CDN** — every filename permutation
   tried 404s (with/without build hash `fd1f0d2f`, with/without `-edge-`,
   `-win-`, etc.). Confirmed:
   - `https://cdn.cribl.io/dl/4.18.2/cribl-4.18.2-fd1f0d2f-linux-x64.tgz` -> 200 (Linux, used)
   - `https://cdn.cribl.io/dl/4.18.2/cribl-4.18.2-fd1f0d2f-windows-x64.zip` -> **200** (82 MB, unpacks to `cribl/bin/...`)
   - all `.../cribl-4.18.2*windows*.msi` permutations -> **404**
   Cribl's own docs (docs.cribl.io/edge/deploy-windows/) say the ONLY supported
   Windows install is the **MSI** (obtained interactively from the Cribl
   Download page — a session/redirect-gated link, not a stable scriptable
   `cdn.cribl.io` path), that it installs the Windows service (name `cribl`,
   NSSM-backed, UI on :9420), and that there is **no supported ZIP install
   path**. So there is no publicly-scriptable, reproducible installer URL for
   Cribl Edge 4.18.2 on Windows.
   RESOLUTION NEEDED (one of): (a) get the real 4.18.2 Windows MSI URL from an
   authenticated Cribl account / the Download page and pin it (then
   `win_package` silent `msiexec /qn /i` per docs), or (b) accept an
   UNSUPPORTED ZIP install (win_get_url the windows-x64.zip + win_unzip to
   C:\Cribl + wrap `cribl.exe` under NSSM ourselves — but Windows `cribl.exe
   boot-start` is undocumented, so the service wiring would be hand-rolled and
   is not guaranteed to match the MSI's behavior). Until (a), win-ce cannot be
   converged reproducibly. win-ce is the LEAST-important host: the smoke test
   and the Linux Vector path do not need it, and the experiment matrix's Cribl
   agent is exercised on Linux (lin-ce). Not blocking anything else.

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

### Pending build tasks — ✅ ALL COMPLETE (session 2)
- **#8 Diagrams** (PLAN §5A): ✅ `docs/diagrams/` topology + scenario-s1..s4 +
  measurement, Mermaid + rendered SVG, names/ports/buckets match deployed
  reality, referenced from README/ARCHITECTURE/REPORT.
- **#9 Low-latency tuning** (PLAN §4.6 items 1–8): ✅ applied to Ansible roles,
  parity-preserving; batch constants/ports/topology/hop_ts unchanged (grep-
  verified). Read-from-end tension (§4.6.1) resolved: generator truncates its
  file per run + agent starts first, so beginning==end offset (documented).
- **#10 Tuning reference table**: ✅ `docs/TUNING.md` (source of truth) + REPORT
  §3.8 mirror; gain column `[Unverified]` vendor-doc expectation until measured.

### Build phase artifacts COMPLETE → still ⏸ before phase 2 (deploy)

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
