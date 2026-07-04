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
     configured 5 s flush per write actually crossed, PLAN §5.4.1).
  6. Writes CSV + Markdown tables + a summary JSON into report/evidence/.

--self-test runs the delta + statistics logic against synthetic in-memory events
with known answers and asserts correctness. It imports NO AWS and runs fully offline:
    python3 analyze.py --self-test

boto3 / pandas are imported lazily; the statistics core is pure stdlib so --self-test
works on a bare Python 3.9+.

IMPORTANT CORRECTNESS NOTES (read before modifying):
  * Linux and Windows generators both emit `seq` 0..N independently (PLAN §5.1) — a
    bare `seq` is NOT a unique key across a run. Every index in this module is keyed
    by the tuple (host_os, seq), never by bare seq, to avoid cross-OS collisions
    silently overwriting one OS's timestamps with the other's.
  * This module streams: it does not retain a list of all parsed event dicts (a
    10-min run at 10k EPS x 2 hosts is 12M+ events). Deltas are computed and folded
    into per-(host_os, hop) running float lists as each event is read; nothing
    per-event survives past that point except the small set of keys needed for
    dedup/loss bookkeeping.
"""

import argparse
import csv
import gzip
import io
import json
import math
import os
import sys
from array import array

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
# (PLAN §5.4.1) — reported both raw and batch-adjusted. `end_to_end` is handled
# specially (see batch_flush_count_for_end_to_end) because S3/S4 cross TWO S3
# writes (landing + final) while S1/S2 cross only one (final).
S3_WRITE_HOPS = {"agent_to_landing", "agg_last_to_final", "end_to_end"}

# Per-hop flush multiplier for the non-end_to_end S3-write hops: each of these
# crosses exactly one S3 PutObject, so exactly one flush is subtracted
# (PLAN §5.4.1 — "per-hop adjustments stay as-is").
_SINGLE_FLUSH_HOPS = {"agent_to_landing", "agg_last_to_final"}


# ===========================================================================
# Core delta logic (pure, unit-tested by --self-test)
# ===========================================================================
def compute_event_deltas(event, obj_times, scenario_hops, require_agg2=False):
    """Compute per-hop deltas (ms) for a single parsed event.

    Args:
      event: dict with t_gen_ns (int ns) and hop_ts (dict of ms floats/ints):
             hop_ts.agent, hop_ts.agg1, hop_ts.agg2 as present.
      obj_times: dict with optional 'landing_ms' and 'final_ms' (epoch ms) — the
             S3 PutObject times for THIS event's landing/final objects.
      scenario_hops: ordered list of hop tokens the scenario defines (from the run
             manifest / scenarios.yaml) — only these are attempted.
      require_agg2: True for two-tier scenarios (S2/S4, PLAN §2). When True, the
             final->last-hop delta ("agg_last_to_final") REQUIRES hop_ts.agg2 —
             the scenario's terminal-tier stamp. An event that reached final with
             only agent+agg1 stamped (agg2 missing — e.g. a T2 timestamp write
             that was lost/never applied) must NOT silently fall back to crediting
             agg1->final as the last-hop latency: that would count the T1->T2 hop
             as part of "last aggregator -> final" and understate the true path.
             Such events are counted separately by the caller as
             "missing_terminal_tier", not folded into agg_last_to_final.

    Returns: (deltas, missing_terminal_tier) where deltas is dict hop_token ->
    delta_ms (float) and missing_terminal_tier is True iff this event reached
    final S3 but was skipped for agg_last_to_final/end_to_end-adjacent bookkeeping
    because it lacked the scenario's required terminal-tier hop_ts. Missing
    inputs otherwise simply omit that hop (counts toward that hop's loss, handled
    by the caller).

    Derivations are EXACTLY PLAN §5.2:
      gen_to_agent       = hop_ts.agent - t_gen
      agent_to_agg1      = hop_ts.agg1  - hop_ts.agent          (S1/S2)
      agent_to_landing   = landing_put  - hop_ts.agent          (S3/S4)
      landing_to_agg1    = hop_ts.agg1  - landing_put           (S3/S4)
      agg1_to_agg2       = hop_ts.agg2  - hop_ts.agg1           (S2/S4)
      agg_last_to_final  = final_put    - last hop_ts (terminal-tier stamp)
      end_to_end         = final_put    - t_gen
    """
    t_gen_ms = event["t_gen_ns"] / NS_PER_MS
    hop_ts = event.get("hop_ts", {}) or {}
    agent = hop_ts.get("agent")
    agg1 = hop_ts.get("agg1")
    agg2 = hop_ts.get("agg2")
    landing = obj_times.get("landing_ms")
    final = obj_times.get("final_ms")

    out = {}
    missing_terminal_tier = False

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

    # agg_last_to_final — the "last hop_ts" before the final S3 write must be the
    # scenario's TERMINAL tier stamp, not merely whichever stamp happens to be
    # present. For single-tier scenarios (S1/S3) that is agg1. For two-tier
    # scenarios (S2/S4, require_agg2=True) that is agg2 — an event missing agg2
    # is under-instrumented for this hop and must be excluded, not silently
    # credited using agg1 (which would smuggle the T1->T2 delta into this hop).
    if final is not None:
        if require_agg2:
            if agg2 is not None:
                put("agg_last_to_final", final - agg2)
            else:
                missing_terminal_tier = True
        else:
            if agg1 is not None:
                put("agg_last_to_final", final - agg1)
            else:
                missing_terminal_tier = True

    # end_to_end
    if final is not None:
        put("end_to_end", final - t_gen_ms)

    return out, missing_terminal_tier


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
    """Return the stats dict for a list (or array('d')) of delta values (ms).

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


def batch_adjust(stats, flush_ms_total):
    """Return a copy of stats with the batch flush(es) actually crossed subtracted.

    `flush_ms_total` is the total flush time to subtract for THIS hop (caller
    computes it — one flush for single-S3-write hops, PLAN §5.4.1; for
    end_to_end it is flush_ms * 2 when the scenario also writes to landing
    (S3/S4), else flush_ms * 1, since end_to_end crosses every S3 write in the
    path). Applied to mean/p50/p90/p99/max; count/stddev unchanged (subtracting
    a constant shifts location, not spread). Values are floored at 0 (a negative
    adjusted delta would indicate the flush model over-corrects for that
    observation).
    """
    if stats["count"] == 0:
        return dict(stats)
    adj = dict(stats)
    for k in ("mean", "p50", "p90", "p99", "max"):
        if adj[k] is not None:
            adj[k] = max(0.0, adj[k] - flush_ms_total)
    return adj


def end_to_end_flush_ms(flush_ms, uses_landing):
    """Total S3-flush time embedded in an end_to_end delta (PLAN §5.4.1).

    S1/S2 (uses_landing=False): the event crosses exactly one S3 write (final)
      -> one flush.
    S3/S4 (uses_landing=True): the event crosses TWO S3 writes (landing AND
      final) -> two flushes. Subtracting only one (the historical behavior)
      systematically overstates the batch-adjusted end-to-end latency for S3/S4
      by one flush interval.
    """
    return flush_ms * (2.0 if uses_landing else 1.0)


# ===========================================================================
# Aggregation across events -> per (run, host_os, hop) stats
#
# STREAMING DESIGN (finding #2): this module never holds a full list of parsed
# event dicts for a run. `RunAccumulator` is fed one event at a time (from the
# final-object stream) and immediately:
#   * dedupes by (host_os, seq) — first delivery wins, later duplicates are
#     counted but do not re-enter the stats (SQS at-least-once, finding #5),
#   * looks up this event's landing time (if any) from the landing index,
#   * computes deltas inline and folds each hop's delta into a per-(host_os,
#     hop) array('d') (compact float storage — cheaper than Python list-of-float
#     for the 12M+ event scale documented in the module docstring),
#   * drops the event dict immediately after.
# Only O(distinct host_os x hops) arrays plus a couple of O(seen-seq) sets
# survive for the duration of a run's analysis — not O(events) dicts.
# ===========================================================================
class RunAccumulator(object):
    """Accumulates per-(host_os, hop) delta samples for one run, streaming.

    Also tracks, per host_os: duplicates seen, landed (unique) seqs, skipped
    (malformed/incomplete) events, and events missing the scenario's terminal
    aggregator-tier stamp (finding #7).
    """

    def __init__(self, scenario_hops, require_agg2):
        self.scenario_hops = list(scenario_hops)
        self.require_agg2 = require_agg2
        # per[host_os][hop] -> array('d') of delta_ms
        self.per = {}
        # host_os -> set of (host_os, seq) already folded into stats at all
        # (dedup key set, finding #5) — a duplicate delivery of an event that
        # never reached final (e.g. only gen_to_agent was ever derivable) must
        # STILL be deduped, so this set is broader than "landed".
        self._dedup_seq = {}
        # host_os -> set of seq that actually reached final S3 (produced an
        # end_to_end delta). This is the loss-rate numerator's complement and
        # is DELIBERATELY separate from _dedup_seq: an event can be seen/deduped
        # (e.g. it reached the agent hop) without ever landing at final.
        self._landed_seq = {}
        self.duplicates = {}            # host_os -> int
        self.skipped_events = {}        # host_os -> int (finding #4)
        self.missing_terminal_tier = {} # host_os -> int (finding #7)

    def _bucket(self, host_os):
        return self.per.setdefault(host_os, {})

    def _bump(self, counter_dict, host_os, n=1):
        counter_dict[host_os] = counter_dict.get(host_os, 0) + n

    def add_skip(self, host_os):
        self._bump(self.skipped_events, host_os or "unknown")

    def add_event(self, host_os, seq, deltas, missing_terminal_tier):
        """Fold one already-computed event's deltas into the running stats.

        Returns True if this event was newly counted (not a duplicate), False
        if it was a duplicate (seq already seen for this host_os) and therefore
        skipped for stats purposes (finding #5: first-wins dedup).
        """
        seen = self._dedup_seq.setdefault(host_os, set())
        if seq in seen:
            self._bump(self.duplicates, host_os)
            return False
        seen.add(seq)

        if "end_to_end" in deltas:
            self._landed_seq.setdefault(host_os, set()).add(seq)

        if missing_terminal_tier:
            self._bump(self.missing_terminal_tier, host_os)

        os_bucket = self._bucket(host_os)
        for hop, dv in deltas.items():
            arr = os_bucket.get(hop)
            if arr is None:
                arr = array("d")
                os_bucket[hop] = arr
            arr.append(dv)
        return True

    def landed_count(self, host_os):
        return len(self._landed_seq.get(host_os, ()))

    def host_os_values(self):
        """All host_os values with ANY activity (events, skips, or dup dupes)."""
        keys = set(self.per.keys())
        keys.update(self._dedup_seq.keys())
        keys.update(self._landed_seq.keys())
        keys.update(self.skipped_events.keys())
        keys.update(self.duplicates.keys())
        keys.update(self.missing_terminal_tier.keys())
        return keys


def _is_finite_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _validate_event(ev):
    """Graceful-degradation guard (finding #4).

    Returns an error string describing why the event is unusable, or None if it
    is well-formed enough to compute deltas from (t_gen_ns present + numeric,
    hop_ts absent/malformed tolerated as "no hop reached yet" rather than an
    error — only t_gen_ns is load-bearing for every derivation).
    """
    if not isinstance(ev, dict):
        return "event is not a JSON object"
    t_gen_ns = ev.get("t_gen_ns")
    if t_gen_ns is None:
        return "missing t_gen_ns"
    if not _is_finite_number(t_gen_ns):
        return "non-numeric/non-finite t_gen_ns"
    hop_ts = ev.get("hop_ts")
    if hop_ts is not None and not isinstance(hop_ts, dict):
        return "hop_ts is present but not an object"
    if isinstance(hop_ts, dict):
        for k, v in hop_ts.items():
            if v is not None and not _is_finite_number(v):
                return "non-numeric hop_ts.%s" % k
    if "seq" not in ev:
        return "missing seq"
    return None


def aggregate_stream(event_iter, obj_time_lookup, scenario_hops, require_agg2):
    """Stream events, dedupe + compute deltas inline, return a RunAccumulator.

    Args:
      event_iter: iterable of (event_dict, final_ms) pairs — final_ms is the
             already-resolved final-object put time for THIS event (finding #1:
             attached per-event at parse time rather than looked up from a
             shared bare-seq dict, so there is no cross-OS collision surface).
             Warmup events must already be excluded by the caller before this
             point (kept as a caller responsibility so the streaming boundary
             stays in one place — see iter_final_events / analyze_run).
      obj_time_lookup: callable(host_os, seq) -> landing_ms or None.
      scenario_hops: hop tokens for this run's scenario (scenarios.yaml).
      require_agg2: True for 2-tier scenarios (S2/S4) — see compute_event_deltas.

    Returns: RunAccumulator.
    """
    acc = RunAccumulator(scenario_hops, require_agg2)
    for ev, final_ms in event_iter:
        err = _validate_event(ev)
        host_os = ev.get("host_os") if isinstance(ev, dict) else None
        if err is not None:
            acc.add_skip(host_os or "unknown")
            continue
        host_os = ev.get("host_os", "unknown")
        seq = ev.get("seq")
        landing_ms = obj_time_lookup(host_os, seq)
        obj_times = {"final_ms": final_ms, "landing_ms": landing_ms}
        try:
            deltas, missing_terminal = compute_event_deltas(
                ev, obj_times, scenario_hops, require_agg2=require_agg2)
        except (TypeError, ValueError, KeyError):
            # Defensive: compute_event_deltas is pure arithmetic over values
            # _validate_event already checked, but guard against any residual
            # malformed shape rather than crashing the whole run (finding #4).
            acc.add_skip(host_os)
            continue
        acc.add_event(host_os, seq, deltas, missing_terminal)
    return acc


def build_rows(acc, sent_counts, all_host_os, run_id, flush_ms, uses_landing):
    """Turn a RunAccumulator into the list of per-(host_os, hop) output rows.

    `all_host_os` (finding #6) is the UNION of observed host_os values and the
    generator-manifest host_os keys (e.g. {"linux", "windows"} even if one OS
    contributed zero events) so a fully-lost OS still produces rows with
    count=0, loss_rate=1.0 instead of silently vanishing from the report.
    """
    rows = []
    ordered = list(acc.scenario_hops)
    if "end_to_end" not in ordered:
        ordered.append("end_to_end")

    e2e_flush = end_to_end_flush_ms(flush_ms, uses_landing)

    for host_os in sorted(all_host_os):
        sent = sent_counts.get(host_os)
        landed = acc.landed_count(host_os)
        # Raw loss is clamped >=0 for the headline `loss_rate` (a negative loss
        # would read as nonsensical to a report consumer expecting a rate in
        # [0,1]), but over-delivery must not vanish silently (finding #5): the
        # unclamped raw value and the duplicate count are both surfaced
        # alongside it so an analyst can see *why* loss_rate reads low/zero.
        loss_rate = None
        loss_rate_raw = None
        if sent:
            loss_rate_raw = (sent - landed) / float(sent)
            loss_rate = max(0.0, loss_rate_raw)
        duplicates = acc.duplicates.get(host_os, 0)
        skipped = acc.skipped_events.get(host_os, 0)
        missing_terminal = acc.missing_terminal_tier.get(host_os, 0)

        for hop in ordered:
            arr = acc.per.get(host_os, {}).get(hop)
            vals = arr if arr is not None else []
            raw = summarize(vals)
            row = {
                "run_id": run_id,
                "host_os": host_os,
                "hop": hop,
                "hop_label": HOP_LABELS.get(hop, hop),
                "sent_events": sent,
                "landed_events": landed,
                "loss_rate": loss_rate,
                "loss_rate_raw": loss_rate_raw,
                "duplicates": duplicates,
                "skipped_events": skipped,
                "missing_terminal_tier": (
                    missing_terminal if hop == "agg_last_to_final" else None),
            }
            for k, v in raw.items():
                row[k] = v
            # Batch-adjusted variant only meaningful for S3-write hops.
            if hop in S3_WRITE_HOPS:
                this_flush = (e2e_flush if hop == "end_to_end" else
                              (flush_ms if hop in _SINGLE_FLUSH_HOPS else 0.0))
                adj = batch_adjust(raw, this_flush)
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


def _event_in_run(ev, run_id):
    """Defense-in-depth run_id guard for final-bucket events.

    Objects are scoped by the S3 prefix `final/{run_id}/`, but the aggregator
    stamps that prefix from ITS converge-time run_id, so an in-flight event
    from a PRIOR cell that flushes just after the aggregator is reconfigured
    could theoretically land under the new run's prefix (cell-boundary bleed).
    We therefore also cross-check the event BODY's run_id. Only a positive
    contradiction (a dict event whose run_id differs from the expected run_id)
    is excluded; non-dict events and events without a run_id field pass through
    unchanged so the downstream malformed-event skip accounting is preserved.
    When the expected run_id is empty (manual/legacy whole-prefix analysis),
    the guard is inert and everything matches — mirroring the `final/` sweep.
    """
    if run_id and isinstance(ev, dict):
        body_run = ev.get("run_id")
        if body_run is not None and body_run != run_id:
            return False
    return True


def iter_final_events(s3, final_bucket, run_id):
    """Yield (event, final_ms) from all objects under final/{run_id}/ (streaming).

    final_ms is attached PER EVENT here (finding #1) rather than accumulated in a
    shared bare-seq dict for later lookup — this is the "best" option named in
    the finding: there is no intermediate index to collide across host_os at all
    for the final-put time, because every event carries its own final_ms the
    moment it is parsed.

    We prefer object metadata key 'x-amz-meta-llt-put-ms' when the sink wrote it
    (ms precision), else fall back to LastModified (second precision — PLAN
    §5.4.3 caveat).
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
                if not _event_in_run(ev, run_id):
                    continue  # cross-run bleed guard (see _event_in_run)
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
    """Build a (host_os, seq) -> landing-put-ms index for S3/S4 scenarios.

    Landing objects are written by the agent; PLAN §5.1 events from BOTH host_os
    values are written into the same landing bucket/prefix, so bare `seq` alone
    is not unique (finding #1) — Linux and Windows each emit seq 0..N. The index
    key here is therefore always the tuple (host_os, seq).

    Per-object metadata ('x-amz-meta-llt-seq' + 'x-amz-meta-llt-put-ms') gives us
    an ms-precision put time when the sink wrote it, but metadata is per-OBJECT,
    not per-event, and carries no host_os discriminator — a metadata-only fast
    path would have to guess which OS's seq it is describing, recreating exactly
    the collision this function exists to avoid. So metadata is used ONLY to
    supply the ms-precision put_ms value; the (host_os, seq) key always comes
    from parsing the event body itself (which carries host_os per PLAN §5.1).

    Returns dict (host_os, seq) -> landing_ms.
    """
    index = {}
    paginator = s3.get_paginator("list_objects_v2")
    prefix = "landing/%s/" % run_id
    for page in paginator.paginate(Bucket=landing_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            head = s3.head_object(Bucket=landing_bucket, Key=key)
            put_ms = _object_put_ms(head, obj)
            body = s3.get_object(Bucket=landing_bucket, Key=key)["Body"]
            for ev in _iter_ndjson(body, key):
                if not isinstance(ev, dict):
                    continue
                seq = ev.get("seq")
                host_os = ev.get("host_os")
                if seq is None or host_os is None:
                    continue
                index[(host_os, seq)] = put_ms
    return index


def build_obj_time_lookup(landing_ms_by_key):
    """Return a callable(host_os, seq) -> landing_ms or None, tuple-keyed."""
    def _lookup(host_os, seq):
        return landing_ms_by_key.get((host_os, seq))
    return _lookup


# ===========================================================================
# Output writers
# ===========================================================================
_CSV_FIELDS = [
    "run_id", "host_os", "hop", "hop_label", "count",
    "mean", "p50", "p90", "p99", "max", "stddev",
    "adj_mean", "adj_p50", "adj_p90", "adj_p99", "adj_max",
    "sent_events", "landed_events", "loss_rate", "loss_rate_raw",
    "duplicates", "skipped_events", "missing_terminal_tier",
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
                 "configured S3 batch flush(es) actually crossed by that hop "
                 "(PLAN §5.4.1; end-to-end subtracts two flushes for S3/S4, "
                 "which write to landing AND final). Warmup events excluded. "
                 "`loss` is clamped to >=0; `dup` (duplicate deliveries, SQS "
                 "at-least-once) and `skip` (malformed/incomplete events "
                 "dropped rather than crashing the run) are reported "
                 "separately so over-delivery is never hidden by the clamp.")
    lines.append("")
    header = ("| run_id | host_os | hop | count | mean | p50 | p90 | p99 | max | "
              "stddev | adj_mean | adj_p99 | loss | dup | skip |")
    sep = "|" + "---|" * 15
    lines.append(header)
    lines.append(sep)
    for r in rows:
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (r["run_id"], r["host_os"], r["hop_label"], _fmt(r["count"]),
               _fmt(r["mean"]), _fmt(r["p50"]), _fmt(r["p90"]), _fmt(r["p99"]),
               _fmt(r["max"]), _fmt(r["stddev"]), _fmt(r.get("adj_mean")),
               _fmt(r.get("adj_p99")), _fmt(r["loss_rate"]),
               _fmt(r.get("duplicates")), _fmt(r.get("skipped_events")))
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
    scenario_topology = run_manifest.get("scenario_topology", {}) or {}
    scenario_hops = scenario_topology.get("hops", [])
    uses_landing = scenario_topology.get("uses_landing", False)
    # Terminal-tier guard (finding #7): 2-tier scenarios (S2/S4, agg_tiers==2,
    # or equivalently agg1_to_agg2 present in the scenario's hop list) require
    # hop_ts.agg2 to credit agg_last_to_final; 1-tier scenarios require agg1.
    require_agg2 = (scenario_topology.get("agg_tiers") == 2
                    or "agg1_to_agg2" in scenario_hops)

    # Sent counts (measurement-only) from generator manifests, for loss rate.
    sent_counts = {}
    for os_name, gm in (run_manifest.get("generator_manifests") or {}).items():
        if isinstance(gm, dict) and "measurement_events" in gm:
            sent_counts[os_name] = gm["measurement_events"]

    # Union of observed host_os with the generator-manifest host_os keys
    # (finding #6): `hosts` on the manifest is the host_pair dict
    # {"linux": host_id, "windows": host_id} (run_matrix.py), so its keys are
    # the authoritative set of host_os values this run WAS SUPPOSED to produce,
    # independent of whether any data ever showed up for one of them.
    manifest_host_os = set((run_manifest.get("hosts") or {}).keys())
    manifest_host_os.update(sent_counts.keys())

    # Landing index only if the scenario writes to landing (S3/S4). Tuple-keyed
    # (host_os, seq) — finding #1.
    landing_ms_by_key = {}
    if uses_landing and landing_bucket:
        print("  building landing index from %s ..." % landing_bucket)
        landing_ms_by_key = build_landing_index(s3, landing_bucket, run_id)
    obj_lookup = build_obj_time_lookup(landing_ms_by_key)

    # Stream final events; compute deltas inline (finding #2 — no event-dict
    # buffering). Exclude warmup events at the streaming boundary, same as
    # before, just without retaining anything else.
    print("  streaming final objects from %s ..." % final_bucket)

    def _measurement_only(pairs):
        for ev, final_ms in pairs:
            if isinstance(ev, dict) and ev.get("warmup") is True:
                continue
            yield ev, final_ms

    acc = aggregate_stream(
        _measurement_only(iter_final_events(s3, final_bucket, run_id)),
        obj_lookup, scenario_hops, require_agg2)

    all_host_os = manifest_host_os | acc.host_os_values()
    rows = build_rows(acc, sent_counts, all_host_os, run_id, flush_ms, uses_landing)
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
    n_assertions = [0]

    def check(name, cond):
        n_assertions[0] += 1
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
    d, missing = compute_event_deltas(ev_s2, {"final_ms": 6035.0}, s2_hops,
                                      require_agg2=True)
    check("S2 gen_to_agent == 5", _approx(d["gen_to_agent"], 5.0))
    check("S2 agent_to_agg1 == 15", _approx(d["agent_to_agg1"], 15.0))
    check("S2 agg1_to_agg2 == 15", _approx(d["agg1_to_agg2"], 15.0))
    check("S2 agg_last_to_final == 5000", _approx(d["agg_last_to_final"], 5000.0))
    check("S2 end_to_end == 5035", _approx(d["end_to_end"], 5035.0))
    check("S2 does not emit landing hops",
          "agent_to_landing" not in d and "landing_to_agg1" not in d)
    check("S2 well-formed event has terminal tier present", missing is False)

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
    d3, missing3 = compute_event_deltas(
        ev_s3, {"landing_ms": 7004.0, "final_ms": 12050.0}, s3_hops,
        require_agg2=False)
    check("S3 gen_to_agent == 4", _approx(d3["gen_to_agent"], 4.0))
    check("S3 agent_to_landing == 5000", _approx(d3["agent_to_landing"], 5000.0))
    check("S3 landing_to_agg1 == 46", _approx(d3["landing_to_agg1"], 46.0))
    check("S3 agg_last_to_final == 5000", _approx(d3["agg_last_to_final"], 5000.0))
    check("S3 end_to_end == 10050", _approx(d3["end_to_end"], 10050.0))
    check("S3 last_hop is agg1 (single tier)",
          "agg1_to_agg2" not in d3)
    check("S3 well-formed single-tier event has terminal tier present",
          missing3 is False)

    # --- Test 3: missing operands -> hop omitted (loss), no crash -----------------
    ev_partial = {
        "seq": 3, "host_os": "linux", "warmup": False,
        "t_gen_ns": 3000 * NS_PER_MS,
        "hop_ts": {"agent": 3005.0},   # never reached agg / final
    }
    dp, missing_p = compute_event_deltas(ev_partial, {}, s2_hops, require_agg2=True)
    check("partial event yields only gen_to_agent",
          list(dp.keys()) == ["gen_to_agent"])
    check("partial event (no final) is not flagged missing-terminal-tier "
          "(never reached final at all)", missing_p is False)

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

    # array('d') input works identically to a list (finding #2 storage change).
    st_arr = summarize(array("d", [float(v) for v in vals]))
    check("summarize over array('d') matches list input",
          _approx(st_arr["mean"], st["mean"]) and st_arr["count"] == st["count"])

    # --- Test 5: batch adjustment subtracts the flush and floors at 0 -------------
    raw = summarize([5000.0, 5010.0, 5020.0, 5030.0, 5100.0])
    adj = batch_adjust(raw, 5000.0)
    check("batch_adjust mean subtracts 5000",
          _approx(adj["mean"], raw["mean"] - 5000.0))
    check("batch_adjust count unchanged", adj["count"] == raw["count"])
    tiny = summarize([10.0])          # smaller than flush -> floored to 0
    tiny_adj = batch_adjust(tiny, 5000.0)
    check("batch_adjust floors negative to 0", _approx(tiny_adj["mean"], 0.0))

    # --- Test 5b: end_to_end flush multiplier (finding #3) ------------------------
    check("end_to_end_flush_ms: S1/S2 (no landing) == 1x flush",
          _approx(end_to_end_flush_ms(5000.0, uses_landing=False), 5000.0))
    check("end_to_end_flush_ms: S3/S4 (landing) == 2x flush",
          _approx(end_to_end_flush_ms(5000.0, uses_landing=True), 10000.0))

    # --- Test 6: full aggregate incl. warmup exclusion + loss ---------------------
    # Two measurement events land; one warmup event must be excluded upstream (we
    # simulate the caller having filtered it). One measurement event is "lost"
    # (no final), giving loss = 1/3 against sent=3.
    ev_a = dict(ev_s2)                        # linux, lands
    ev_a["hop_ts"] = dict(ev_s2["hop_ts"])
    ev_b = dict(ev_s2); ev_b["seq"] = 11      # linux, lands (identical deltas)
    ev_b["hop_ts"] = dict(ev_s2["hop_ts"])
    ev_c = dict(ev_partial); ev_c["seq"] = 12 # linux, does NOT land

    def _stream():
        yield ev_a, 6035.0
        yield ev_b, 6035.0
        yield ev_c, None   # never reached final -> no final_ms

    acc = aggregate_stream(_stream(), lambda host_os, seq: None, s2_hops,
                           require_agg2=True)
    rows = build_rows(acc, sent_counts={"linux": 3}, all_host_os={"linux"},
                      run_id="s2-vec-vagg-5k-TEST", flush_ms=5000.0,
                      uses_landing=False)
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

    # --- Test 7: seq collision across host_os (regression, finding #1) ------------
    # Linux seq=1 and Windows seq=1 exist in the SAME run with DIFFERENT final
    # times. A bare-seq index would let one clobber the other; tuple-keyed
    # (host_os, seq) handling must keep them fully independent.
    ev_lin1 = {
        "seq": 1, "host_os": "linux", "warmup": False,
        "t_gen_ns": 1000 * NS_PER_MS,
        "hop_ts": {"agent": 1005.0, "agg1": 1020.0, "agg2": 1035.0},
    }
    ev_win1 = {
        "seq": 1, "host_os": "windows", "warmup": False,
        "t_gen_ns": 2000 * NS_PER_MS,
        "hop_ts": {"agent": 2005.0, "agg1": 2020.0, "agg2": 2035.0},
    }

    def _collision_stream():
        # Linux seq=1 finals at 6035 (5000 flush); Windows seq=1 finals at a
        # totally different time, 9035 (3000ms path, NOT 5000) — if these ever
        # collide via a bare-seq dict, one host_os's agg_last_to_final would be
        # overwritten by the other's, corrupting both.
        yield ev_lin1, 6035.0
        yield ev_win1, 4035.0

    acc_collision = aggregate_stream(_collision_stream(),
                                     lambda host_os, seq: None, s2_hops,
                                     require_agg2=True)
    rows_collision = build_rows(
        acc_collision, sent_counts={"linux": 1, "windows": 1},
        all_host_os={"linux", "windows"}, run_id="s2-collision-TEST",
        flush_ms=5000.0, uses_landing=False)
    lin_final = [r for r in rows_collision
                 if r["host_os"] == "linux" and r["hop"] == "agg_last_to_final"][0]
    win_final = [r for r in rows_collision
                 if r["host_os"] == "windows" and r["hop"] == "agg_last_to_final"][0]
    check("seq collision: linux agg_last_to_final == 5000 (6035-1035, unaffected "
          "by windows seq=1)", _approx(lin_final["mean"], 5000.0))
    check("seq collision: windows agg_last_to_final == 2000 (4035-2035, "
          "unaffected by linux seq=1)", _approx(win_final["mean"], 2000.0))
    check("seq collision: counts independent (1 each)",
          lin_final["count"] == 1 and win_final["count"] == 1)

    # Also regression-test the landing-index tuple key directly (S3/S4 path):
    # Linux seq=5 and Windows seq=5 must resolve to different landing_ms.
    landing_index = {("linux", 5): 100.0, ("windows", 5): 999.0}
    lookup = build_obj_time_lookup(landing_index)
    check("landing lookup: (linux, 5) != (windows, 5) collision",
          lookup("linux", 5) == 100.0 and lookup("windows", 5) == 999.0)

    # --- Test 8: batch-adjusted end_to_end for S3/S4 subtracts TWO flushes -------
    # (finding #3) landing at t_gen+5000 (1 flush), final at landing+5000
    # (another flush) -> raw end_to_end == 10000; adj (2x5000) must floor to ~0,
    # NOT ~5000 (which is what subtracting only one flush would give).
    ev_s4 = {
        "seq": 20, "host_os": "linux", "warmup": False,
        "t_gen_ns": 0 * NS_PER_MS,
        "hop_ts": {"agent": 0.0, "agg1": 5010.0, "agg2": 5020.0},
    }
    s4_hops = ["gen_to_agent", "agent_to_landing", "landing_to_agg1",
               "agg1_to_agg2", "agg_last_to_final"]

    def _s4_stream():
        yield ev_s4, 10000.0   # final put at 10000ms

    def _s4_landing_lookup(host_os, seq):
        return 5000.0 if (host_os, seq) == ("linux", 20) else None

    acc_s4 = aggregate_stream(_s4_stream(), _s4_landing_lookup, s4_hops,
                              require_agg2=True)
    rows_s4 = build_rows(acc_s4, sent_counts={"linux": 1}, all_host_os={"linux"},
                         run_id="s4-flush-TEST", flush_ms=5000.0,
                         uses_landing=True)
    e2e_s4 = [r for r in rows_s4
              if r["host_os"] == "linux" and r["hop"] == "end_to_end"][0]
    check("S3/S4 end_to_end raw == 10000 (t_gen=0 -> final=10000)",
          _approx(e2e_s4["mean"], 10000.0))
    check("S3/S4 end_to_end adj subtracts TWO flushes (10000 - 10000 == 0), "
          "not one (which would floor-clip to a wrong 5000 signal)",
          _approx(e2e_s4["adj_mean"], 0.0))
    # Compare against an S1/S2-shaped run with an identical raw end_to_end of
    # 10000 to prove the multiplier really differs by uses_landing, not by
    # some other accidental factor.
    ev_s2_same_e2e = {
        "seq": 21, "host_os": "linux", "warmup": False,
        "t_gen_ns": 0 * NS_PER_MS,
        "hop_ts": {"agent": 0.0, "agg1": 10.0, "agg2": 20.0},
    }

    def _s2_stream():
        yield ev_s2_same_e2e, 10000.0

    acc_s2b = aggregate_stream(_s2_stream(), lambda host_os, seq: None, s2_hops,
                               require_agg2=True)
    rows_s2b = build_rows(acc_s2b, sent_counts={"linux": 1}, all_host_os={"linux"},
                          run_id="s2-flush-TEST", flush_ms=5000.0,
                          uses_landing=False)
    e2e_s2b = [r for r in rows_s2b
               if r["host_os"] == "linux" and r["hop"] == "end_to_end"][0]
    check("S1/S2 end_to_end raw == 10000 (same as S4 case, for comparison)",
          _approx(e2e_s2b["mean"], 10000.0))
    check("S1/S2 end_to_end adj subtracts ONE flush (10000 - 5000 == 5000), "
          "confirming the S3/S4 case above used the 2x multiplier and not a "
          "shared/miscounted constant",
          _approx(e2e_s2b["adj_mean"], 5000.0))

    # --- Test 9: duplicate deliveries deduped first-wins, reported separately ----
    # (finding #5) SQS at-least-once: seq=30 delivered twice with DIFFERENT
    # final_ms (simulating two separate object writes for the same event). The
    # first delivery's deltas must win; the second must be counted as a
    # duplicate and NOT alter the stats or silently inflate landed_events.
    ev_dup = {
        "seq": 30, "host_os": "linux", "warmup": False,
        "t_gen_ns": 1000 * NS_PER_MS,
        "hop_ts": {"agent": 1005.0, "agg1": 1020.0, "agg2": 1035.0},
    }

    def _dup_stream():
        yield ev_dup, 6035.0   # first delivery: agg_last_to_final = 5000
        yield dict(ev_dup), 9999.0   # duplicate delivery, different final_ms

    acc_dup = aggregate_stream(_dup_stream(), lambda host_os, seq: None, s2_hops,
                               require_agg2=True)
    rows_dup = build_rows(acc_dup, sent_counts={"linux": 1}, all_host_os={"linux"},
                          run_id="s2-dup-TEST", flush_ms=5000.0,
                          uses_landing=False)
    e2e_dup = [r for r in rows_dup
               if r["host_os"] == "linux" and r["hop"] == "end_to_end"][0]
    check("dedupe: end_to_end count == 1 despite two deliveries",
          e2e_dup["count"] == 1)
    check("dedupe: duplicates == 1 reported on the row",
          e2e_dup["duplicates"] == 1)
    final_dup = [r for r in rows_dup
                 if r["host_os"] == "linux" and r["hop"] == "agg_last_to_final"][0]
    check("dedupe: first-wins value kept (5000, not the second delivery's 8964)",
          _approx(final_dup["mean"], 5000.0))
    # loss_rate must not go negative-looking due to a duplicate inflating
    # landed_events past sent_events; landed_events is the UNIQUE-seq count so
    # sent=1, landed=1 -> loss_rate == 0 exactly, not "-100%" from double count.
    check("dedupe: loss_rate == 0 (landed_events counts unique seq, not "
          "deliveries)", _approx(e2e_dup["loss_rate"], 0.0))
    check("dedupe: loss_rate_raw == 0 too (no over-delivery leaks into loss)",
          _approx(e2e_dup["loss_rate_raw"], 0.0))

    # --- Test 10: missing t_gen_ns / malformed JSON -> skipped, not a crash ------
    # (finding #4) Mix of: missing t_gen_ns, non-numeric t_gen_ns, non-dict
    # event, and one healthy event, all in the same stream. The run must
    # complete and report skipped_events per host_os rather than raising.
    ev_missing_tgen = {"seq": 40, "host_os": "linux", "warmup": False,
                       "hop_ts": {"agent": 1.0}}   # no t_gen_ns at all
    ev_bad_tgen = {"seq": 41, "host_os": "linux", "warmup": False,
                  "t_gen_ns": "not-a-number", "hop_ts": {"agent": 1.0}}
    ev_healthy = {"seq": 42, "host_os": "linux", "warmup": False,
                 "t_gen_ns": 1000 * NS_PER_MS, "hop_ts": {"agent": 1005.0}}

    def _degraded_stream():
        yield ev_missing_tgen, None
        yield ev_bad_tgen, None
        yield "not-even-a-dict", None
        yield ev_healthy, None

    try:
        acc_deg = aggregate_stream(_degraded_stream(), lambda host_os, seq: None,
                                   s2_hops, require_agg2=True)
        crashed = False
    except Exception:
        crashed = True
        acc_deg = None
    check("graceful degradation: malformed events do not crash the run",
          not crashed)
    if acc_deg is not None:
        rows_deg = build_rows(acc_deg, sent_counts={}, all_host_os={"linux"},
                              run_id="s2-degraded-TEST", flush_ms=5000.0,
                              uses_landing=False)
        g2a_deg = [r for r in rows_deg
                   if r["host_os"] == "linux" and r["hop"] == "gen_to_agent"][0]
        check("graceful degradation: exactly 1 healthy event counted",
              g2a_deg["count"] == 1)
        # 3 bad inputs: missing t_gen_ns, non-numeric t_gen_ns, non-dict event.
        # The non-dict event has no host_os to attribute to, so it is bucketed
        # under "unknown" — the other two attribute to "linux".
        check("graceful degradation: 2 skips attributed to linux",
              acc_deg.skipped_events.get("linux") == 2)
        check("graceful degradation: 1 skip attributed to unknown (non-dict "
              "event, no host_os to read)",
              acc_deg.skipped_events.get("unknown") == 1)

    # --- Test 11: 100%-loss OS still produces a visible row (finding #6) ---------
    # windows is in the generator-manifest host_pair but contributes ZERO
    # events (fully lost). It must still appear with count=0, loss_rate=1.0
    # rather than being absent from the report entirely.
    def _linux_only_stream():
        yield ev_healthy, None   # only linux ever shows up in the data

    acc_loss = aggregate_stream(_linux_only_stream(), lambda host_os, seq: None,
                                s2_hops, require_agg2=True)
    # Simulate the union performed in analyze_run: manifest says both OSes
    # exist, but only linux produced any accumulator activity.
    all_host_os_loss = {"linux", "windows"} | acc_loss.host_os_values()
    rows_loss = build_rows(acc_loss, sent_counts={"linux": 1, "windows": 5},
                           all_host_os=all_host_os_loss,
                           run_id="s2-100pctloss-TEST", flush_ms=5000.0,
                           uses_landing=False)
    win_rows = [r for r in rows_loss if r["host_os"] == "windows"]
    check("100%-loss OS produces rows for every hop (not silently absent)",
          len(win_rows) == len(set(s2_hops) | {"end_to_end"}))
    win_e2e = [r for r in win_rows if r["hop"] == "end_to_end"][0]
    check("100%-loss OS: count == 0", win_e2e["count"] == 0)
    check("100%-loss OS: loss_rate == 1.0", _approx(win_e2e["loss_rate"], 1.0))
    check("100%-loss OS: landed_events == 0", win_e2e["landed_events"] == 0)
    lin_rows_loss = [r for r in rows_loss if r["host_os"] == "linux"]
    check("100%-loss test: linux (the OS with data) is unaffected by the "
          "windows union", any(r["hop"] == "gen_to_agent" and r["count"] == 1
                               for r in lin_rows_loss))

    # --- Test 12: terminal-tier guard (finding #7) -------------------------------
    # S2/S4 event reaches final with agent+agg1 stamped but agg2 MISSING (e.g.
    # the T2 timestamp write never landed). This must NOT be silently credited
    # as agg_last_to_final using agg1 (which would smuggle the T1->T2 delta
    # into "last aggregator -> final"); it must be excluded from that hop and
    # counted under missing_terminal_tier instead.
    ev_no_agg2 = {
        "seq": 50, "host_os": "linux", "warmup": False,
        "t_gen_ns": 1000 * NS_PER_MS,
        "hop_ts": {"agent": 1005.0, "agg1": 1020.0},   # agg2 absent
    }
    d_guard, missing_guard = compute_event_deltas(
        ev_no_agg2, {"final_ms": 6020.0}, s2_hops, require_agg2=True)
    check("terminal-tier guard: agg_last_to_final NOT emitted when agg2 missing "
          "in a 2-tier scenario", "agg_last_to_final" not in d_guard)
    check("terminal-tier guard: missing_terminal_tier flagged True",
          missing_guard is True)
    check("terminal-tier guard: end_to_end still emitted (independent of the "
          "guard — it only gates the last-hop derivation)",
          "end_to_end" in d_guard)

    def _guard_stream():
        yield ev_no_agg2, 6020.0

    acc_guard = aggregate_stream(_guard_stream(), lambda host_os, seq: None,
                                 s2_hops, require_agg2=True)
    rows_guard = build_rows(acc_guard, sent_counts={"linux": 1},
                            all_host_os={"linux"}, run_id="s2-guard-TEST",
                            flush_ms=5000.0, uses_landing=False)
    final_row_guard = [r for r in rows_guard if r["hop"] == "agg_last_to_final"][0]
    check("terminal-tier guard: aggregate-level agg_last_to_final count == 0 "
          "(the only event lacked agg2)", final_row_guard["count"] == 0)
    check("terminal-tier guard: missing_terminal_tier == 1 reported on the "
          "agg_last_to_final row", final_row_guard["missing_terminal_tier"] == 1)
    e2e_row_guard = [r for r in rows_guard if r["hop"] == "end_to_end"][0]
    check("terminal-tier guard: end_to_end row still counts the event (guard "
          "is scoped to agg_last_to_final only)", e2e_row_guard["count"] == 1)

    # Contrast: a single-tier scenario (require_agg2=False) with only agg1 must
    # NOT be guarded — agg1 IS that scenario's terminal tier.
    d_s1_ok, missing_s1_ok = compute_event_deltas(
        ev_no_agg2, {"final_ms": 6020.0}, ["gen_to_agent", "agent_to_agg1",
                                           "agg_last_to_final"],
        require_agg2=False)
    check("terminal-tier guard: single-tier scenario credits agg1 normally "
          "(agg1 IS its terminal tier, no guard should fire)",
          "agg_last_to_final" in d_s1_ok and missing_s1_ok is False)

    # --- Test 13: cross-run bleed guard (_event_in_run) --------------------------
    # A final object is prefix-scoped by run_id, but the aggregator stamps that
    # prefix from its own converge-time run_id, so a stale event from a prior
    # cell could bleed into a new run's prefix at a cell boundary. iter_final_events
    # cross-checks the event body's run_id to exclude exactly that case, WITHOUT
    # disturbing malformed-event handling (root-caused from the smoke-test 2x).
    check("run_id guard: matching body run_id kept",
          _event_in_run({"seq": 1, "run_id": "R1"}, "R1") is True)
    check("run_id guard: contradicting body run_id excluded (cell-boundary "
          "bleed guard)", _event_in_run({"seq": 1, "run_id": "R2"}, "R1") is False)
    check("run_id guard: event with no run_id field passes through (no positive "
          "mismatch — prefix already scoped it)",
          _event_in_run({"seq": 1}, "R1") is True)
    check("run_id guard: non-dict event passes through (preserves downstream "
          "skip accounting)", _event_in_run("not-a-dict", "R1") is True)
    check("run_id guard: empty expected run_id is inert (manual whole-prefix "
          "sweep still matches everything)",
          _event_in_run({"seq": 1, "run_id": "anything"}, "") is True)

    print("")
    print("Self-test assertion count: %d" % n_assertions[0])
    if failures:
        print("SELF-TEST FAILED: %d assertion(s) failed: %s"
              % (len(failures), ", ".join(failures)))
        return 1
    print("SELF-TEST PASSED: all assertions OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
