"""Seed a few Nagercoil corridors so there is something to demonstrate on.

IMPORTANT, AND WORTH SAYING OUT LOUD IN THE DEMO:

The geometry below is *approximate*. It traces real corridors between real
landmarks, but the vertices were placed from map knowledge rather than
surveyed, so the lines will not sit exactly on the carriageway, and the stop
positions are indicative.

That is deliberate and it is the point of the project rather than a gap in it.
Nagercoil has no GTFS feed, no open route list, and no published stop
database. There is nothing accurate to import. The accurate version of this
data is produced by *recording* it: open driver.html in record mode, ride the
route once, and the trace it captures replaces the approximation here. Then
open admin.html, pick that bus under "Promote a recorded trace", load it onto
the map, and save it as a route.

So: use this seed to get pixels moving today, and replace it with a recorded
route before claiming any of it is survey-grade.

Usage:
    python tools/seed_nagercoil.py
    python tools/seed_nagercoil.py --api https://<app>.azurewebsites.net/api
"""

from __future__ import annotations

import argparse
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from shared.geo import encode_polyline, route_length_m  # noqa: E402

CITY = "nagercoil"

# Approximate corridors. See the warning above before treating these as real.
ROUTES = [
    {
        "routeId": "NGL-VAD-KKD",
        "name": "Vadasery - Kottar - Nagercoil Junction",
        "points": [
            (8.19350, 77.43100),   # Vadasery bus stand area
            (8.19050, 77.43050),
            (8.18700, 77.42980),
            (8.18350, 77.42880),
            (8.18020, 77.42780),   # Kottar
            (8.17750, 77.42900),
            (8.17450, 77.43080),
            (8.17100, 77.43220),
            (8.16800, 77.43310),
            (8.16670, 77.43330),   # Nagercoil Junction railway station
        ],
        "stops": [
            ("NGL-S1", "Vadasery Bus Stand", 8.19350, 77.43100),
            ("NGL-S2", "Vadasery Market", 8.18700, 77.42980),
            ("NGL-S3", "Kottar", 8.18020, 77.42780),
            ("NGL-S4", "Nagercoil Town", 8.17450, 77.43080),
            ("NGL-S5", "Nagercoil Junction", 8.16670, 77.43330),
        ],
    },
    {
        "routeId": "NGL-SUC",
        "name": "Nagercoil - Suchindram",
        "points": [
            (8.17750, 77.42900),   # Nagercoil town
            (8.17400, 77.43600),
            (8.16900, 77.44400),
            (8.16400, 77.45200),
            (8.15900, 77.46000),
            (8.15470, 77.46740),   # Suchindram
        ],
        "stops": [
            ("NGL-S4", "Nagercoil Town", 8.17450, 77.43080),
            ("SUC-S1", "Suchindram Temple", 8.15470, 77.46740),
        ],
    },
    {
        "routeId": "NGL-KK",
        "name": "Nagercoil - Kanyakumari",
        "points": [
            (8.17750, 77.42900),   # Nagercoil town
            (8.16500, 77.44000),
            (8.15000, 77.45500),
            (8.13500, 77.47500),
            (8.12000, 77.49500),
            (8.10500, 77.51500),
            (8.09000, 77.53000),
            (8.07810, 77.54100),   # Kanyakumari
        ],
        "stops": [
            ("NGL-S4", "Nagercoil Town", 8.17450, 77.43080),
            ("KK-S1", "Kanyakumari Bus Stand", 8.07810, 77.54100),
        ],
    },
]


def seed(api: str, admin_key: str = "") -> None:
    headers = {"Content-Type": "application/json"}
    if admin_key:
        headers["X-Admin-Key"] = admin_key

    # Collect which routes serve each stop before writing, so a shared stop
    # such as Nagercoil Town lists every route through it.
    stop_routes: dict[str, set[str]] = {}
    stop_meta: dict[str, tuple[str, float, float]] = {}
    for route in ROUTES:
        for stop_id, name, lat, lon in route["stops"]:
            stop_routes.setdefault(stop_id, set()).add(route["routeId"])
            stop_meta[stop_id] = (name, lat, lon)

    for route in ROUTES:
        polyline = encode_polyline(route["points"])
        length_km = route_length_m(route["points"]) / 1000.0

        resp = requests.post(
            f"{api}/manage/routes",
            json={
                "routeId": route["routeId"],
                "name": route["name"],
                "city": CITY,
                "polyline": polyline,
                "stopIds": [s[0] for s in route["stops"]],
                "source": "approximate-seed",
            },
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  route {route['routeId']:<14} {route['name']:<40} {length_km:5.1f} km")

    for stop_id, (name, lat, lon) in stop_meta.items():
        resp = requests.post(
            f"{api}/manage/stops",
            json={
                "stopId": stop_id,
                "name": name,
                "lat": lat,
                "lon": lon,
                "city": CITY,
                "routeIds": sorted(stop_routes[stop_id]),
            },
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  stop  {stop_id:<14} {name:<40} {len(stop_routes[stop_id])} route(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=os.environ.get("BUSTRACK_API", "http://127.0.0.1:7071/api"))
    parser.add_argument("--admin-key", default=os.environ.get("ADMIN_KEY", ""))
    args = parser.parse_args()

    print(f"Seeding approximate Nagercoil routes into {args.api}")
    print("These are placeholders. Replace them with recorded traces.\n")

    try:
        seed(args.api, args.admin_key)
    except requests.RequestException as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1

    print("\nDone. Now run the simulator:")
    print("    python tools/simulator.py --route NGL-VAD-KKD --buses 3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
