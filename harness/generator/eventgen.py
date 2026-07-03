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

Parameters come from CLI flags and/or --config generator.json (the deployed
service form is `eventgen.py --config <dir>/generator.json`; the config file is
re-templated per run by the event-generator Ansible role). Explicit CLI flags
override config values.

The generator writes:
  * NDJSON events, one per line, to --out (rotated at --max-bytes to prevent disk
    fill at 10k EPS ≈ 5 MB/s of padded events; a failed rotation NEVER truncates
    the live file — it appends and counts rotation_failures instead).
  * A local manifest JSON (path = <out>.manifest.json) recording actual events
    emitted, warmup/measurement split, start/end t_gen_ns, wall-clock start/end,
    achieved EPS, rotation counts, and early_stop. run_matrix.py collects this
    via the artifacts bucket (§ orch).

SIGTERM (and Windows SIGBREAK) trigger a clean early stop: output is flushed and
the manifest is written with "early_stop": true. Normal self-exit at
warmup+duration is unchanged.

Exit code 0 on success; non-zero on fatal error.
"""

import argparse
import io
import json
import os
import platform
import signal
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

# ---------------------------------------------------------------------------
# Cooperative shutdown (SIGTERM / Windows SIGBREAK)
# ---------------------------------------------------------------------------
# systemd `systemctl stop` sends SIGTERM; NSSM's stop sequence on Windows can
# deliver console control events surfaced as SIGBREAK. Without a handler the
# process dies mid-loop and the manifest (the loss-rate denominator, PLAN §5.2)
# is never written. The handler only sets a flag; the generation loop checks it
# and exits cleanly, flushing output and writing the manifest with
# "early_stop": true.
_STOP_REQUESTED = False


def _request_stop(signum, frame):  # noqa: ARG001 - signal handler signature
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def _install_signal_handlers():
    """Register the stop flag for SIGTERM (and SIGBREAK where it exists)."""
    for sig_name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue  # SIGBREAK is Windows-only
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError, RuntimeError):
            # Non-main thread or unsupported on this platform: degrade to the
            # pre-handler behavior (hard kill) rather than refusing to run.
            pass


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


def _render_event(tpl, seq, t_gen_ns, warmup, target_bytes=TARGET_EVENT_BYTES):
    """Render one NDJSON event line (WITHOUT trailing newline) sized to target_bytes.

    Returns a str. The pad length is computed so len(line.encode('utf-8')) ==
    target_bytes whenever possible; if the invariant portion already exceeds
    the target (very long run_id/host_id), pad is empty and the event is simply
    longer — correctness is preserved, size normalization degrades gracefully.
    target_bytes defaults to TARGET_EVENT_BYTES (512, PLAN §5.1) and is wired
    from --event-size-bytes / config key event_size_bytes.
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
    pad_len = target_bytes - fixed_len
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

    Rotation FAILURE strategy: on Windows the tailing agent typically holds the
    active file open without FILE_SHARE_DELETE, so the rename can fail. We must
    NEVER truncate the live file in that case (the agent has not read those
    events yet — truncation is silent data loss that shows up as bogus "loss
    rate" in the results). Instead we reopen the SAME file in append mode, keep
    writing, and count the skipped rotation in `rotation_failures` (surfaced in
    the manifest). The byte counter resets so the next rotation attempt happens
    one max_bytes increment later, not on every subsequent write.
    """

    def __init__(self, path, max_bytes):
        self.path = path
        self.max_bytes = int(max_bytes)
        self._bytes_since_open = 0
        self._fh = None
        self.rotations = 0           # successful rotations (manifest evidence)
        self.rotation_failures = 0   # skipped rotations (manifest evidence)
        self._open_new()

    def _open_new(self, mode="w"):
        # Ensure parent directory exists (e.g. /var/log/llt or C:\llt).
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        # buffering=1 => genuinely line-buffered text mode, so each event line is
        # handed to the OS as soon as it is complete and the tailing agent sees
        # it promptly. The periodic writer.flush() in the generation loop is kept
        # as belt-and-braces. Explicit UTF-8 and '\n' newline so Windows does NOT
        # translate to '\r\n' (which would break the fixed byte-size invariant
        # and the agents' newline-delimited parsing).
        self._fh = io.open(
            self.path, mode=mode, encoding="utf-8", newline="\n", buffering=1
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
                # Rotation rename failed (typical cause: the Windows agent holds
                # the file without FILE_SHARE_DELETE). Do NOT truncate — the
                # agent has unread events in this file. Continue appending to the
                # current file and record the skipped rotation.
                self.rotation_failures += 1
                self._open_new(mode="a")
                return
            self.rotations += 1
            self._open_new(mode="w")

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
    event_bytes = int(args.event_size_bytes)

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
    early_stop = False

    # Flush cadence: the writer is line-buffered (buffering=1), so this periodic
    # explicit flush is belt-and-braces only — it bounds staleness even if the io
    # layer coalesces writes for any reason.
    flush_every = max(1, int(eps // 10))  # ~10 flushes/sec

    try:
        i = 0
        while i < target_total:
            # Cooperative shutdown (SIGTERM / SIGBREAK): exit the loop cleanly so
            # the file is flushed and the manifest below is still written.
            if _STOP_REQUESTED:
                early_stop = True
                break
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
                if _STOP_REQUESTED:
                    early_stop = True
                    break
                t_gen_ns = time.time_ns()
                # Warmup flag: true while within the warmup window on the schedule
                # timeline. Using scheduled offset (not measured) keeps the warmup
                # boundary deterministic and independent of jitter.
                sched_offset = i / eps
                is_warmup = sched_offset < warmup_secs
                line = _render_event(tpl, i, t_gen_ns, is_warmup, event_bytes)
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
        # True when the run was cut short by SIGTERM/SIGBREAK (orchestrator stop)
        # rather than reaching warmup+duration — analysis must not treat the
        # nominal target as the loss denominator for such runs.
        "early_stop": early_stop,
        # Rotation evidence: rotation_failures > 0 means the size guard could not
        # rename the active file (agent holding it) and appended instead — no
        # events were truncated, but the file exceeded max_bytes.
        "rotations": writer.rotations,
        "rotation_failures": writer.rotation_failures,
        "t_gen_start_ns": wall_start_ns,
        "t_gen_end_ns": wall_end_ns,
        "wall_start_utc": _iso_utc(wall_start_ns),
        "wall_end_utc": _iso_utc(wall_end_ns),
        "elapsed_secs": elapsed_s,
        "achieved_eps": achieved_eps,
        "event_target_bytes": int(args.event_size_bytes),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "generator_version": "1.1.0",
    }
    manifest_path = args.out + ".manifest.json"
    # Manifest is small; write atomically-ish (write temp then replace).
    tmp = manifest_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as mf:
        json.dump(manifest, mf, indent=2, sort_keys=True)
        mf.write("\n")
    os.replace(tmp, manifest_path)

    return manifest


# ---------------------------------------------------------------------------
# CLI / config-file parameter resolution
# ---------------------------------------------------------------------------
# The deployed service (systemd unit / NSSM service, event-generator role) runs:
#     eventgen.py --config <install dir>/generator.json
# generator.json is the SINGLE run-parameter contract, re-templated per cell by
# configure-scenario.yml. JSON key -> parameter mapping:
_CONFIG_KEY_TO_ATTR = {
    "run_id": "run_id",
    "eps": "eps",
    "warmup_seconds": "warmup",
    "duration_seconds": "duration",
    "host_id": "host_id",
    "host_os": "host_os",
    "output_path": "out",
    "event_size_bytes": "event_size_bytes",
    "max_bytes": "max_bytes",
}
# Unknown JSON keys are IGNORED (forward compatibility — the template also
# carries documentation keys like "_comment" / "_schema_note").

# Values required after merging CLI + config (either source may supply them).
_REQUIRED_ATTRS = [
    ("run_id", "--run-id", "run_id"),
    ("eps", "--eps", "eps"),
    ("duration", "--duration", "duration_seconds"),
    ("host_id", "--host-id", "host_id"),
    ("host_os", "--host-os", "host_os"),
]

# Built-in fallbacks applied when neither CLI nor config supplies a value.
_DEFAULT_WARMUP_SECS = 120.0
_DEFAULT_MAX_BYTES = 256 * 1024 * 1024

# Coercions applied to merged values (config JSON may carry them as any scalar).
_ATTR_COERCE = {
    "run_id": str,
    "eps": float,
    "warmup": float,
    "duration": float,
    "host_id": str,
    "host_os": str,
    "out": str,
    "max_bytes": int,
    "event_size_bytes": int,
}


def build_parser():
    p = argparse.ArgumentParser(
        prog="eventgen.py",
        description="Cross-platform NDJSON latency-test event generator (llt).",
    )
    p.add_argument("--config", default=None,
                   help="Path to a generator.json config (rendered per run by the "
                        "event-generator Ansible role). Keys: run_id, eps, "
                        "warmup_seconds, duration_seconds, host_id, host_os, "
                        "output_path, event_size_bytes, max_bytes (optional). "
                        "Explicit CLI flags override config values; unknown keys "
                        "are ignored.")
    p.add_argument("--run-id", default=None,
                   help="Run identifier, PLAN §3 convention "
                        "(e.g. s2-vec-vagg-5k-20260710T140000Z). Required here "
                        "or via --config.")
    p.add_argument("--eps", default=None, type=float,
                   help="Target events per second (1000 / 5000 / 10000 per PLAN §3). "
                        "Required here or via --config.")
    p.add_argument("--duration", default=None, type=float,
                   help="Measurement duration in seconds (PLAN: 600 = 10 min). "
                        "Required here or via --config (duration_seconds).")
    p.add_argument("--warmup", default=None, type=float,
                   help="Warm-up seconds; these events carry warmup:true and are "
                        "excluded by analysis (PLAN: 120 = 2 min). Default 120 "
                        "unless set here or via --config (warmup_seconds).")
    p.add_argument("--host-id", default=None,
                   help="Host identifier, e.g. llt-lin-vec-01. Required here or "
                        "via --config.")
    p.add_argument("--host-os", default=None, choices=["linux", "windows"],
                   help="Operating system dimension (PLAN §3 / host_os field). "
                        "Required here or via --config.")
    p.add_argument("--out", default=None,
                   help="Output NDJSON path (--config key: output_path). Default: "
                        "Linux /var/log/llt/events.ndjson, Windows C:\\llt\\events.ndjson.")
    p.add_argument("--max-bytes", default=None, type=int,
                   help="Rotate/guard the output file at this size (bytes) to prevent "
                        "disk fill at high EPS. Default 256 MiB.")
    p.add_argument("--event-size-bytes", default=None, type=int,
                   help="Target serialized size of each padded event in bytes "
                        "(PLAN §5.1). Default %d." % TARGET_EVENT_BYTES)
    return p


def _load_config(parser, path):
    """Load and validate the JSON config file; argparse-style error on failure."""
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        parser.error("--config %s: %s" % (path, exc))
    if not isinstance(cfg, dict):
        parser.error("--config %s: top-level JSON must be an object" % path)
    return cfg


def resolve_params(parser, args):
    """Merge CLI flags over --config values, apply defaults, validate.

    Precedence per parameter: explicit CLI flag > config key > built-in default.
    A required value missing from BOTH sources is an argparse-style error
    (exit code 2), mirroring the old `required=True` behavior.
    """
    if args.config is not None:
        cfg = _load_config(parser, args.config)
        for key, attr in _CONFIG_KEY_TO_ATTR.items():
            if getattr(args, attr) is None and cfg.get(key) is not None:
                setattr(args, attr, cfg[key])
        # Any other key (e.g. "_comment", "_schema_note") is deliberately ignored.

    missing = [flag for attr, flag, _k in _REQUIRED_ATTRS
               if getattr(args, attr) is None]
    if missing:
        cfg_keys = [k for attr, flag, k in _REQUIRED_ATTRS
                    if flag in missing]
        parser.error(
            "the following arguments are required (via flag or --config): %s "
            "(config keys: %s)" % (", ".join(missing), ", ".join(cfg_keys)))

    # Built-in defaults for optional values absent from both sources.
    if args.warmup is None:
        args.warmup = _DEFAULT_WARMUP_SECS
    if args.max_bytes is None:
        args.max_bytes = _DEFAULT_MAX_BYTES
    if args.event_size_bytes is None:
        args.event_size_bytes = TARGET_EVENT_BYTES
    if args.out is None:
        args.out = _default_out_path()

    # Coerce config-sourced values to their parameter types (CLI values are
    # already typed by argparse; coercing again is a no-op for them).
    for attr, conv in _ATTR_COERCE.items():
        val = getattr(args, attr)
        if val is None:
            continue
        try:
            setattr(args, attr, conv(val))
        except (TypeError, ValueError):
            parser.error("invalid value for %s: %r" % (attr, val))

    # Semantic validation (mirrors the argparse choices= / sanity constraints).
    if args.host_os not in ("linux", "windows"):
        parser.error("argument --host-os/host_os: invalid choice: %r "
                     "(choose from 'linux', 'windows')" % args.host_os)
    if args.eps <= 0:
        parser.error("--eps/eps must be > 0 (got %s)" % args.eps)
    if args.duration <= 0:
        parser.error("--duration/duration_seconds must be > 0 (got %s)" % args.duration)
    if args.warmup < 0:
        parser.error("--warmup/warmup_seconds must be >= 0 (got %s)" % args.warmup)
    if args.event_size_bytes <= 0:
        parser.error("--event-size-bytes/event_size_bytes must be > 0 (got %s)"
                     % args.event_size_bytes)
    return args


def main(argv=None):
    parser = build_parser()
    args = resolve_params(parser, parser.parse_args(argv))
    # SIGTERM (systemctl stop) / SIGBREAK (Windows) set a flag; the loop exits
    # cleanly and the manifest is still written with early_stop=true.
    _install_signal_handlers()
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
        "achieved %.1f EPS, out=%s%s\n"
        % (
            manifest["events_emitted"],
            manifest["warmup_events"],
            manifest["measurement_events"],
            manifest["achieved_eps"],
            manifest["out_path"],
            " [EARLY STOP — signal]" if manifest["early_stop"] else "",
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
