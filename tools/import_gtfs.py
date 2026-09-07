"""Import a GTFS feed into the same schema the hand-made Nagercoil routes use.

The point of this tool is not Chennai specifically. It is to show that the
storage schema, the route-snapping maths and the confidence model do not care
whether a route was drawn by hand on the admin page, recorded by riding a bus,
or imported from a 4,600-route published feed. Nothing downstream changes.

Chennai has a public GTFS feed; Nagercoil has nothing. Those are the two ends
of the problem this project exists for, and both end up in the same tables.

Memory notes, because real feeds are big:
  * stop_times.txt is usually the largest file by an order of magnitude and is
    streamed, never loaded whole.
  * Only the shapes actually referenced by a chosen trip are retained.
  * Feeds that ship no shapes.txt (common in India) fall back to building
    geometry from the ordered stops of a representative trip.

Usage:
    python tools/import_gtfs.py --city chennai
    python tools/import_gtfs.py --file path/to/gtfs.zip --city chennai
    python tools/import_gtfs.py --city chennai --limit 50     # small demo subset
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import zipfile
from collections import Counter, defaultdict
from typing import Iterator

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from shared import storage  # noqa: E402
from shared.geo import encode_polyline, route_length_m  # noqa: E402

# Community-maintained unified feed for Chennai MTC.
DEFAULT_FEED = "https://raw.githubusercontent.com/ungalsoththu/ChennaiGTFS/main/data/chennai-unified-gtfs.zip"

# Routes shorter than this are almost always data errors rather than services.
MIN_ROUTE_LENGTH_M = 300.0


def read_csv(zf: zipfile.ZipFile, name: str) -> Iterator[dict]:
    """Stream one CSV out of the archive without materialising it."""
    if name not in zf.namelist():
        return
    with zf.open(name) as raw:
        # utf-8-sig: GTFS files very often carry a byte order mark.
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text)


def download(url: str, dest: str) -> str:
    print(f"Downloading {url}")
    resp = requests.get(url, timeout=180, stream=True)
    resp.raise_for_status()

    total = 0
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            fh.write(chunk)
            total += len(chunk)
    print(f"  {total / 1e6:.1f} MB -> {dest}")
    return dest


def build(zf: zipfile.ZipFile, limit: int | None) -> tuple[list[dict], list[dict]]:
    """Turn a GTFS archive into our route and stop records."""

    # --- routes.txt -------------------------------------------------------
    route_names: dict[str, str] = {}
    for row in read_csv(zf, "routes.txt"):
        rid = (row.get("route_id") or "").strip()
        if not rid:
            continue
        short = (row.get("route_short_name") or "").strip()
        long_name = (row.get("route_long_name") or "").strip()
        # Plain hyphen, not an em dash: these names get printed to Windows
        # consoles that are not always running a UTF-8 code page.
        route_names[rid] = " - ".join(p for p in (short, long_name) if p) or rid
    print(f"  routes.txt      {len(route_names)} routes")

    # --- trips.txt --------------------------------------------------------
    # Pick one representative trip per route: the shape used by the most trips,
    # which is the main variant rather than a one-off diversion.
    shape_votes: dict[str, Counter] = defaultdict(Counter)
    trip_to_route: dict[str, str] = {}
    route_trip: dict[str, str] = {}

    for row in read_csv(zf, "trips.txt"):
        rid = (row.get("route_id") or "").strip()
        tid = (row.get("trip_id") or "").strip()
        sid = (row.get("shape_id") or "").strip()
        if not rid or not tid:
            continue
        trip_to_route[tid] = rid
        route_trip.setdefault(rid, tid)
        if sid:
            shape_votes[rid][sid] += 1
    print(f"  trips.txt       {len(trip_to_route)} trips")

    chosen_shape = {rid: votes.most_common(1)[0][0] for rid, votes in shape_votes.items() if votes}

    # Trim early so a --limit run does not parse shapes and stop_times for
    # thousands of routes it is going to throw away.
    keep_routes = set(route_names)
    if limit:
        keep_routes = set(sorted(keep_routes)[:limit])
        chosen_shape = {r: s for r, s in chosen_shape.items() if r in keep_routes}

    # --- shapes.txt (optional) -------------------------------------------
    wanted_shapes = set(chosen_shape.values())
    shape_points: dict[str, list[tuple[int, float, float]]] = defaultdict(list)

    if wanted_shapes and "shapes.txt" in zf.namelist():
        for row in read_csv(zf, "shapes.txt"):
            sid = (row.get("shape_id") or "").strip()
            if sid not in wanted_shapes:
                continue
            try:
                shape_points[sid].append((
                    int(float(row.get("shape_pt_sequence") or 0)),
                    float(row["shape_pt_lat"]),
                    float(row["shape_pt_lon"]),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        print(f"  shapes.txt      {len(shape_points)} shapes retained")
    else:
        print("  shapes.txt      absent; geometry will come from stop sequences")

    # --- stops.txt --------------------------------------------------------
    stop_meta: dict[str, tuple[str, float, float]] = {}
    for row in read_csv(zf, "stops.txt"):
        sid = (row.get("stop_id") or "").strip()
        if not sid:
            continue
        try:
            stop_meta[sid] = (
                (row.get("stop_name") or sid).strip(),
                float(row["stop_lat"]),
                float(row["stop_lon"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    print(f"  stops.txt       {len(stop_meta)} stops")

    # --- stop_times.txt (streamed; the big one) --------------------------
    stop_routes: dict[str, set[str]] = defaultdict(set)
    trip_stop_sequence: dict[str, list[tuple[int, str]]] = defaultdict(list)
    needed_trips = {route_trip[r] for r in keep_routes if r in route_trip}
    rows_seen = 0

    for row in read_csv(zf, "stop_times.txt"):
        rows_seen += 1
        tid = (row.get("trip_id") or "").strip()
        rid = trip_to_route.get(tid)
        if rid is None or rid not in keep_routes:
            continue

        sid = (row.get("stop_id") or "").strip()
        if sid:
            stop_routes[sid].add(rid)

        # Retain ordering only for the representative trips, for the fallback.
        if tid in needed_trips:
            try:
                trip_stop_sequence[tid].append(
                    (int(float(row.get("stop_sequence") or 0)), sid)
                )
            except (TypeError, ValueError):
                pass
    print(f"  stop_times.txt  {rows_seen} rows streamed")

    # --- assemble ---------------------------------------------------------
    routes: list[dict] = []
    skipped_no_geometry = 0
    skipped_too_short = 0

    for rid in sorted(keep_routes):
        points: list[tuple[float, float]] = []

        sid = chosen_shape.get(rid)
        if sid and shape_points.get(sid):
            points = [(lat, lon) for _, lat, lon in sorted(shape_points[sid])]
        else:
            # Fallback: the ordered stops of the representative trip. Coarser
            # than a real shape, but it still snaps and still measures.
            tid = route_trip.get(rid)
            if tid and trip_stop_sequence.get(tid):
                for _, stop_id in sorted(trip_stop_sequence[tid]):
                    meta = stop_meta.get(stop_id)
                    if meta:
                        points.append((meta[1], meta[2]))

        if len(points) < 2:
            skipped_no_geometry += 1
            continue
        if route_length_m(points) < MIN_ROUTE_LENGTH_M:
            skipped_too_short += 1
            continue

        routes.append({
            "routeId": rid,
            "name": route_names.get(rid, rid),
            "polyline": encode_polyline(points),
            "stopIds": sorted(s for s, rs in stop_routes.items() if rid in rs)[:200],
            "source": "gtfs",
        })

    used_route_ids = {r["routeId"] for r in routes}
    stops = [
        {
            "stopId": sid,
            "name": meta[0],
            "lat": meta[1],
            "lon": meta[2],
            "routeIds": sorted(stop_routes[sid] & used_route_ids)[:50],
        }
        for sid, meta in stop_meta.items()
        if stop_routes[sid] & used_route_ids
    ]

    if skipped_no_geometry:
        print(f"  skipped {skipped_no_geometry} route(s) with no usable geometry")
    if skipped_too_short:
        print(f"  skipped {skipped_too_short} route(s) shorter than {MIN_ROUTE_LENGTH_M:.0f} m")

    return routes, stops


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--city", default="chennai", help="partition key the data lands under")
    parser.add_argument("--url", default=DEFAULT_FEED)
    parser.add_argument("--file", help="use a local GTFS zip instead of downloading")
    parser.add_argument("--limit", type=int, help="import only the first N routes")
    parser.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = parser.parse_args()

    path = args.file
    if not path:
        cache = os.path.join(os.path.dirname(__file__), "..", ".azurite", f"{args.city}-gtfs.zip")
        cache = os.path.abspath(cache)
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        if os.path.exists(cache):
            print(f"Using cached feed {cache}")
            path = cache
        else:
            try:
                path = download(args.url, cache)
            except requests.RequestException as exc:
                print(f"Could not download the feed: {exc}", file=sys.stderr)
                return 1

    print(f"\nReading {os.path.basename(path)}")
    try:
        with zipfile.ZipFile(path) as zf:
            missing = {"routes.txt", "trips.txt", "stops.txt"} - set(zf.namelist())
            if missing:
                print(f"Feed is missing required files: {', '.join(sorted(missing))}", file=sys.stderr)
                return 1
            routes, stops = build(zf, args.limit)
    except zipfile.BadZipFile:
        print(f"{path} is not a valid zip. Delete it and retry.", file=sys.stderr)
        return 1

    print(f"\nPrepared {len(routes)} route(s) and {len(stops)} stop(s) for '{args.city}'")

    if not routes:
        print("Nothing to import.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\nDry run; nothing written. Sample:")
        for r in routes[:5]:
            print(f"  {r['routeId']:<12} {r['name'][:60]}")
        return 0

    print(f"Writing to {storage.connection_string()[:40]}…")
    written_routes = storage.batch_upsert_routes(routes, args.city)
    written_stops = storage.batch_upsert_stops(stops, args.city)

    print(f"\nImported {written_routes} routes and {written_stops} stops into '{args.city}'.")
    print("\nThe same schema now holds hand-drawn Nagercoil routes and an entire")
    print("published city network. Nothing downstream needed changing.")
    print(f"\nSee them with:  curl \"http://localhost:7071/api/routes?city={args.city}&geometry=0\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
