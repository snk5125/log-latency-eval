#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze.py — computes per-hop latency statistics for the llt experiment (PLAN §5).

Given one or more run manifests, the final bucket, and the landing bucket(s), this:
  1. Lists final objects under final/{run_id}/, streams + gz-decodes each, parses
     NDJSON events (PLAN §5.1 schema).
  2. Joins event-embedded hop timestamps with S3 object times:
       * landing PutObject time  (agent -> landing S3, and landing S3 -> aggregator)
       * final  PutObject time   (last aggregator -> final S3, and end-to-end)
     Object times come from per-object metadata written by the sink when present
     (millisecond precision), else from S3 LastModified (documented second-precision
     caveat, PLAN §5.4.3).
  3. Derives each hop delta EXACTLY per the PLAN §5.2 table.
  4. Excludes warmup events (PLAN §3).
  5. Computes mean / p50 / p90 / p99 / max / stddev / count / loss per
     run × host_os × hop, plus batch-adjusted variants for →S3 hops (subtracting the
     configured 5 s flush, PLAN §5.4.1).
  6. Writes CSV + Markdown tables + a summary JSON into report/evidence/.

--self-test runs the delta + statistics logic against synthetic in-memory events
with known answers and asserts correctness. It imports NO AWS and runs fully offline:
    python3 analyze.py --self-test

boto3 / pandas are imported lazily; the statistics core is pure stdlib so --self-test
works on a bare Python 3.9+.
"""

import argparse
import csv
import gzip
import io
import json
import math
import os
import sys

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
EVIDENCE_DIR = os.path.join(REPO_ROOT, "report", "evidence")

# Unit conversions. Event t_gen is ns; hop_ts.* are ms (tool-native, PLAN §5.1);
# S3 object times we normalize to ms. All deltas are computed in milliseconds.
NS_PER_MS = 1_000_000
S_PER_MS = 1_000

# The set of hops we can compute and the human labels/order for output tables.
# Keys mirror scenarios.yaml `hops` tokens so a run only emits hops its scenario has.
HOP_LABELS = {
    "gen_to_agent": "Generation -> agent",
    "agent_to_agg1": "Agent -> Aggregator-T1",
    "agent_to_landing": "Agent -> landing S3",
    "landing_to_agg1": "Landing S3 -> Aggregator",
    "agg1_to_agg2": "Aggregator-T1 -> T2",
    "agg_last_to_final": "Last aggregator -> final S3",
    "end_to_end": "End-to-end",
}

# Hops whose delta crosses an S3 write and therefore include the batch flush
# (PLAN §5.4.1) — reported both raw and batch-adjusted.
S3_WRITE_HOPS = {"agent_to_landing", "agg_last_to_final", "end_to_end"}


# ===========================================================================
# Core delta logic (pure, unit-tested by --self-test)
# ===========================================================================
def compute_event_deltas(event, obj_times, scenario_hops):
    """Compute per-hop deltas (ms) for a single parsed event.

    Args:
      event: dict with t_gen_ns (int ns) and hop_ts (dict of ms floats/ints):
             hop_ts.agent, hop_ts.agg1, hop_ts.agg2 as present.
      obj_times: dict with optional 'landing_ms' and 'final_ms' (epoch ms) — the
             S3 PutObject times for THIS event's landing/final objects.
      scenario_hops: ordered list of hop tokens the scenario defines (from the run
             manifest / scenarios.yaml) — only these are attempted.

    Returns: dict hop_token -> delta_ms (float). Missing inputs -> hop omitted
    (counts toward that hop's loss, handled by the caller).

    Derivations are EXACTLY PLAN §5.2:
      gen_to_agent       = hop_ts.agent - t_gen
      agent_to_agg1      = hop_ts.agg1  - hop_ts.agent          (S1/S2)
      agent_to_landing   = landing_put  - hop_ts.agent          (S3/S4)
      landing_to_agg1    = hop_ts.agg1  - landing_put           (S3/S4)
      agg1_to_agg2       = hop_ts.agg2  - hop_ts.agg1           (S2/S4)
      agg_last_to_final  = final_put    - last hop_ts
      end_to_end         = final_put    - t_gen
    """
    t_gen_ms = event["t_gen_ns"] / NS_PER_MS
    hop_ts = event.get("hop_ts", {}) or {}
    agent = hop_ts.get("agent")
    agg1 = hop_ts.get("agg1")
    agg2 = hop_ts.get("agg2")
    landing = obj_times.get("landing_ms")
    final = obj_times.get("final_ms")

    # The "last hop_ts" before the final S3 write = the deepest aggregator tier the
    # event reached (agg2 if the scenario has two tiers and it is present, else agg1).
    last_hop_ts = agg2 if (agg2 is not None) else agg1

    out = {}

    scenario_hops_set = set(scenario_hops)

    def put(token, value):
        # Guard: only record if all operands were present (value is not None) and
        # the hop belongs to this scenario. `end_to_end` is always reported (it is
        # not listed per-scenario but is universally derivable once final exists).
        if value is None:
            return
        if token == "end_to_end" or token in scenario_hops_set:
            out[token] = float(value)

    # gen_to_agent
    if agent is not None:
        put("gen_to_agent", agent - t_gen_ms)

    # agent_to_agg1  (direct agent->aggregator path, S1/S2)
    if agent is not None and agg1 is not None:
        put("agent_to_agg1", agg1 - agent)

    # agent_to_landing (S3/S4)
    if agent is not None and landing is not None:
        put("agent_to_landing", landing - agent)

    # landing_to_agg1 (S3/S4)
    if landing is not None and agg1 is not None:
        put("landing_to_agg1", agg1 - landing)

    # agg1_to_agg2 (S2/S4)
    if agg1 is not None and agg2 is not None:
        put("agg1_to_agg2", agg2 - agg1)

    # agg_last_to_final
    if last_hop_ts is not None and final is not None:
        put("agg_last_to_final", final - last_hop_ts)

    # end_to_end
    if final is not None:
        put("end_to_end", final - t_gen_ms)

    return out


# ===========================================================================
# Statistics (pure, unit-tested)
# ===========================================================================
def percentile(sorted_vals, q):
    """Linear-interpolation percentile (q in [0,1]) over a pre-sorted list.

    Matches the common 'linear'/type-7 method (numpy default) so results are
    reproducible without numpy. sorted_vals must be non-empty and ascending.
    """
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def summarize(values):
    """Return the stats dict for a list of delta values (ms).

    Keys: count, mean, p50, p90, p99, max, stddev (population). Empty -> zeros/None.
    """
    n = len(values)
    if n == 0:
        return {"count": 0, "mean": None, "p50": None, "p90": None,
                "p99": None, "max": None, "stddev": None}
    s = sorted(values)
    mean = sum(s) / n
    if n > 1:
        var = sum((x - mean) ** 2 for x in s) / n   # population variance
        stddev = math.sqrt(var)
    else:
        stddev = 0.0
    return {
        "count": n,
        "mean": mean,
        "p50": percentile(s, 0.50),
        "p90": percentile(s, 0.90),
        "p99": percentile(s, 0.99),
        "max": s[-1],
        "stddev": stddev,
    }


def batch_adjust(stats, flush_ms):
    """Return a copy of stats with the fixed S3 flush subtracted (PLAN §5.4.1).

    Applied to mean/p50/p90/p99/max; count/stddev unchanged (subtracting a constant
    shifts location, not spread). Values are floored at 0 (a negative adjusted delta
    would indicate the flush model over-corrects for that observation).
    """
    if stats["count"] == 0:
        return dict(stats)
    adj = dict(stats)
    for k in ("mean", "p50", "p90", "p99", "max"):
        if adj[k] is not None:
            adj[k] = max(0.0, adj[k] - flush_ms)
    return adj


# ===========================================================================
# Aggregation across events -> per (run, host_os, hop) stats
# ===========================================================================
def aggregate_run(events, obj_time_index, scenario_hops, run_id,
                  flush_ms, sent_counts):
    """Aggregate parsed measurement events into per-(host_os, hop) statistics.

    Args:
      events: iterable of parsed event dicts (WARMUP ALREADY EXCLUDED by caller).
      obj_time_index: callable(event) -> {'landing_ms':..,'final_ms':..} providing
             the S3 object times for that event (see build_obj_time_index).
      scenario_hops: list of hop tokens for this run's scenario.
      run_id: run identifier.
      flush_ms: S3 batch flush to subtract for batch-adjusted variants.
      sent_counts: dict host_os -> number of measurement events SENT (from generator
             manifests) for loss-rate computation (PLAN §5.2 loss rate).

    Returns: list of row dicts, one per (host_os, hop), each with raw + adjusted stats.
    """
    # Collect delta samples: per_os_hop[host_os][hop] -> [delta_ms, ...]
    per = {}
    landed_seqs = {}   # host_os -> set of seq that reached final (for loss)
    for ev in events:
        host_os = ev.get("host_os", "unknown")
        obj_times = obj_time_index(ev)
        deltas = compute_event_deltas(ev, obj_times, scenario_hops)
        os_bucket = per.setdefault(host_os, {})
        for hop, dv in deltas.items():
            os_bucket.setdefault(hop, []).append(dv)
        # An event "landed" if it produced an end_to_end delta (reached final S3).
        if "end_to_end" in deltas:
            landed_seqs.setdefault(host_os, set()).add(ev.get("seq"))

    rows = []
    # Preserve the scenario hop order, then append end_to_end (always reported).
    ordered = list(scenario_hops)
    if "end_to_end" not in ordered:
        ordered.append("end_to_end")

    for host_os in sorted(per.keys()):
        sent = sent_counts.get(host_os)
        landed = len(landed_seqs.get(host_os, set()))
        loss_rate = None
        if sent:
            loss_rate = max(0.0, (sent - landed) / float(sent))
        for hop in ordered:
            vals = per.get(host_os, {}).get(hop, [])
            raw = summarize(vals)
            row = {
                "run_id": run_id,
                "host_os": host_os,
                "hop": hop,
                "hop_label": HOP_LABELS.get(hop, hop),
                "sent_events": sent,
                "landed_events": landed,
                "loss_rate": loss_rate,
            }
            for k, v in raw.items():
                row[k] = v
            # Batch-adjusted variant only meaningful for S3-write hops.
            if hop in S3_WRITE_HOPS:
                adj = batch_adjust(raw, flush_ms)
                for k in ("mean", "p50", "p90", "p99", "max"):
                    row["adj_" + k] = adj[k]
            else:
                for k in ("mean", "p50", "p90", "p99", "max"):
                    row["adj_" + k] = None
            rows.append(row)
    return rows


# ===========================================================================
# I/O — S3 listing / streaming / parsing (lazy boto3)
# ===========================================================================
def _s3_client(profile, region):
    import boto3  # noqa: WPS433
    sess = boto3.Session(profile_name=profile, region_name=region)
    return sess.client("s3")


def iter_final_events(s3, final_bucket, run_id):
    """Yield parsed events from all objects under final/{run_id}/ (gz-aware, streaming).

    Also yields, alongside each event, the FINAL object's put-time in ms so the
    caller can index it. We prefer object metadata key 'x-amz-meta-llt-put-ms' when
    the sink wrote it (ms precision), else fall back to LastModified (second
    precision — PLAN §5.4.3 caveat).
    """
    paginator = s3.get_paginator("list_objects_v2")
    prefix = "final/%s/" % run_id
    for page in paginator.paginate(Bucket=final_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            head = s3.head_object(Bucket=final_bucket, Key=key)
            final_ms = _object_put_ms(head, obj)
            body = s3.get_object(Bucket=final_bucket, Key=key)["Body"]
            for ev in _iter_ndjson(body, key):
                yield ev, final_ms


def _object_put_ms(head_or_meta, list_entry):
    """Return an epoch-ms put time for an object, preferring ms metadata."""
    meta = (head_or_meta or {}).get("Metadata", {})
    if "llt-put-ms" in meta:
        try:
            return int(meta["llt-put-ms"])
        except (TypeError, ValueError):
            pass
    # Fall back to LastModified (datetime, second precision).
    lm = (head_or_meta or {}).get("LastModified") or (list_entry or {}).get("LastModified")
    if lm is not None:
        return int(lm.timestamp() * S_PER_MS)
    return None


def _iter_ndjson(body, key):
    """Stream NDJSON from an S3 StreamingBody, transparently gunzipping .gz keys."""
    stream = body
    if key.endswith(".gz"):
        stream = gzip.GzipFile(fileobj=body)
    text = io.TextIOWrapper(stream, encoding="utf-8")
    for line in text:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue  # skip malformed line, do not abort the run


def build_landing_index(s3, landing_bucket, run_id):
    """Build a seq -> landing-put-ms index for S3/S4 scenarios.

    Landing objects are written by the agent; we key by the event `seq`. When the
    sink stamps per-object metadata 'x-amz-meta-llt-seq' + 'x-amz-meta-llt-put-ms'
    we use those (ms). Otherwise we must parse the landing objects and use the
    object LastModified for every seq inside (second precision — documented caveat).
    Returns dict seq -> landing_ms.
    """
    index = {}
    paginator = s3.get_paginator("list_objects_v2")
    prefix = "landing/%s/" % run_id
    for page in paginator.paginate(Bucket=landing_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            head = s3.head_object(Bucket=landing_bucket, Key=key)
            put_ms = _object_put_ms(head, obj)
            meta = head.get("Metadata", {})
            if "llt-seq" in meta:
                try:
                    index[int(meta["llt-seq"])] = put_ms
                    continue
                except (TypeError, ValueError):
                    pass
            # No per-object seq metadata: assign this object's put time to every
            # seq contained in it (second-precision caveat applies).
            body = s3.get_object(Bucket=landing_bucket, Key=key)["Body"]
            for ev in _iter_ndjson(body, key):
                index[ev.get("seq")] = put_ms
    return index


def build_obj_time_index(final_ms_by_seq, landing_ms_by_seq):
    """Return a callable(event) -> {'final_ms':.., 'landing_ms':..} using seq lookup."""
    def _lookup(ev):
        seq = ev.get("seq")
        return {
            "final_ms": final_ms_by_seq.get(seq),
            "landing_ms": landing_ms_by_seq.get(seq),
        }
    return _lookup


# ===========================================================================
# Output writers
# ===========================================================================
_CSV_FIELDS = [
    "run_id", "host_os", "hop", "hop_label", "count",
    "mean", "p50", "p90", "p99", "max", "stddev",
    "adj_mean", "adj_p50", "adj_p90", "adj_p99", "adj_max",
    "sent_events", "landed_events", "loss_rate",
]


def write_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return "%.3f" % v
    return str(v)


def write_markdown(rows, path, run_ids):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    lines.append("# llt Per-Hop Latency Statistics")
    lines.append("")
    lines.append("Runs: %s" % ", ".join(sorted(set(run_ids))))
    lines.append("")
    lines.append("All latencies in milliseconds. `adj_*` columns subtract the "
                 "configured S3 batch flush for S3-write hops (PLAN §5.4.1). "
                 "Warmup events excluded.")
    lines.append("")
    header = ("| run_id | host_os | hop | count | mean | p50 | p90 | p99 | max | "
              "stddev | adj_mean | adj_p99 | loss |")
    sep = "|" + "---|" * 13
    lines.append(header)
    lines.append(sep)
    for r in rows:
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (r["run_id"], r["host_os"], r["hop_label"], _fmt(r["count"]),
               _fmt(r["mean"]), _fmt(r["p50"]), _fmt(r["p90"]), _fmt(r["p99"]),
               _fmt(r["max"]), _fmt(r["stddev"]), _fmt(r.get("adj_mean")),
               _fmt(r.get("adj_p99")), _fmt(r["loss_rate"]))
        )
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def write_summary_json(rows, path, meta):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"meta": meta, "rows": rows}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, default=str)
    os.replace(tmp, path)


# ===========================================================================
# Live analysis driver
# ===========================================================================
def analyze_run(s3, run_manifest, flush_ms, out_dir):
    """Analyze a single run given its manifest; return the stat rows."""
    run_id = run_manifest["run_id"]
    final_bucket = run_manifest["final_bucket"]
    landing_bucket = run_manifest.get("landing_bucket")
    scenario_hops = run_manifest.get("scenario_topology", {}).get("hops", [])
    uses_landing = run_manifest.get("scenario_topology", {}).get("uses_landing", False)

    # Sent counts (measurement-only) from generator manifests, for loss rate.
    sent_counts = {}
    for os_name, gm in (run_manifest.get("generator_manifests") or {}).items():
        if isinstance(gm, dict) and "measurement_events" in gm:
            sent_counts[os_name] = gm["measurement_events"]

    # Landing index only if the scenario writes to landing (S3/S4).
    landing_ms_by_seq = {}
    if uses_landing and landing_bucket:
        print("  building landing index from %s ..." % landing_bucket)
        landing_ms_by_seq = build_landing_index(s3, landing_bucket, run_id)

    # Stream final events; build final-put index by seq and buffer events. Final
    # objects for a 10-min run at 10k EPS are large, so we buffer parsed events
    # (dict-per-event) but drop the heavy 'pad' field to bound memory.
    final_ms_by_seq = {}
    events = []
    print("  streaming final objects from %s ..." % final_bucket)
    for ev, final_ms in iter_final_events(s3, final_bucket, run_id):
        seq = ev.get("seq")
        final_ms_by_seq[seq] = final_ms
        ev.pop("pad", None)   # discard padding to reduce memory footprint
        # Exclude warmup events (PLAN §3) at ingest.
        if ev.get("warmup") is True:
            continue
        events.append(ev)

    obj_index = build_obj_time_index(final_ms_by_seq, landing_ms_by_seq)
    rows = aggregate_run(events, obj_index, scenario_hops, run_id,
                         flush_ms, sent_counts)
    return rows


def load_run_manifests(paths):
    """Load run-manifest JSON files (explicit paths or a directory of them)."""
    manifests = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name == "run-manifest.json":
                    with open(os.path.join(p, name), "r", encoding="utf-8") as fh:
                        manifests.append(json.load(fh))
            # also look one level down (evidence/<run_id>/run-manifest.json)
            for entry in sorted(os.listdir(p)):
                sub = os.path.join(p, entry, "run-manifest.json")
                if os.path.isfile(sub):
                    with open(sub, "r", encoding="utf-8") as fh:
                        manifests.append(json.load(fh))
        else:
            with open(p, "r", encoding="utf-8") as fh:
                manifests.append(json.load(fh))
    return manifests


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.self_test:
        return run_self_test()

    manifests = load_run_manifests(args.manifest)
    if not manifests:
        sys.stderr.write("analyze: no run manifests found\n")
        return 2

    s3 = _s3_client(args.logging_profile, args.region)
    flush_ms = args.s3_flush_secs * S_PER_MS
    all_rows = []
    run_ids = []
    for man in manifests:
        print("Analyzing run %s ..." % man["run_id"])
        rows = analyze_run(s3, man, flush_ms, args.out_dir)
        all_rows.extend(rows)
        run_ids.append(man["run_id"])

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, "latency_stats.csv")
    md_path = os.path.join(args.out_dir, "latency_stats.md")
    json_path = os.path.join(args.out_dir, "latency_summary.json")
    write_csv(all_rows, csv_path)
    write_markdown(all_rows, md_path, run_ids)
    write_summary_json(all_rows, json_path,
                       {"runs": run_ids, "s3_flush_secs": args.s3_flush_secs})
    print("Wrote:\n  %s\n  %s\n  %s" % (csv_path, md_path, json_path))
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="analyze.py",
        description="Per-hop latency stats for the llt experiment (PLAN §5).",
    )
    p.add_argument("--manifest", nargs="*", default=[EVIDENCE_DIR],
                   help="Run-manifest JSON file(s) or directory. Default: "
                        "report/evidence/ (recurses one level).")
    p.add_argument("--out-dir", default=EVIDENCE_DIR,
                   help="Output directory for CSV/MD/JSON. Default: report/evidence/.")
    p.add_argument("--s3-flush-secs", type=float, default=5.0,
                   help="Configured S3 batch flush to subtract for batch-adjusted "
                        "variants (PLAN §4.5 / §5.4.1). Default 5.")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-2"),
                   help="AWS region.")
    p.add_argument("--logging-profile",
                   default=os.environ.get("LLT_LOGGING_PROFILE", "llt-logging"),
                   help="Named AWS profile for the logging account (owns buckets).")
    p.add_argument("--self-test", action="store_true",
                   help="Run offline unit tests of the delta + stats logic and exit.")
    return p


# ===========================================================================
# SELF-TEST (unit test) — offline, stdlib-only, known-answer assertions
# ===========================================================================
def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def run_self_test():
    """Assert the delta and statistics logic against hand-computed known answers."""
    failures = []

    def check(name, cond):
        if cond:
            print("  PASS: %s" % name)
        else:
            print("  FAIL: %s" % name)
            failures.append(name)

    # --- Test 1: S2 event (Host -> agent -> T1 -> T2 -> final), no landing --------
    # Times chosen so every delta is a round number.
    #   t_gen   = 1000 ms  (as ns: 1000 * 1e6)
    #   agent   = 1005 ms  -> gen_to_agent = 5
    #   agg1    = 1020 ms  -> agent_to_agg1 = 15
    #   agg2    = 1035 ms  -> agg1_to_agg2  = 15
    #   final   = 6035 ms  -> agg_last_to_final = 5000 (includes 5s flush)
    #                         end_to_end        = 5035
    ev_s2 = {
        "seq": 1, "host_os": "linux", "warmup": False,
        "t_gen_ns": 1000 * NS_PER_MS,
        "hop_ts": {"agent": 1005.0, "agg1": 1020.0, "agg2": 1035.0},
    }
    s2_hops = ["gen_to_agent", "agent_to_agg1", "agg1_to_agg2", "agg_last_to_final"]
    d = compute_event_deltas(ev_s2, {"final_ms": 6035.0}, s2_hops)
    check("S2 gen_to_agent == 5", _approx(d["gen_to_agent"], 5.0))
    check("S2 agent_to_agg1 == 15", _approx(d["agent_to_agg1"], 15.0))
    check("S2 agg1_to_agg2 == 15", _approx(d["agg1_to_agg2"], 15.0))
    check("S2 agg_last_to_final == 5000", _approx(d["agg_last_to_final"], 5000.0))
    check("S2 end_to_end == 5035", _approx(d["end_to_end"], 5035.0))
    check("S2 does not emit landing hops",
          "agent_to_landing" not in d and "landing_to_agg1" not in d)

    # --- Test 2: S3 event (Host -> landing S3 -> agg -> final), single tier -------
    #   t_gen   = 2000 ms
    #   agent   = 2004 ms  -> gen_to_agent = 4
    #   landing = 7004 ms  -> agent_to_landing = 5000 (flush)
    #   agg1    = 7050 ms  -> landing_to_agg1  = 46
    #   final   = 12050 ms -> agg_last_to_final = 5000, end_to_end = 10050
    ev_s3 = {
        "seq": 2, "host_os": "windows", "warmup": False,
        "t_gen_ns": 2000 * NS_PER_MS,
        "hop_ts": {"agent": 2004.0, "agg1": 7050.0},
    }
    s3_hops = ["gen_to_agent", "agent_to_landing", "landing_to_agg1",
               "agg_last_to_final"]
    d3 = compute_event_deltas(
        ev_s3, {"landing_ms": 7004.0, "final_ms": 12050.0}, s3_hops)
    check("S3 gen_to_agent == 4", _approx(d3["gen_to_agent"], 4.0))
    check("S3 agent_to_landing == 5000", _approx(d3["agent_to_landing"], 5000.0))
    check("S3 landing_to_agg1 == 46", _approx(d3["landing_to_agg1"], 46.0))
    check("S3 agg_last_to_final == 5000", _approx(d3["agg_last_to_final"], 5000.0))
    check("S3 end_to_end == 10050", _approx(d3["end_to_end"], 10050.0))
    check("S3 last_hop is agg1 (single tier)",
          "agg1_to_agg2" not in d3)

    # --- Test 3: missing operands -> hop omitted (loss), no crash -----------------
    ev_partial = {
        "seq": 3, "host_os": "linux", "warmup": False,
        "t_gen_ns": 3000 * NS_PER_MS,
        "hop_ts": {"agent": 3005.0},   # never reached agg / final
    }
    dp = compute_event_deltas(ev_partial, {}, s2_hops)
    check("partial event yields only gen_to_agent",
          list(dp.keys()) == ["gen_to_agent"])

    # --- Test 4: percentile known answers -----------------------------------------
    # Values 1..10 -> p50=5.5, p90=9.1, p99=9.91, max=10 (type-7 interpolation).
    vals = list(range(1, 11))
    st = summarize([float(v) for v in vals])
    check("summarize count == 10", st["count"] == 10)
    check("summarize mean == 5.5", _approx(st["mean"], 5.5))
    check("summarize p50 == 5.5", _approx(st["p50"], 5.5))
    check("summarize p90 == 9.1", _approx(st["p90"], 9.1))
    check("summarize p99 == 9.91", _approx(st["p99"], 9.91))
    check("summarize max == 10", _approx(st["max"], 10.0))
    # Population stddev of 1..10 = sqrt(8.25) ≈ 2.8722813
    check("summarize stddev ~ 2.87228", _approx(st["stddev"], math.sqrt(8.25)))

    # single-value edge case
    st1 = summarize([42.0])
    check("single value p99 == value", _approx(st1["p99"], 42.0))
    check("single value stddev == 0", _approx(st1["stddev"], 0.0))
    check("empty summarize count == 0", summarize([])["count"] == 0)

    # --- Test 5: batch adjustment subtracts the flush and floors at 0 -------------
    raw = summarize([5000.0, 5010.0, 5020.0, 5030.0, 5100.0])
    adj = batch_adjust(raw, 5000.0)
    check("batch_adjust mean subtracts 5000",
          _approx(adj["mean"], raw["mean"] - 5000.0))
    check("batch_adjust count unchanged", adj["count"] == raw["count"])
    tiny = summarize([10.0])          # smaller than flush -> floored to 0
    tiny_adj = batch_adjust(tiny, 5000.0)
    check("batch_adjust floors negative to 0", _approx(tiny_adj["mean"], 0.0))

    # --- Test 6: full aggregate incl. warmup exclusion + loss ---------------------
    # Two measurement events land; one warmup event must be excluded upstream (we
    # simulate the caller having filtered it). One measurement event is "lost"
    # (no final), giving loss = 1/3 against sent=3.
    ev_a = dict(ev_s2)                        # linux, lands
    ev_b = dict(ev_s2); ev_b["seq"] = 11      # linux, lands (identical deltas)
    ev_c = dict(ev_partial); ev_c["seq"] = 12 # linux, does NOT land
    final_by_seq = {1: 6035.0, 11: 6035.0}    # seq 12 absent -> lost
    idx = build_obj_time_index(final_by_seq, {})
    rows = aggregate_run([ev_a, ev_b, ev_c], idx, s2_hops, "s2-vec-vagg-5k-TEST",
                         flush_ms=5000.0, sent_counts={"linux": 3})
    # find the end_to_end row for linux
    e2e = [r for r in rows if r["host_os"] == "linux" and r["hop"] == "end_to_end"]
    check("aggregate produced end_to_end row", len(e2e) == 1)
    check("end_to_end count == 2 (two landed)", e2e[0]["count"] == 2)
    check("loss_rate == 1/3", _approx(e2e[0]["loss_rate"], 1.0 / 3.0))
    g2a = [r for r in rows if r["host_os"] == "linux" and r["hop"] == "gen_to_agent"]
    check("gen_to_agent count == 3 (all reached agent)", g2a[0]["count"] == 3)
    final_row = [r for r in rows
                 if r["host_os"] == "linux" and r["hop"] == "agg_last_to_final"][0]
    check("agg_last_to_final has adj (S3-write hop)",
          final_row["adj_mean"] is not None)
    check("agg_last_to_final adj mean == 0 (5000-5000)",
          _approx(final_row["adj_mean"], 0.0))

    print("")
    if failures:
        print("SELF-TEST FAILED: %d assertion(s) failed: %s"
              % (len(failures), ", ".join(failures)))
        return 1
    print("SELF-TEST PASSED: all assertions OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
