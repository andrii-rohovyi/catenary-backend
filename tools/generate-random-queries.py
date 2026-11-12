#!/usr/bin/env python3
"""Generate reproducible random origin/destination queries from GTFS stops.

Input: CSV "lat,lon" rows (e.g. psql \\copy of gtfs.stops). Output: JSON Lines,
one Edelweiss query per line. Points are stops jittered by up to --jitter-m so
OSR access/egress is exercised; pairs are limited to [--min-km, --max-km].
Departures are uniform over the given local dates and hours.
"""
import argparse
import csv
from datetime import date, datetime, time, timedelta
import json
import math
from pathlib import Path
import random
from zoneinfo import ZoneInfo


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def jitter(rng, point, meters):
    lat, lon = point
    r = meters * math.sqrt(rng.random())
    t = rng.uniform(0, 2 * math.pi)
    return (
        round(lat + (r * math.cos(t)) / 111_320, 7),
        round(lon + (r * math.sin(t)) / (111_320 * math.cos(math.radians(lat))), 7),
    )


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stops", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--seed", type=int, default=20261112)
    p.add_argument("--first-date", type=date.fromisoformat, required=True)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--timezone", default="America/Los_Angeles")
    p.add_argument("--min-km", type=float, default=2.0)
    p.add_argument("--max-km", type=float, default=60.0)
    p.add_argument("--jitter-m", type=float, default=250.0)
    args = p.parse_args()

    with args.stops.open(newline="") as f:
        stops = sorted({(round(float(r[0]), 6), round(float(r[1]), 6)) for r in csv.reader(f) if r and r[0]})
    if len(stops) < 2:
        raise SystemExit("need at least two stops")
    rng = random.Random(args.seed)
    tz = ZoneInfo(args.timezone)
    out = []
    while len(out) < args.count:
        a, b = rng.sample(stops, 2)
        if not args.min_km <= haversine_km(a, b) <= args.max_km:
            continue
        day = args.first_date + timedelta(days=rng.randrange(args.days))
        minute = rng.randrange(6 * 60, 21 * 60 + 30)
        departure = datetime.combine(day, time(minute // 60, minute % 60), tz)
        o, d = jitter(rng, a, args.jitter_m), jitter(rng, b, args.jitter_m)
        out.append({
            "id": len(out),
            "from": {"lat": o[0], "lon": o[1]},
            "to": {"lat": d[0], "lon": d[1]},
            "departure_unix_seconds": int(departure.timestamp()),
            "access": [],
            "egress": [],
            "direct_walk_seconds": None,
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(q) + "\n" for q in out))
    print(f"{len(out)} queries from {len(stops)} distinct stops -> {args.output}")


if __name__ == "__main__":
    main()
