#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_matrix.py — orchestrates the 48-run llt hop-latency matrix (PLAN §3, §8).

Per cell (PLAN orchestration requirements):
  1. Render scenario config via `ansible-playbook configure-scenario.yml` (subprocess).
  2. Assert clock sync via `assert-clocks.yml`; ABORT the cell on failure (PLAN §5.3).
  3. Start generators on the correct Linux+Windows host pair via SSM SendCommand
     (systemd start on Linux / Windows service start) with run params.
  4. Wait warmup + measurement + drain buffer.
  5. Stop generators; collect per-host generator manifests via the artifacts bucket.
  6. Write a run-manifest JSON to report/evidence/ and to the artifacts bucket.

Properties:
  * Idempotent + resumable via a JSON state file (completed run-IDs skipped).
  * --dry-run prints the full plan and makes NO AWS calls.
  * --only accepts glob filters (e.g. 's2-vec-vagg-5k', 's*-vec-*-10k').
  * --parallel-stacks runs two cells concurrently ONLY when they differ in BOTH
    the agent and the aggregator dimension (disjoint generator hosts AND disjoint
    aggregator stacks). Two-phase schedule with a hard barrier between phases:
      phase 1: (vec,vagg) lane  ∥  (ce,cs) lane
      phase 2: (vec,cs)  lane  ∥  (ce,vagg) lane
    Cells sharing a generator pair (same agent) or an aggregator stack must NOT
    run concurrently: configure-scenario re-templates generator.json/agent config
    on the shared hosts and the shared aggregator tier configs, which would
    clobber a run in flight.
  * SSM command polling uses bounded exponential backoff.

boto3 is imported lazily inside AWS code paths so this module compiles and --dry-run
runs on machines without boto3/credentials installed.
"""

import argparse
import concurrent.futures
import datetime
import fnmatch
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Repo-relative paths. This file lives at harness/orchestrator/run_matrix.py, so
# the repo root is two levels up. All artifact paths are derived from it.
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
ANSIBLE_DIR = os.path.join(REPO_ROOT, "ansible")
PLAYBOOK_DIR = os.path.join(ANSIBLE_DIR, "playbooks")
EVIDENCE_DIR = os.path.join(REPO_ROOT, "report", "evidence")
DEFAULT_STATE_FILE = os.path.join(EVIDENCE_DIR, "run_matrix_state.json")
SCENARIOS_YAML = os.path.join(_THIS_DIR, "scenarios.yaml")

# Ansible playbooks referenced (PLAN §6 layout).
PB_CONFIGURE = os.path.join(PLAYBOOK_DIR, "configure-scenario.yml")
PB_ASSERT_CLOCKS = os.path.join(PLAYBOOK_DIR, "assert-clocks.yml")


# ---------------------------------------------------------------------------
# YAML loading. PyYAML is standard on control nodes with Ansible installed; if it
# is genuinely absent we fail with a clear message rather than a traceback.
# ---------------------------------------------------------------------------
def _load_scenarios(path=SCENARIOS_YAML):
    try:
        import yaml  # noqa: WPS433 - intentional local import
    except ImportError:  # pragma: no cover
        sys.stderr.write(
            "run_matrix: PyYAML is required to parse scenarios.yaml "
            "(pip install pyyaml; it ships with Ansible).\n"
        )
        raise
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Cell model
# ---------------------------------------------------------------------------
class Cell(object):
    """A single matrix cell = one orchestrated run (Linux+Windows concurrent)."""

    def __init__(self, scenario, agent, aggregator, volume, spec):
        self.scenario = scenario        # e.g. 's2'
        self.agent = agent              # 'vec' | 'ce'
        self.aggregator = aggregator    # 'vagg' | 'cs'
        self.volume = volume            # '1k' | '5k' | '10k'
        self.spec = spec                # resolved parameter dict
        # Run-ID prefix (timestamp appended at launch) — PLAN §3 convention.
        self.run_id_prefix = "%s-%s-%s-%s" % (scenario, agent, aggregator, volume)

    @property
    def stack(self):
        """Aggregator stack technology ('vector'|'cribl') — used for --parallel-stacks."""
        return self.spec["aggregator"]["stack"]

    def new_run_id(self):
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return "%s-%s" % (self.run_id_prefix, ts)

    def __repr__(self):
        return "<Cell %s>" % self.run_id_prefix


def expand_matrix(scen):
    """Expand scenarios.yaml into the full list of Cells (48 for the full matrix)."""
    m = scen["matrix"]
    cells = []
    for scenario, agent, aggregator, volume in itertools.product(
        m["scenario"], m["agent"], m["aggregator"], m["volume"]
    ):
        agent_map = scen["agent_token_map"][agent]
        host_pair_key = agent_map["host_pair"]
        agg_key = scen["aggregator_token_map"][aggregator]["key"]
        spec = {
            "scenario_def": scen["scenarios"][scenario],
            "host_pair": scen["host_pairs"][host_pair_key],
            "aggregator": scen["aggregators"][agg_key],
            "eps": scen["volumes"][volume]["eps"],
            "agent_stack": agent_map["stack"],
            "defaults": scen["defaults"],
            "final_bucket": scen["final_bucket"],
            "artifacts_bucket": scen["artifacts_bucket"],
        }
        cells.append(Cell(scenario, agent, aggregator, volume, spec))
    return cells


def filter_cells(cells, only_globs):
    """Filter cells by run-ID-prefix glob(s). Empty/None -> all cells."""
    if not only_globs:
        return cells
    kept = []
    for cell in cells:
        for pat in only_globs:
            if fnmatch.fnmatch(cell.run_id_prefix, pat):
                kept.append(cell)
                break
    return kept


# ---------------------------------------------------------------------------
# State (resumability). A flat JSON file mapping run_id_prefix -> record.
# ---------------------------------------------------------------------------
def load_state(path):
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {"completed": {}, "version": 1}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# AWS helpers (lazy). All boto3 usage is confined here so --dry-run never imports it.
# ---------------------------------------------------------------------------
class Aws(object):
    def __init__(self, region, logging_profile, sender_profile):
        import boto3  # noqa: WPS433 - lazy on purpose
        self._logging = boto3.Session(profile_name=logging_profile, region_name=region)
        self._sender = boto3.Session(profile_name=sender_profile, region_name=region)
        self.region = region
        self.ssm = self._sender.client("ssm")       # generators live in sender acct
        self.s3 = self._logging.client("s3")         # buckets live in logging acct
        self.ec2 = self._sender.client("ec2")

    def resolve_instance_id(self, host_tag_value):
        """Resolve an EC2 instance ID from its Name tag (host_id convention §7).

        Filters: tag:Name == host_id AND tag:Project=llt AND tag:Role=generator
        AND state=running, so a similarly named instance from another project (or
        a half-terminated host) can never be selected. Exactly ONE instance must
        match — SSM commands sent to an arbitrary pick would corrupt the run, so
        ambiguity is a hard error, never a silent first-match.
        """
        resp = self.ec2.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [host_tag_value]},
                {"Name": "tag:Project", "Values": ["llt"]},
                {"Name": "tag:Role", "Values": ["generator"]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        )
        matches = [
            inst["InstanceId"]
            for res in resp.get("Reservations", [])
            for inst in res.get("Instances", [])
        ]
        if not matches:
            raise RuntimeError(
                "no running instance with tag:Name=%r (and Project=llt, "
                "Role=generator)" % host_tag_value)
        if len(matches) > 1:
            raise RuntimeError(
                "ambiguous host %r: %d running instances match "
                "(Project=llt, Role=generator): %s — refusing to pick one"
                % (host_tag_value, len(matches), ", ".join(sorted(matches))))
        return matches[0]

    def send_command(self, instance_id, document_name, parameters, comment):
        """Issue an SSM SendCommand; return the command ID."""
        resp = self.ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName=document_name,
            Parameters=parameters,
            Comment=comment[:100],
            TimeoutSeconds=3600,
        )
        return resp["Command"]["CommandId"]

    def wait_command(self, command_id, instance_id, timeout_s=900):
        """Poll GetCommandInvocation with bounded exponential backoff until terminal."""
        deadline = time.time() + timeout_s
        delay = 2.0
        terminal = {"Success", "Cancelled", "TimedOut", "Failed"}
        last = None
        while time.time() < deadline:
            try:
                inv = self.ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
            except Exception as exc:  # InvocationDoesNotExist right after send
                if "InvocationDoesNotExist" in str(type(exc).__name__) or \
                        "InvocationDoesNotExist" in str(exc):
                    time.sleep(delay)
                    continue
                raise
            last = inv["Status"]
            if last in terminal:
                return inv
            time.sleep(delay)
            delay = min(delay * 1.5, 30.0)   # bounded backoff, cap 30s
        raise TimeoutError(
            "SSM command %s on %s did not finish within %ss (last=%s)"
            % (command_id, instance_id, timeout_s, last)
        )

    def download_object(self, bucket, key, dest_path):
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        self.s3.download_file(bucket, key, dest_path)

    def put_object(self, bucket, key, body_bytes):
        self.s3.put_object(Bucket=bucket, Key=key, Body=body_bytes)

    def account_id(self, session_kind="logging"):
        sess = self._logging if session_kind == "logging" else self._sender
        return sess.client("sts").get_caller_identity()["Account"]


# ---------------------------------------------------------------------------
# Ansible invocation
# ---------------------------------------------------------------------------
def _ssm_plugin_extra_vars():
    """Extra-vars needed so the aws_ssm connection plugin can find its binary.

    All hosts are SSM-only (PLAN §4.2), so every playbook the orchestrator drives
    connects via the aws_ssm plugin, which shells out to `session-manager-plugin`.
    That binary's path is a CONTROL-NODE-LOCAL concern (not infra state), so it is
    NOT in group_vars/inventory; the operator supplies it. Interactive/manual runs
    pass `-e ansible_aws_ssm_plugin=<path>` on the CLI (see HANDOFF env recipe), but
    run_matrix shells out to configure-scenario.yml / assert-clocks.yml itself and
    must forward the same override or the plugin defaults to
    /usr/local/bin/session-manager-plugin — absent on many control nodes, which
    fails every SSM task with "failed to find the executable specified".

    Resolution order: $LLT_SSM_PLUGIN, else `session-manager-plugin` on $PATH
    (its resolved absolute path). Returns {} if neither is available, leaving the
    plugin's own default in place (so behavior is unchanged where the binary IS at
    the default path).
    """
    plugin = os.environ.get("LLT_SSM_PLUGIN") or shutil.which("session-manager-plugin")
    return {"ansible_aws_ssm_plugin": plugin} if plugin else {}


def run_ansible(playbook, extra_vars, dry_run):
    """Invoke ansible-playbook with -e JSON extra-vars. Returns True on success."""
    merged_vars = dict(extra_vars)
    merged_vars.update(_ssm_plugin_extra_vars())
    cmd = [
        "ansible-playbook",
        playbook,
        "-e", json.dumps(merged_vars),
    ]
    if dry_run:
        print("    [dry-run] would run: %s" % " ".join(cmd))
        return True
    print("    running: ansible-playbook %s" % os.path.basename(playbook))
    proc = subprocess.run(cmd, cwd=ANSIBLE_DIR)
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Per-cell execution
# ---------------------------------------------------------------------------
def execute_cell(cell, aws, args, acct_logging):
    """Run one full cell. Returns a run-manifest dict. Raises on abort conditions."""
    run_id = cell.new_run_id()
    d = cell.spec["defaults"]
    print("\n=== CELL %s -> run_id %s ===" % (cell.run_id_prefix, run_id))

    # Resolve concrete bucket names with the logging account suffix (PLAN §4.3).
    # These are for run_matrix's OWN S3 work (manifest collection, evidence
    # upload) and the run manifest — they are NOT passed to Ansible: the ansible
    # side selects landing bucket/endpoint per cell from generated_infra.yml,
    # keyed on the `aggregator` var.
    landing_bucket = "%s-%s" % (cell.spec["aggregator"]["landing_bucket"], acct_logging)
    final_bucket = "%s-%s" % (cell.spec["final_bucket"], acct_logging)
    artifacts_bucket = "%s-%s" % (cell.spec["artifacts_bucket"], acct_logging)

    # --- Extra-vars for configure-scenario.yml ------------------------------------
    # Contract with the ansible side (highest-precedence extra-vars):
    #   run_id/scenario/agent/aggregator/volume — cell identity (validated by the
    #     playbook, used to select which stack to re-template).
    #   eventgen_* — consumed by the event-generator role's generator.json.j2
    #     (the single generator param contract; the service reads
    #     `eventgen.py --config .../generator.json`).
    common_vars = {
        "run_id": run_id,
        "scenario": cell.scenario,
        "agent": cell.agent,
        "aggregator": cell.aggregator,
        "volume": cell.volume,
        "eventgen_eps": cell.spec["eps"],
        "eventgen_warmup_seconds": d["warmup_secs"],
        "eventgen_duration_seconds": d["measurement_secs"],
    }

    # --- Phase 1: render scenario config -----------------------------------------
    print("  [1/6] configure scenario")
    if not run_ansible(PB_CONFIGURE, common_vars, args.dry_run):
        raise RuntimeError("configure-scenario failed for %s" % run_id)

    # --- Phase 2: assert clocks (ABORT cell on failure, PLAN §5.3) ---------------
    # Pass the cell's `agent` so assert-clocks can PARTICIPANT-SCOPE the Windows
    # clock gate: win-vec (the only gated Windows host) participates ONLY in
    # agent=vec cells; it stamps nothing in agent=ce cells and so must not gate
    # them. See assert-clocks.yml's Windows play `hosts` pattern.
    print("  [2/6] assert clock sync")
    if not run_ansible(PB_ASSERT_CLOCKS, {"run_id": run_id, "agent": cell.agent},
                       args.dry_run):
        raise RuntimeError("clock assertion FAILED for %s — aborting cell" % run_id)

    # --- Phase 3: start generators on both hosts via SSM -------------------------
    # Run params were already written to generator.json by configure-scenario
    # (phase 1) — the start command only starts the service.
    print("  [3/6] start generators (linux + windows)")
    started = _start_generators(cell, aws, run_id, args.dry_run)

    # --- Phase 4: wait warmup + measurement + drain ------------------------------
    total_wait = d["warmup_secs"] + d["measurement_secs"] + args.drain_secs
    print("  [4/6] wait %ss (warmup %s + measure %s + drain %s)"
          % (total_wait, d["warmup_secs"], d["measurement_secs"], args.drain_secs))
    if not args.dry_run:
        time.sleep(total_wait)
    else:
        print("    [dry-run] skipping sleep")

    # --- Phase 5: stop generators + collect manifests ----------------------------
    print("  [5/6] stop generators + collect manifests")
    manifests = _stop_and_collect(cell, aws, run_id, artifacts_bucket,
                                  started, args.dry_run)

    # --- Phase 6: write run-manifest to evidence + artifacts bucket --------------
    print("  [6/6] write run manifest")
    run_manifest = {
        "run_id": run_id,
        "run_id_prefix": cell.run_id_prefix,
        "scenario": cell.scenario,
        "agent": cell.agent,
        "aggregator": cell.aggregator,
        "aggregator_stack": cell.stack,
        "volume": cell.volume,
        "eps_per_host": cell.spec["eps"],
        "landing_bucket": landing_bucket,
        "final_bucket": final_bucket,
        "artifacts_bucket": artifacts_bucket,
        "hosts": cell.spec["host_pair"],
        "scenario_topology": cell.spec["scenario_def"],
        # Methodology constants (PLAN §4.5) recorded as run evidence. The values
        # live in scenarios.yaml defaults and are mirrored by the Ansible role
        # defaults — they are constants, not per-run extra-vars.
        "batch_constants": {
            "s3_timeout_secs": d["s3_batch_timeout_secs"],
            "s3_max_bytes": d["s3_batch_max_bytes"],
            "http_timeout_secs": d["http_batch_timeout_secs"],
        },
        "timing": {
            "warmup_secs": d["warmup_secs"],
            "measurement_secs": d["measurement_secs"],
            "drain_secs": args.drain_secs,
        },
        "generator_manifests": manifests,
        "started_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dry_run": args.dry_run,
    }
    _write_run_manifest(aws, run_manifest, artifacts_bucket, args.dry_run)
    return run_manifest


def _start_generators(cell, aws, run_id, dry_run):
    """Start the generator on both hosts of the pair via SSM. Returns per-host info.

    The ONLY run-parameter contract is generator.json, re-templated per cell by
    configure-scenario.yml (phase 1). The systemd unit / NSSM service invokes
    `eventgen.py --config .../generator.json`, so the start command here does
    nothing but start the service — no env files, no exported variables.

    A Failed/TimedOut start is a hard error: continuing would produce a
    "completed" cell with zero data, which is worse than an aborted one.
    """
    started = {}
    for os_name, host_id in cell.spec["host_pair"].items():
        # AWS-managed run-command docs; the start command is OS-specific.
        if os_name == "linux":
            document = "AWS-RunShellScript"
            start_cmd = "sudo systemctl start llt-eventgen.service"
        else:
            document = "AWS-RunPowerShellScript"
            start_cmd = "Start-Service -Name 'llt-eventgen'"
        parameters = {"commands": [start_cmd]}

        if dry_run:
            print("    [dry-run] would SSM-start %s (%s) doc=%s cmd=%r"
                  % (host_id, os_name, document, start_cmd))
            started[os_name] = {"host_id": host_id, "instance_id": None,
                                "command_id": None}
            continue

        instance_id = aws.resolve_instance_id(host_id)
        command_id = aws.send_command(
            instance_id, document, parameters,
            comment="llt start gen %s %s" % (run_id, os_name),
        )
        # Wait for the *start* command to return (fast; it launches a background
        # service/unit and exits — it does NOT block for the run duration) and
        # REQUIRE Success: a failed start means no generator, hence no data.
        inv = aws.wait_command(command_id, instance_id, timeout_s=300)
        if inv["Status"] != "Success":
            raise RuntimeError(
                "generator start FAILED on %s (%s): SSM status=%s stderr=%r"
                % (host_id, os_name, inv["Status"],
                   (inv.get("StandardErrorContent") or "")[:400]))
        started[os_name] = {"host_id": host_id, "instance_id": instance_id,
                            "command_id": command_id}
        print("    started %s (%s) instance=%s" % (host_id, os_name, instance_id))
    return started


# Local generator manifest paths (event-generator role defaults: output_path +
# ".manifest.json" — see ansible/roles/event-generator/defaults/main.yml).
_LINUX_MANIFEST_PATH = "/opt/llt/generator/out/events.ndjson.manifest.json"
_WIN_MANIFEST_PATH = r"C:\llt\generator\out\events.ndjson.manifest.json"


def _stop_and_collect(cell, aws, run_id, artifacts_bucket, started, dry_run):
    """Stop generators (defensive; they self-terminate after duration), upload the
    local generator manifest from each host, and pull the manifests back down.

    The generator writes <output_path>.manifest.json locally (including on
    SIGTERM early-stop). The manifest carries the sent-event counts that are the
    LOSS-RATE denominator (PLAN §5.2 — a §5.2 deliverable), so this step uploads
    it to s3://<artifacts_bucket>/gen-manifests/{run_id}/{host_os}.manifest.json
    via the same SSM command that stops the service:
      * Linux: awscli v2 ships on AL2023 -> `aws s3 cp`.
      * Windows: AWS Tools for PowerShell ship on Amazon WS2022 AMIs ->
        `Write-S3Object`.
    Stop/upload failures are warnings, not aborts — the fetch below already
    tolerates absence and records the error in the run manifest.
    """
    manifests = {}
    for os_name, info in started.items():
        host_id = info["host_id"]
        key = "gen-manifests/%s/%s.manifest.json" % (run_id, os_name)
        # Best-effort stop (idempotent — service may already have exited), then
        # upload the local manifest in the same SSM command.
        if not dry_run and info["instance_id"]:
            try:
                if os_name == "linux":
                    doc = "AWS-RunShellScript"
                    commands = [
                        "sudo systemctl stop llt-eventgen.service || true",
                        "aws s3 cp %s s3://%s/%s"
                        % (_LINUX_MANIFEST_PATH, artifacts_bucket, key),
                    ]
                else:
                    doc = "AWS-RunPowerShellScript"
                    commands = [
                        "Stop-Service -Name 'llt-eventgen' -ErrorAction SilentlyContinue",
                        "Write-S3Object -BucketName %s -Key %s -File %s"
                        % (artifacts_bucket, key, _WIN_MANIFEST_PATH),
                    ]
                cid = aws.send_command(info["instance_id"], doc,
                                       {"commands": commands},
                                       comment="llt stop gen %s" % run_id)
                inv = aws.wait_command(cid, info["instance_id"], timeout_s=180)
                if inv["Status"] != "Success":
                    print("    warn: stop/manifest-upload on %s status=%s stderr=%r"
                          % (host_id, inv["Status"],
                             (inv.get("StandardErrorContent") or "")[:400]))
            except Exception as exc:  # non-fatal — record and continue
                print("    warn: stop/manifest-upload on %s failed: %s"
                      % (host_id, exc))

        if dry_run:
            print("    [dry-run] would SSM stop %s + upload manifest, then fetch "
                  "s3://%s/%s" % (host_id, artifacts_bucket, key))
            manifests[os_name] = {"s3_key": key, "fetched": False}
            continue
        dest = os.path.join(EVIDENCE_DIR, run_id, "%s.manifest.json" % os_name)
        try:
            aws.download_object(artifacts_bucket, key, dest)
            with open(dest, "r", encoding="utf-8") as fh:
                manifests[os_name] = json.load(fh)
        except Exception as exc:
            print("    warn: could not fetch manifest for %s: %s" % (os_name, exc))
            manifests[os_name] = {"s3_key": key, "fetched": False, "error": str(exc)}
    return manifests


def _write_run_manifest(aws, run_manifest, artifacts_bucket, dry_run):
    """Write the run manifest to report/evidence/ and to the artifacts bucket."""
    run_id = run_manifest["run_id"]
    if dry_run:
        # Dry-run must not touch the filesystem or AWS; just show where it would go.
        print("    [dry-run] would write %s"
              % os.path.join(EVIDENCE_DIR, run_id, "run-manifest.json"))
        return
    local_dir = os.path.join(EVIDENCE_DIR, run_id)
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, "run-manifest.json")
    body = json.dumps(run_manifest, indent=2, sort_keys=True).encode("utf-8")
    with open(local_path, "wb") as fh:
        fh.write(body)
    print("    wrote %s" % local_path)
    if not dry_run:
        key = "run-manifests/%s/run-manifest.json" % run_id
        aws.put_object(artifacts_bucket, key, body)
        print("    uploaded s3://%s/%s" % (artifacts_bucket, key))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
# Concurrency-safety rule for --parallel-stacks (see module docstring): two
# cells may run at the same time IFF they differ in BOTH agent AND aggregator.
#   * same agent  -> same generator host pair -> configure-scenario would
#     re-template generator.json/agent config under a live run;
#   * same aggregator -> same aggregator stack -> re-templating tier configs
#     restarts services carrying the other cell's traffic.
# The 2x2 agent x aggregator space therefore yields exactly two phases of two
# concurrency-compatible classes each, with a hard barrier between phases:
_PARALLEL_PHASES = [
    ((("vec", "vagg")), (("ce", "cs"))),   # phase 1: disjoint in agent AND agg
    ((("vec", "cs")), (("ce", "vagg"))),   # phase 2: the cross pairings
]


def _partition_two_phase(cells):
    """Split cells into the two-phase / two-lane schedule.

    Returns (phases, leftover) where phases is a list of (lane1_cells,
    lane2_cells) tuples. Order WITHIN a lane preserves the input cell order
    (scenario-major, then volume — the expand_matrix product order). leftover
    holds any cell with unrecognized tokens (defensive; run serially at the end
    rather than guessing what it may conflict with).
    """
    by_class = {}
    leftover = []
    known = {cls for phase in _PARALLEL_PHASES for cls in phase}
    for cell in cells:
        key = (cell.agent, cell.aggregator)
        if key in known:
            by_class.setdefault(key, []).append(cell)
        else:
            leftover.append(cell)
    phases = [
        (by_class.get(lane1, []), by_class.get(lane2, []))
        for lane1, lane2 in _PARALLEL_PHASES
    ]
    return phases, leftover


def run_all(cells, aws, args, acct_logging, state, state_path):
    """Execute cells serially, or via the two-phase parallel schedule."""
    # One lock serializes all state mutation + persistence across lanes: both
    # lanes share the `state` dict, and unsynchronized update+save would race
    # (dict-changed-during-iteration in json.dump / clobbered .tmp file).
    state_lock = threading.Lock()
    if args.parallel_stacks:
        phases, leftover = _partition_two_phase(cells)
        for phase_no, (lane1, lane2) in enumerate(phases, start=1):
            print("\n--- parallel phase %d/%d: lane1=%d cell(s) %s | "
                  "lane2=%d cell(s) %s ---"
                  % (phase_no, len(phases),
                     len(lane1), "(%s-%s)" % _PARALLEL_PHASES[phase_no - 1][0],
                     len(lane2), "(%s-%s)" % _PARALLEL_PHASES[phase_no - 1][1]))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                futs = []
                if lane1:
                    futs.append(ex.submit(
                        _run_serial, lane1, aws, args, acct_logging,
                        state, state_path, "p%d-lane1" % phase_no, state_lock))
                if lane2:
                    futs.append(ex.submit(
                        _run_serial, lane2, aws, args, acct_logging,
                        state, state_path, "p%d-lane2" % phase_no, state_lock))
                for f in concurrent.futures.as_completed(futs):
                    f.result()  # re-raise any worker exception
            # HARD BARRIER: the `with` block joins both lanes before the next
            # phase starts — phase-2 classes share hosts/stacks with phase 1.
        if leftover:
            print("\n--- %d cell(s) with unrecognized agent/aggregator tokens: "
                  "running serially ---" % len(leftover))
            _run_serial(leftover, aws, args, acct_logging, state, state_path,
                        "leftover", state_lock)
    else:
        _run_serial(cells, aws, args, acct_logging, state, state_path, "all",
                    state_lock)


def _run_serial(cells, aws, args, acct_logging, state, state_path, label,
                state_lock):
    for cell in cells:
        # Completed-cell skip applies to live runs only: dry-run always shows the
        # full plan (deterministic 48-cell output regardless of any state file).
        if (not args.dry_run and cell.run_id_prefix in state["completed"]
                and not args.rerun):
            print("[%s] SKIP %s (already completed; --rerun to force)"
                  % (label, cell.run_id_prefix))
            continue
        try:
            manifest = execute_cell(cell, aws, args, acct_logging)
            if not args.dry_run:
                with state_lock:
                    state["completed"][cell.run_id_prefix] = {
                        "run_id": manifest["run_id"],
                        "at": manifest["started_at_utc"],
                    }
                    save_state(state_path, state)
        except Exception as exc:
            print("[%s] CELL FAILED %s: %s" % (label, cell.run_id_prefix, exc))
            if args.stop_on_error:
                raise
            # otherwise continue to next cell (partial-matrix resilience)


def build_parser():
    p = argparse.ArgumentParser(
        prog="run_matrix.py",
        description="Orchestrate the 48-run llt hop-latency matrix (PLAN §3/§8).",
    )
    p.add_argument("--only", nargs="*", default=None,
                   help="Glob(s) over run-ID prefixes, e.g. s2-vec-vagg-5k or "
                        "'s*-vec-*-10k'. Default: whole matrix.")
    p.add_argument("--parallel-stacks", action="store_true",
                   help="Run Vector-aggregator and Cribl-Stream cells concurrently "
                        "(disjoint infra; halves wall time — PLAN §8).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan; make NO AWS calls and no sleeps.")
    p.add_argument("--rerun", action="store_true",
                   help="Re-run cells already marked complete in the state file.")
    p.add_argument("--stop-on-error", action="store_true",
                   help="Abort the whole matrix on the first failing cell.")
    p.add_argument("--drain-secs", type=int, default=120,
                   help="Post-stop drain buffer for final S3 flush (default 120).")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-2"),
                   help="AWS region (default us-east-2 / $AWS_REGION).")
    p.add_argument("--logging-profile", default=os.environ.get("LLT_LOGGING_PROFILE",
                                                               "llt-logging"),
                   help="Named AWS profile for the logging account.")
    p.add_argument("--sender-profile", default=os.environ.get("LLT_SENDER_PROFILE",
                                                              "llt-sender"),
                   help="Named AWS profile for the sender account.")
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE,
                   help="Resumability state file path.")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    # The Ansible dynamic inventory (inventories/*.aws_ec2.yml) and the per-host
    # llt_ssm_profile compose expressions read these env vars. Export them here
    # (respecting any values the operator already exported) so every
    # ansible-playbook subprocess resolves the same profiles/region this
    # orchestrator was invoked with — one source of truth per run.
    os.environ.setdefault("LLT_AWS_PROFILE_SENDER", args.sender_profile)
    os.environ.setdefault("LLT_AWS_PROFILE_LOGGING", args.logging_profile)
    os.environ.setdefault("LLT_AWS_REGION", args.region)

    scen = _load_scenarios()
    cells = filter_cells(expand_matrix(scen), args.only)
    if not cells:
        print("No cells match --only filter; nothing to do.")
        return 0

    print("Matrix: %d cell(s) selected%s"
          % (len(cells), " (DRY-RUN)" if args.dry_run else ""))
    for c in cells:
        print("  - %s  scenario=%s agent=%s agg=%s(%s) eps=%d"
              % (c.run_id_prefix, c.scenario, c.agent, c.aggregator,
                 c.stack, c.spec["eps"]))

    state = load_state(args.state_file)

    if args.dry_run:
        # In dry-run we still exercise the full per-cell plan printout with no AWS.
        for c in cells:
            execute_cell(c, aws=None, args=args, acct_logging="<ACCT>")
        print("\nDry-run complete. %d cell(s) planned." % len(cells))
        return 0

    # Live run: construct AWS clients and resolve the logging account ID for suffixes.
    aws = Aws(args.region, args.logging_profile, args.sender_profile)
    acct_logging = aws.account_id("logging")
    print("Logging account: %s  region: %s" % (acct_logging, args.region))

    run_all(cells, aws, args, acct_logging, state, args.state_file)
    save_state(args.state_file, state)
    print("\nMatrix run complete. State: %s" % args.state_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
