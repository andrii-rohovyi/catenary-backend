#!/usr/bin/env python3
"""Run many Edelweiss queries under each EDELWEISS_EARLY_PRUNING mode.

Each query runs once per mode (off, pong, full) in a fresh process; mode order
rotates per query. Itineraries are compared across modes for every query.
Writes a per-query CSV and prints/publishes an aggregate Markdown summary.

  python3 tools/benchmark-early-pruning-batch.py --binary target/release/edelweiss \
      --queries temp/random-queries.jsonl --csv temp/ep-batch.csv -- \
      --regions-config routing_regions.toml --catenary-config catenaryconfig.toml \
      --database-url "$DATABASE_URL"
"""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

MODES = ("off", "pong", "full")
FIELDS = ("raptor_ms", "walking_ms", "route_scan_ms", "footpath_edges_examined", "query_total_ms")


def run(command, mode, timings_path):
    env = dict(os.environ, EDELWEISS_EARLY_PRUNING=mode)
    p = subprocess.run(command, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        return None, p.stderr.strip().splitlines()[-1:] or ["exit %d" % p.returncode]
    response = json.loads(p.stdout)
    stats = response["debugOutput"]["raptor"]
    timings = json.loads(timings_path.read_text())
    row = {
        "raptor_ms": timings["raptor_ms"],
        "query_total_ms": timings["query_total_ms"],
        "walking_ms": stats.get("walking_ms", 0.0),
        "route_scan_ms": stats.get("route_scan_ms", 0.0),
        "footpath_edges_examined": stats.get("footpath_edges_examined", 0),
        "budget_exhausted": bool(stats.get("profile_budget_exhausted")),
        "journeys": len(response.get("itineraries") or []),
    }
    return (row, {k: response.get(k) for k in ("itineraries", "direct")}), None


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def gmean(values):
    return math.exp(sum(math.log(v) for v in values) / len(values))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", required=True, type=Path)
    ap.add_argument("--queries", required=True, type=Path)
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--warmups", type=int, default=3)
    ap.add_argument("edelweiss_args", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    extra = args.edelweiss_args[1:] if args.edelweiss_args[:1] == ["--"] else args.edelweiss_args
    queries = [json.loads(line) for line in args.queries.read_text().splitlines() if line.strip()]
    if args.limit:
        queries = queries[: args.limit]

    rows, errors, mismatches = [], [], []
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        qpath, tpath = Path(tmp) / "q.json", Path(tmp) / "t.json"
        command = [str(args.binary.resolve()), "--query", str(qpath), "--query-timings", str(tpath), *extra]
        for i, q in enumerate(queries):
            body = {k: v for k, v in q.items() if k != "id"}
            qpath.write_text(json.dumps(body))
            if i == 0:
                for _ in range(args.warmups):
                    for mode in MODES:
                        run(command, mode, tpath)
            shift = i % len(MODES)
            results, failed = {}, None
            for mode in MODES[shift:] + MODES[:shift]:
                result, err = run(command, mode, tpath)
                if err:
                    failed = f"{mode}: {err[0][:200]}"
                    break
                results[mode] = result
            if failed:
                errors.append((q.get("id", i), failed))
                continue
            same = all(results[m][1] == results["off"][1] for m in MODES)
            if not same:
                mismatches.append(q.get("id", i))
            row = {"id": q.get("id", i), "identical": same, "journeys": results["off"][0]["journeys"],
                   "budget_exhausted": any(results[m][0]["budget_exhausted"] for m in MODES)}
            for m in MODES:
                for f in FIELDS:
                    row[f"{m}_{f}"] = results[m][0][f]
            rows.append(row)
            if (i + 1) % 50 == 0:
                print(f"{i + 1}/{len(queries)} queries, {len(errors)} errors, {len(mismatches)} mismatches, "
                      f"{time.monotonic() - started:.0f}s", file=sys.stderr, flush=True)

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    def summarize(title, subset):
        lines = [f"### {title}: {len(subset)} queries", "",
                 "| Mode | RAPTOR median ms | RAPTOR p90 ms | RAPTOR total s | Walking total s | Footpath edges total | Geo-mean RAPTOR speedup vs off |",
                 "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        if not subset:
            return lines + ["| - | - | - | - | - | - | - |", ""]
        for m in MODES:
            r = [x[f"{m}_raptor_ms"] for x in subset]
            speed = gmean([max(x["off_raptor_ms"], 1e-3) / max(x[f"{m}_raptor_ms"], 1e-3) for x in subset])
            lines.append(f"| {m} | {statistics.median(r):.3f} | {pct(r, 0.9):.3f} | {sum(r) / 1000:.2f} | "
                         f"{sum(x[f'{m}_walking_ms'] for x in subset) / 1000:.2f} | "
                         f"{sum(x[f'{m}_footpath_edges_examined'] for x in subset):,} | {speed:.3f}x |")
        return lines + [""]

    with_transit = [r for r in rows if r["journeys"] > 0 and not r["budget_exhausted"]]
    heavy = sorted(with_transit, key=lambda r: r["off_raptor_ms"])[int(0.75 * len(with_transit)):] if with_transit else []
    text = "\n".join([
        f"## Early Pruning batch benchmark ({len(queries)} random SoCal queries)", "",
        f"Completed {len(rows)}, errors {len(errors)}, itinerary mismatches across modes: **{len(mismatches)}**, "
        f"with transit itineraries: {len(with_transit)}. Wall time {time.monotonic() - started:.0f}s.", "",
        *summarize("All completed queries", rows),
        *summarize("Queries with transit itineraries", with_transit),
        *summarize("Slowest 25% (by off RAPTOR time) with transit", heavy),
        *(["Mismatching query ids: " + ", ".join(map(str, mismatches[:50])), ""] if mismatches else []),
        *(["First errors:", *[f"- {qid}: {e}" for qid, e in errors[:10]], ""] if errors else []),
    ])
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
