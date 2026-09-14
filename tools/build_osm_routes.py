"""Build road-following Nagercoil routes from OpenStreetMap.

Why this exists
---------------
The original seed routes were hand-placed vertices, and they were worse than
"approximate": checked against OpenStreetMap, Kottar and Nagercoil Junction
were each about 1.4 km from where the seed put them, and one route visited its
stops in the wrong order. The intended fix was record mode -- ride the bus once
and let the trace become the route -- but that needs someone on a bus.

This tool gets most of the way there without anyone riding anything:

  1. Every stop is pinned to a *specific, named OpenStreetMap feature* by its
     element id (e.g. way/227903070 is Vadasery Bus Stand). Anyone can check a
     stop by opening https://www.openstreetmap.org/way/227903070.
  2. The OSRM routing engine then finds the drivable path between consecutive
     stops over the real road network, so lines follow carriageways instead of
     cutting across fields.
  3. The result is written to tools/data/nagercoil_routes.json and committed.
     Seeding reads that file, so nothing depends on OSM or OSRM being up at
     demo time.

What this is NOT
----------------
It is the *road* a vehicle would drive between these stops, not a surveyed
bus route. Where a real bus takes a longer road than the shortest drive, this
will differ. The output is labelled `osm-routed`, not `surveyed`, for exactly
that reason. Recording a real trip still beats it.

Data use: OpenStreetMap data is (c) OpenStreetMap contributors, ODbL. Routing
uses the public OSRM demo server, which permits light, occasional use like a
one-off build -- do not call it from the running app.

Usage:
    python tools/build_osm_routes.py            # fetch, route, write JSON
    python tools/build_osm_routes.py --check    # fetch and report, write nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from shared.geo import (  # noqa: E402
    decode_polyline,
    encode_polyline,
    find_excursions,
    haversine,
    remove_excursions,
    route_length_m,
    snap_to_route,
)

OUTPUT = os.path.join(os.path.dirname(__file__), "data", "nagercoil_routes.json")

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
HEADERS = {"User-Agent": "bus-tracking-student-project/0.2 (college coursework; one-off build)"}

# A stop further than this from any drivable road is probably pinned to the
# wrong feature (a temple interior, a field) and should be looked at by hand.
MAX_SNAP_DISTANCE_M = 250.0

# Road distance over straight-line distance. Real roads wind, so 1.2-1.8 is
# normal; above this the router has probably found some absurd detour.
MAX_DETOUR_RATIO = 2.5

# A placed stop further than this from its route line is not really on the
# route. Matches the ingest off-route threshold, so a bus waiting exactly at
# the stop is never flagged as off-route.
MAX_STOP_OFFSET_M = 150.0

# How close a detour's far end must be to a stop to be blamed on that stop.
SPUR_BLAME_RADIUS_M = 80.0

# Out-and-back detours longer than this are left alone and reported instead.
MAX_SPUR_LENGTH_M = 600.0

STATION = "station"    # buses genuinely drive in: bus stands, railway forecourts
ROADSIDE = "roadside"  # buses stop on the road passing it: markets, neighbourhoods, towns

# stopId -> (display name, kind, OSM element type, OSM element id)
STOPS: dict[str, tuple[str, str, str, int]] = {
    "NGL-S1": ("Vadasery Bus Stand", STATION, "way", 227903070),
    "NGL-S2": ("Vadasery Market", ROADSIDE, "way", 248610348),
    "NGL-S4": ("Anna Bus Stand (Nagercoil Town)", STATION, "way", 253175443),
    "NGL-S3": ("Kottar", ROADSIDE, "node", 360396403),
    "NGL-S5": ("Nagercoil Junction", STATION, "node", 353002292),
    "SUC-S1": ("Suchindram", ROADSIDE, "node", 353035635),
    "KTM-S1": ("Kottaram", ROADSIDE, "node", 2593132676),
    "KK-S1": ("Kanyakumari Bus Stand", STATION, "way", 551655745),
}

# Stops listed in travel order. Route ids are kept from the original seed so
# existing buses, history and tests that name them keep working.
ROUTES = [
    {
        "routeId": "NGL-VAD-KKD",
        "name": "Vadasery - Anna Bus Stand - Kottar - Nagercoil Junction",
        "stops": ["NGL-S1", "NGL-S2", "NGL-S4", "NGL-S3", "NGL-S5"],
    },
    {
        "routeId": "NGL-SUC",
        "name": "Anna Bus Stand - Suchindram",
        "stops": ["NGL-S4", "SUC-S1"],
    },
    {
        # The Nagercoil-Kanyakumari road runs through Suchindram and Kottaram,
        # so they are real intermediate stops rather than a straight shot. More
        # stops also means more road segments for travel times to be learned on.
        "routeId": "NGL-KK",
        "name": "Anna Bus Stand - Suchindram - Kottaram - Kanyakumari",
        "stops": ["NGL-S4", "SUC-S1", "KTM-S1", "KK-S1"],
    },
]


def fetch_stop_locations() -> dict[str, dict]:
    """Resolve every stop's OSM element id to coordinates in one Overpass call."""
    by_type: dict[str, list[int]] = {"node": [], "way": [], "relation": []}
    for _, _, osm_type, osm_id in STOPS.values():
        by_type[osm_type].append(osm_id)

    parts = [
        f"{osm_type}(id:{','.join(str(i) for i in ids)});"
        for osm_type, ids in by_type.items()
        if ids
    ]
    query = f"[out:json][timeout:60];({''.join(parts)});out center tags;"

    # Public Overpass servers are shared and regularly time out under load, so
    # try each mirror in turn rather than failing the whole build on one 504.
    elements = None
    errors = []
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(url, data={"data": query}, headers=HEADERS, timeout=120)
            resp.raise_for_status()
            elements = resp.json()["elements"]
            break
        except (requests.RequestException, ValueError, KeyError) as exc:
            errors.append(f"{url}: {exc}")
            time.sleep(2.0)
    if elements is None:
        raise RuntimeError("every Overpass mirror failed:\n    " + "\n    ".join(errors))

    found: dict[tuple[str, int], dict] = {}
    for element in elements:
        lat = element.get("lat", element.get("center", {}).get("lat"))
        lon = element.get("lon", element.get("center", {}).get("lon"))
        found[(element["type"], element["id"])] = {
            "lat": lat,
            "lon": lon,
            "osmName": element.get("tags", {}).get("name", ""),
        }

    out: dict[str, dict] = {}
    missing = []
    for stop_id, (name, kind, osm_type, osm_id) in STOPS.items():
        hit = found.get((osm_type, osm_id))
        if not hit or hit["lat"] is None:
            missing.append(f"{stop_id} ({osm_type}/{osm_id})")
            continue
        out[stop_id] = {
            "stopId": stop_id,
            "name": name,
            "kind": kind,
            "osm": f"{osm_type}/{osm_id}",
            "osmName": hit["osmName"],
            "featureLat": round(hit["lat"], 6),
            "featureLon": round(hit["lon"], 6),
        }

    if missing:
        # An element can be deleted or merged in OSM. Fail loudly rather than
        # silently dropping a stop from a route.
        raise RuntimeError("OSM elements not found: " + ", ".join(missing))
    return out


def route_through(stops: list[dict]) -> dict:
    """Ask OSRM for the driving route through stops, in order."""
    coords = ";".join(f"{s['featureLon']},{s['featureLat']}" for s in stops)
    url = f"{OSRM_URL}/{coords}"
    params = {"overview": "full", "geometries": "polyline", "steps": "false"}

    resp = requests.get(url, params=params, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != "Ok" or not body.get("routes"):
        raise RuntimeError(f"OSRM could not route: {body.get('code')} {body.get('message', '')}")
    return body


def build() -> dict:
    print("Resolving stops from OpenStreetMap...")
    stop_info = fetch_stop_locations()
    for s in stop_info.values():
        print(f"  {s['stopId']:<8} {s['name']:<34} {s['kind']:<9} {s['osm']:<18} "
              f"{s['featureLat']:.5f},{s['featureLon']:.5f}")

    routes_out = []
    problems: list[str] = []

    print("\nRouting over the road network (OSRM)...")
    for spec in ROUTES:
        ordered = [stop_info[sid] for sid in spec["stops"]]
        body = route_through(ordered)
        route = body["routes"][0]

        # Where OSRM put each stop on the road network.
        road_points: list[tuple[float, float]] = []
        for stop, wp in zip(ordered, body["waypoints"]):
            snap_lon, snap_lat = wp["location"]
            road_points.append((snap_lat, snap_lon))
            snap_m = float(wp.get("distance", 0.0))
            if snap_m > MAX_SNAP_DISTANCE_M:
                problems.append(f"{spec['routeId']}: {stop['stopId']} is {snap_m:.0f} m from the nearest road")

        raw_points = decode_polyline(route["geometry"])
        raw_m = route_length_m(raw_points)
        osrm_m = float(route["distance"])

        # Cross-check our polyline decoder against the router's own distance on
        # real data, before changing anything. A mismatch would mean every
        # downstream distance (snapping, ETAs) is quietly wrong.
        if abs(raw_m - osrm_m) / osrm_m > 0.03:
            problems.append(f"{spec['routeId']}: decoded length {raw_m:.0f} m vs OSRM {osrm_m:.0f} m")

        # ---- Spurs ----------------------------------------------------------
        # A stop pinned to a market or a town centre snaps to the nearest lane,
        # and the router drives up that lane and back to touch it. Cut those
        # out. Keep the ones into stations, which buses really do enter.
        to_remove = []
        spur_log = []
        for ex in find_excursions(raw_points, max_length_m=MAX_SPUR_LENGTH_M):
            stop, road_pt = min(
                zip(ordered, road_points),
                key=lambda pair: haversine(pair[1][0], pair[1][1], ex.tip[0], ex.tip[1]),
            )
            blame_m = haversine(road_pt[0], road_pt[1], ex.tip[0], ex.tip[1])

            if blame_m > SPUR_BLAME_RADIUS_M:
                problems.append(
                    f"{spec['routeId']}: unexplained {ex.length_m:.0f} m detour near "
                    f"{ex.tip[0]:.5f},{ex.tip[1]:.5f} (no stop within {SPUR_BLAME_RADIUS_M:.0f} m); left in"
                )
                continue
            if stop["kind"] == STATION:
                spur_log.append({"stopId": stop["stopId"], "lengthM": round(ex.length_m, 1), "action": "kept"})
                continue
            to_remove.append(ex)
            spur_log.append({"stopId": stop["stopId"], "lengthM": round(ex.length_m, 1), "action": "removed"})

        points = remove_excursions(raw_points, to_remove)
        route_m = route_length_m(points)

        # ---- Place stops on the cleaned line --------------------------------
        placements = []
        for stop in ordered:
            snap = snap_to_route((stop["featureLat"], stop["featureLon"]), points)
            placements.append((stop, snap))
            if snap.offset_m > MAX_STOP_OFFSET_M:
                problems.append(
                    f"{spec['routeId']}: {stop['stopId']} ends up {snap.offset_m:.0f} m from the route line"
                )
            # The first route through a stop decides where its marker sits.
            # That is where a bus actually halts: on the line, not in a market.
            stop.setdefault("position", {
                "lat": round(snap.snapped[0], 6),
                "lon": round(snap.snapped[1], 6),
                "offsetFromFeatureM": round(snap.offset_m, 1),
            })

        alongs = [snap.along_m for _, snap in placements]
        if any(b <= a for a, b in zip(alongs, alongs[1:])):
            problems.append(
                f"{spec['routeId']}: stops are out of order along the line: "
                + ", ".join(f"{st['stopId']}@{a:.0f}m" for (st, _), a in zip(placements, alongs))
            )

        # ---- Legs -----------------------------------------------------------
        # Distances come from the cleaned line so they agree with what snapping
        # measures at runtime. OSRM's free-flow time per leg is scaled by how
        # much that leg shrank when a spur was cut.
        legs = []
        for k, leg in enumerate(route["legs"]):
            a_stop, a_snap = placements[k]
            b_stop, b_snap = placements[k + 1]
            leg_m = b_snap.along_m - a_snap.along_m
            osrm_leg_m = float(leg["distance"]) or 1.0
            ratio = leg_m / osrm_leg_m
            legs.append({
                "from": a_stop["stopId"],
                "to": b_stop["stopId"],
                "distanceM": round(leg_m, 1),
                # Free-flow car time. Buses are slower; this is a physical
                # baseline for the simulator, not a timetable.
                "freeFlowS": round(float(leg["duration"]) * ratio, 1),
            })
            if ratio < 0.4 or ratio > 1.1:
                problems.append(
                    f"{spec['routeId']}: leg {a_stop['stopId']}->{b_stop['stopId']} is "
                    f"{leg_m:.0f} m on the line vs {osrm_leg_m:.0f} m routed"
                )

        straight_m = sum(
            haversine(a["featureLat"], a["featureLon"], b["featureLat"], b["featureLon"])
            for a, b in zip(ordered, ordered[1:])
        )
        detour = route_m / straight_m if straight_m else 0.0
        if detour > MAX_DETOUR_RATIO:
            problems.append(f"{spec['routeId']}: road is {detour:.1f}x the straight-line distance")

        routes_out.append({
            "routeId": spec["routeId"],
            "name": spec["name"],
            "stopIds": spec["stops"],
            "polyline": encode_polyline(points),
            "distanceM": round(route_m, 1),
            "freeFlowS": round(sum(leg["freeFlowS"] for leg in legs), 1),
            "vertices": len(points),
            "detourRatio": round(detour, 2),
            "stopsAlong": [
                {"stopId": st["stopId"], "alongM": round(sn.along_m, 1), "offsetM": round(sn.offset_m, 1)}
                for st, sn in placements
            ],
            "spurs": spur_log,
            "legs": legs,
        })

        removed = sum(1 for x in spur_log if x["action"] == "removed")
        print(f"  {spec['routeId']:<12} {route_m / 1000:5.2f} km  {len(points):4d} vertices  "
              f"detour {detour:.2f}x  spurs removed {removed}")
        for x in spur_log:
            print(f"      spur {x['action']:<7} {x['lengthM']:6.0f} m  at {x['stopId']}")

        time.sleep(1.0)  # be polite to a shared public server

    return {
        "city": "nagercoil",
        "source": "osm-routed",
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "attribution": "Map data (c) OpenStreetMap contributors, ODbL. Routing by OSRM.",
        "caveat": (
            "Road-following geometry between OSM-pinned stops. This is the drivable "
            "path between stops, not a surveyed bus route."
        ),
        "stops": list(stop_info.values()),
        "routes": routes_out,
        "problems": problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report only; do not write the JSON")
    args = parser.parse_args()

    try:
        data = build()
    except (requests.RequestException, RuntimeError) as exc:
        print(f"\nBuild failed: {exc}", file=sys.stderr)
        return 1

    if data["problems"]:
        print("\nProblems worth a look before trusting this:")
        for p in data["problems"]:
            print(f"  ! {p}")
    else:
        print("\nAll sanity checks passed.")

    if args.check:
        print("\n--check given; nothing written.")
        return 0

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"\nWrote {os.path.relpath(OUTPUT)}")
    return 1 if data["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
