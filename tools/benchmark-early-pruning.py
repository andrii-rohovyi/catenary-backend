#!/usr/bin/env python3
"""Compare footpath Early Pruning modes on real Edelweiss queries.

Modes (EDELWEISS_EARLY_PRUNING): off, pong (pruning only in PONG passes),
full (pruning in PING and PONG; the default). Each run starts a fresh process
with the same binary and timetable; mode order rotates every run to reduce
ordering bias. Itineraries must be identical across modes.

Example (CI SoCal fixtures):
  python3 tools/benchmark-early-pruning.py --binary target/release/edelweiss \
      --query temp/uci-cityhall-request.json --label 'UCI to City Hall' \
      --runs 7 -- --regions-config routing_regions.toml \
      --catenary-config catenaryconfig.toml --database-url "$DATABASE_URL"
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

MODES = ("off", "pong", "full")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--query", required=True, type=Path)
    parser.add_argument("--label", default=None)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("edelweiss_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    label = args.label or args.query.stem
    extra = args.edelweiss_args
    if extra[:1] == ["--"]:
        extra = extra[1:]
    query = json.loads(args.query.read_text())
    if query.get("timetable_view", True) is not True:
        parser.error("query must use timetable_view=true to exercise the PING/PONG kernel")

    samples = {mode: [] for mode in MODES}
    expected = None
    with tempfile.TemporaryDirectory() as tmp:
        timings_path = Path(tmp) / "timings.json"
        command = [
            str(args.binary.resolve()), "--query", str(args.query.resolve()),
            "--query-timings", str(timings_path), *extra,
        ]
        for run in range(args.warmups + args.runs):
            shift = run % len(MODES)
            for mode in MODES[shift:] + MODES[:shift]:
                env = dict(os.environ, EDELWEISS_EARLY_PRUNING=mode)
                process = subprocess.run(command, env=env, capture_output=True, text=True)
                if process.returncode != 0:
                    sys.stderr.write(process.stderr)
                    raise RuntimeError(f"{label}: edelweiss exited {process.returncode} (mode={mode})")
                response = json.loads(process.stdout)
                stats = response["debugOutput"]["raptor"]
                if stats.get("profile_budget_exhausted"):
                    raise RuntimeError(f"{label}: search budget exhausted (mode={mode}); results not comparable")
                actual = {k: response.get(k) for k in ("itineraries", "direct")}
                if expected is None:
                    expected = actual
                elif actual != expected:
                    raise RuntimeError(f"{label}: itineraries differ with EDELWEISS_EARLY_PRUNING={mode}")
                if run >= args.warmups:
                    timings = json.loads(timings_path.read_text())
                    samples[mode].append((timings, stats))
                print(f"{label}: {'warmup' if run < args.warmups else 'sample'} {run + 1} mode={mode} "
                      f"raptor_ms={json.loads(timings_path.read_text())['raptor_ms']:.3f}", file=sys.stderr)

    def median(mode, section, key):
        return statistics.median(row[section][key] for row in samples[mode])

    base = median("off", 0, "raptor_ms")
    rows = []
    for mode in MODES:
        raptor = median(mode, 0, "raptor_ms")
        rows.append((
            mode, raptor,
            median(mode, 1, "walking_ms"),
            median(mode, 1, "route_scan_ms"),
            median(mode, 1, "footpath_edges_examined"),
            median(mode, 0, "query_total_ms"),
            base / raptor,
        ))
    header = "| Query | Mode | RAPTOR ms | Walking ms | Route scan ms | Footpath edges | Query total ms | RAPTOR speedup vs off |"
    lines = [header, "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for mode, raptor, walk, scan, edges, total, speedup in rows:
        lines.append(f"| {label} | {mode} | {raptor:.3f} | {walk:.3f} | {scan:.3f} | {edges:.0f} | {total:.3f} | {speedup:.3f}x |")
    lines.append(f"\n{label}: all {len(journeys(expected))} itineraries identical across modes "
                 f"({args.runs} samples per mode after {args.warmups} warmups; medians).\n")
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def journeys(response):
    return (response.get("itineraries") or []) + (response.get("direct") or [])


if __name__ == "__main__":
    main()
