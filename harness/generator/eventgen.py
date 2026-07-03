#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eventgen.py — cross-platform NDJSON event generator for the llt hop-latency experiment.

Conforms to PLAN.md:
  * §5.1 event schema (run_id, seq, host_id, host_os, t_gen_ns, hop_ts, pad;
    fixed ~512-byte serialized size).
  * §3 volume tiers (1000 / 5000 / 10000 EPS) with precise, drift-free rate control.
  * 2 min warm-up window: the first --warmup seconds of events carry warmup:true so
    analysis (analyze.py) can exclude them per §3 / §5.

Design constraints (binding):
  * Python 3.9+ standard library ONLY. No third-party imports, no fcntl, no uvloop,
    no asyncio event-loop tricks — must run byte-for-byte identically under Windows
    Python (py.exe) and Linux CPython.
  * Wall-clock generation timestamp uses time.time_ns() (CLOCK_REALTIME on Linux;
    the Windows CPython implementation reads the system UTC clock). This is the
    field the analysis joins against tool-native hop_ts and S3 PutObject times, so
    it MUST be real wall-clock, NOT a monotonic counter.
  * Rate *scheduling* uses time.perf_counter() (monotonic, high-resolution) so that
    the emission cadence does not drift even if the wall clock is being disciplined
    by chrony/w32time mid-run (§5.3). We schedule against an absolute perf_counter
    baseline and compensate sleeps, so cumulative drift stays ~0 over a 12-min run.

The generator writes:
  * NDJSON events, one per line, to --out (rotated at --max-bytes to prevent disk
    fill at 10k EPS ≈ 5 MB/s of padded events).
  * A local manifest JSON (path = <out>.manifest.json) recording actual events
    emitted, warmup/measurement split, start/end t_gen_ns, wall-clock start/end,
    and achieved EPS. run_matrix.py collects this via the artifacts bucket (§ orch).

Exit code 0 on success; non-zero on fatal error.
"""

import argparse
import io
import json
import os
import platform
import sys
import time

# ---------------------------------------------------------------------------
# Schema constants (PLAN §5.1)
# ---------------------------------------------------------------------------

# Target serialized size of one NDJSON event (bytes, excluding trailing newline).
# The 'pad' field is sized so the whole JSON object hits this length exactly, which
# keeps per-event wire/serialization cost constant across volume tiers so that
# *hop count* and *volume* are the only variables (PLAN §4.4 parity intent).
TARGET_EVENT_BYTES = 512

# Padding character. Chosen as a JSON-safe ASCII char with no escaping so that the
# byte length of the serialized 'pad' value equals its character length exactly.
PAD_CHAR = "x"

# Compact JSON separators — deterministic, minimal, and identical on all platforms.
_JSON_SEPARATORS = (",", ":")


def _iso_utc(ns):
    """Render an epoch-nanoseconds value as an ISO-8601 UTC string (second precision).

    Used only for human-readable manifest fields; the authoritative timestamps in
    events are integer nanoseconds. time.gmtime avoids any locale/timezone drift.
    """
    secs = ns // 1_000_000_000
    tm = time.gmtime(secs)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", tm)


def _build_event_template(run_id, host_id, host_os):
    """Return (prefix_bytes, suffix_bytes, base_len) for fast per-event assembly.

    We serialize the invariant parts of the event once. Per event we only need to
    substitute the numeric 'seq', 't_gen_ns', and the boolean 'warmup', then size
    the 'pad' string to hit TARGET_EVENT_BYTES. Assembling via string concatenation
    of pre-measured parts avoids a full json.dumps() per event, which matters at
    10k EPS.

    The field ORDER is fixed and matches PLAN §5.1:
      run_id, seq, host_id, host_os, t_gen_ns, hop_ts, warmup, pad
    (hop_ts is emitted as an empty object {}; each downstream hop appends its own
    receive timestamp — Vector VRL remap / Cribl Eval — per §5.1.)
    """
    # Everything up to and including the numeric fields that vary per event is
    # assembled at emit time; here we precompute the static head and the static
    # tail (host fields + empty hop_ts), then the variable pad closes the object.
    #
    # Concretely each line looks like:
    #   {"run_id":"...","seq":<N>,"host_id":"...","host_os":"...",
    #    "t_gen_ns":<NS>,"hop_ts":{},"warmup":<bool>,"pad":"xxxx…"}
    #
    # We split into: HEAD  = '{"run_id":"<run_id>","seq":'
    #                MID   = ',"host_id":"<host_id>","host_os":"<host_os>","t_gen_ns":'
    #                TAIL0 = ',"hop_ts":{},"warmup":'
    #                TAIL1 = ',"pad":"'  ... pad ...  '"}'
    #
    # run_id/host_id/host_os are JSON-escaped once via json.dumps to be safe against
    # any special characters (defensive; run IDs are ASCII by convention §3).
    run_id_j = json.dumps(run_id, separators=_JSON_SEPARATORS)
    host_id_j = json.dumps(host_id, separators=_JSON_SEPARATORS)
    host_os_j = json.dumps(host_os, separators=_JSON_SEPARATORS)

    head = '{"run_id":' + run_id_j + ',"seq":'
    mid = ',"host_id":' + host_id_j + ',"host_os":' + host_os_j + ',"t_gen_ns":'
    tail0 = ',"hop_ts":{},"warmup":'
    tail1 = ',"pad":"'
    tail2 = '"}'

    return {
        "head": head,
        "mid": mid,
        "tail0": tail0,
        "tail1": tail1,
        "tail2": tail2,
    }


def _render_event(tpl, seq, t_gen_ns, warmup):
    """Render one NDJSON event line (WITHOUT trailing newline) sized to TARGET_EVENT_BYTES.

    Returns a str. The pad length is computed so len(line.encode('utf-8')) ==
    TARGET_EVENT_BYTES whenever possible; if the invariant portion already exceeds
    the target (very long run_id/host_id), pad is empty and the event is simply
    longer — correctness is preserved, size normalization degrades gracefully.
    """
    warmup_lit = "true" if warmup else "false"
    # Assemble everything except the pad *value*, then compute how many pad chars
    # are needed to reach the target byte length. All substituted pieces are ASCII
    # (digits / true|false), so their UTF-8 byte length equals their char length.
    prefix = (
        tpl["head"]
        + str(seq)
        + tpl["mid"]
        + str(t_gen_ns)
        + tpl["tail0"]
        + warmup_lit
        + tpl["tail1"]
    )
    suffix = tpl["tail2"]
    # Byte length of the fixed structural bytes (prefix + suffix), pad excluded.
    # PAD_CHAR is 1 byte in UTF-8, so pad char count == pad byte count.
    fixed_len = len(prefix.encode("utf-8")) + len(suffix.encode("utf-8"))
    pad_len = TARGET_EVENT_BYTES - fixed_len
    if pad_len < 0:
        pad_len = 0
    return prefix + (PAD_CHAR * pad_len) + suffix


class RotatingWriter:
    """Minimal, cross-platform size-based rotating writer.

    Why not logging.handlers.RotatingFileHandler? We need a plain byte/line sink
    with predictable flush behavior and no logging-format overhead at 10k EPS, and
    identical semantics on Windows. This truncate/rotate guard exists purely to
    prevent disk fill during a run — the events that matter are shipped by the
    agent (Vector/Cribl Edge) tailing this file, not retained locally.

    Rotation strategy: when the active file exceeds max_bytes, close it, rename to
    "<path>.1" (overwriting any previous .1), and reopen a fresh active file. Only
    one backup is kept (backup_count fixed at 1) — this is a throughput guard, not
    an archival store.
    """

    def __init__(self, path, max_bytes):
        self.path = path
        self.max_bytes = int(max_bytes)
        self._bytes_since_open = 0
        self._fh = None
        self._open_new()

    def _open_new(self):
        # Ensure parent directory exists (e.g. /var/log/llt or C:\llt).
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        # Line-buffered text mode with explicit UTF-8 and '\n' newline so Windows
        # does NOT translate to '\r\n' (which would break the fixed byte-size
        # invariant and the agents' newline-delimited parsing).
        self._fh = io.open(
            self.path, mode="w", encoding="utf-8", newline="\n"
        )
        self._bytes_since_open = 0

    def write_line(self, line):
        # line has no trailing newline; we add exactly one '\n'.
        n = self._fh.write(line)
        self._fh.write("\n")
        # Track bytes for rotation. Approximate with char count + 1 for the newline;
        # pad is ASCII so this equals byte count for the normalized events.
        self._bytes_since_open += (n + 1)
        if self.max_bytes and self._bytes_since_open >= self.max_bytes:
            self._rotate()

    def _rotate(self):
        try:
            self._fh.flush()
            self._fh.close()
        finally:
            backup = self.path + ".1"
            # os.replace is atomic where supported and overwrites on both POSIX and
            # Windows (unlike os.rename on Windows, which errors if dest exists).
            try:
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(self.path, backup)
            except OSError:
                # If rotation rename fails for any reason, fall back to truncating
                # in place on reopen so the run does not abort on a rotation hiccup.
                pass
            self._open_new()

    def flush(self):
        if self._fh is not None:
            self._fh.flush()

    def close(self):
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None


def _default_out_path():
    """Platform-appropriate default output path (PLAN: Linux /var/log/llt, Win C:\\llt)."""
    if platform.system() == "Windows":
        return r"C:\llt\events.ndjson"
    return "/var/log/llt/events.ndjson"


def generate(args):
    """Core generation loop with drift-free absolute-schedule rate control.

    Rate control model (token-bucket equivalent via absolute schedule):
      * We compute the ideal emit time of event i as:
            t_i = t0 + i / eps          (seconds, on the perf_counter timeline)
      * Before emitting event i we sleep until perf_counter() >= t_i, but we cap the
        sleep and re-check to absorb OS sleep granularity. Because targets are
        absolute (not "sleep 1/eps each loop"), any late emission is corrected on the
        next iteration and cumulative drift trends to zero — this is what lets us
        hit 10k EPS over 12 minutes without the classic per-iteration drift.
      * At high EPS we batch: emit a small burst of events whose scheduled times have
        already passed, then sleep to the next future target. This keeps syscall/
        sleep overhead low while preserving the absolute schedule.
    """
    tpl = _build_event_template(args.run_id, args.host_id, args.host_os)
    writer = RotatingWriter(args.out, args.max_bytes)

    eps = float(args.eps)
    total_secs = float(args.warmup) + float(args.duration)
    warmup_secs = float(args.warmup)

    # Nominal total events to emit across warmup+measurement.
    target_total = int(round(eps * total_secs))

    # perf_counter baseline for scheduling; time_ns() baseline for the recorded
    # wall-clock start. These two clocks are read as close together as possible.
    perf0 = time.perf_counter()
    wall_start_ns = time.time_ns()

    emitted = 0
    warmup_emitted = 0
    measurement_emitted = 0

    # Flush cadence: flush the file buffer periodically rather than per-line so the
    # agent sees data promptly without paying a flush syscall on every event.
    flush_every = max(1, int(eps // 10))  # ~10 flushes/sec

    try:
        i = 0
        while i < target_total:
            # Absolute scheduled offset (seconds) for event i.
            target_offset = i / eps
            now_offset = time.perf_counter() - perf0

            if now_offset < target_offset:
                # Sleep until the next target. Cap the sleep so a disciplined clock
                # jump or signal cannot make us oversleep badly; loop re-checks.
                sleep_for = target_offset - now_offset
                # Leave a tiny slack (200 microseconds) then busy-check the tail to
                # improve precision at high EPS where OS sleep granularity dominates.
                if sleep_for > 0.0005:
                    time.sleep(sleep_for - 0.0002)
                # Busy-wait the final sub-ms remainder for cadence accuracy.
                while (time.perf_counter() - perf0) < target_offset:
                    pass

            # Emit a burst: all events whose scheduled time has already elapsed
            # (bounded so we never runaway if the process was paused).
            now_offset = time.perf_counter() - perf0
            # How many events are "due" by now, capped by remaining and a burst cap.
            due = int(now_offset * eps) - i + 1
            if due < 1:
                due = 1
            burst_cap = max(1, int(eps // 20))  # cap burst at ~50ms worth of events
            if due > burst_cap:
                due = burst_cap
            if i + due > target_total:
                due = target_total - i

            for _ in range(due):
                t_gen_ns = time.time_ns()
                # Warmup flag: true while within the warmup window on the schedule
                # timeline. Using scheduled offset (not measured) keeps the warmup
                # boundary deterministic and independent of jitter.
                sched_offset = i / eps
                is_warmup = sched_offset < warmup_secs
                line = _render_event(tpl, i, t_gen_ns, is_warmup)
                writer.write_line(line)
                emitted += 1
                if is_warmup:
                    warmup_emitted += 1
                else:
                    measurement_emitted += 1
                i += 1
                if emitted % flush_every == 0:
                    writer.flush()

        writer.flush()
    finally:
        writer.close()

    wall_end_ns = time.time_ns()
    elapsed_s = (wall_end_ns - wall_start_ns) / 1e9
    achieved_eps = (emitted / elapsed_s) if elapsed_s > 0 else 0.0

    manifest = {
        "run_id": args.run_id,
        "host_id": args.host_id,
        "host_os": args.host_os,
        "out_path": os.path.abspath(args.out),
        "requested_eps": eps,
        "warmup_secs": warmup_secs,
        "measurement_secs": float(args.duration),
        "target_total_events": target_total,
        "events_emitted": emitted,
        "warmup_events": warmup_emitted,
        "measurement_events": measurement_emitted,
        "t_gen_start_ns": wall_start_ns,
        "t_gen_end_ns": wall_end_ns,
        "wall_start_utc": _iso_utc(wall_start_ns),
        "wall_end_utc": _iso_utc(wall_end_ns),
        "elapsed_secs": elapsed_s,
        "achieved_eps": achieved_eps,
        "event_target_bytes": TARGET_EVENT_BYTES,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "generator_version": "1.0.0",
    }
    manifest_path = args.out + ".manifest.json"
    # Manifest is small; write atomically-ish (write temp then replace).
    tmp = manifest_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as mf:
        json.dump(manifest, mf, indent=2, sort_keys=True)
        mf.write("\n")
    os.replace(tmp, manifest_path)

    return manifest


def build_parser():
    p = argparse.ArgumentParser(
        prog="eventgen.py",
        description="Cross-platform NDJSON latency-test event generator (llt).",
    )
    p.add_argument("--run-id", required=True,
                   help="Run identifier, PLAN §3 convention "
                        "(e.g. s2-vec-vagg-5k-20260710T140000Z).")
    p.add_argument("--eps", required=True, type=float,
                   help="Target events per second (1000 / 5000 / 10000 per PLAN §3).")
    p.add_argument("--duration", required=True, type=float,
                   help="Measurement duration in seconds (PLAN: 600 = 10 min).")
    p.add_argument("--warmup", default=120.0, type=float,
                   help="Warm-up seconds; these events carry warmup:true and are "
                        "excluded by analysis (PLAN: 120 = 2 min).")
    p.add_argument("--host-id", required=True,
                   help="Host identifier, e.g. llt-lin-vec-01.")
    p.add_argument("--host-os", required=True, choices=["linux", "windows"],
                   help="Operating system dimension (PLAN §3 / host_os field).")
    p.add_argument("--out", default=None,
                   help="Output NDJSON path. Default: Linux /var/log/llt/events.ndjson, "
                        "Windows C:\\llt\\events.ndjson.")
    p.add_argument("--max-bytes", default=256 * 1024 * 1024, type=int,
                   help="Rotate/guard the output file at this size (bytes) to prevent "
                        "disk fill at high EPS. Default 256 MiB.")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.out is None:
        args.out = _default_out_path()
    try:
        manifest = generate(args)
    except KeyboardInterrupt:
        # Graceful: a run cut short still produced a (partial) file; exit non-zero
        # so the orchestrator can flag the cell.
        sys.stderr.write("eventgen: interrupted\n")
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard, report and fail
        sys.stderr.write("eventgen: fatal: %s\n" % exc)
        return 1
    sys.stderr.write(
        "eventgen: emitted %d events (%d warmup / %d measurement), "
        "achieved %.1f EPS, out=%s\n"
        % (
            manifest["events_emitted"],
            manifest["warmup_events"],
            manifest["measurement_events"],
            manifest["achieved_eps"],
            manifest["out_path"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
