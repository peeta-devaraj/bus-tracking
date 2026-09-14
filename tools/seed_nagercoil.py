"""Load the Nagercoil routes and stops into the API.

The data comes from tools/data/nagercoil_routes.json, which is produced by
tools/build_osm_routes.py:

  * every stop is pinned to a specific OpenStreetMap feature, so it can be
    checked by opening openstreetmap.org/<type>/<id>
  * every route follows the real road network between its stops
  * detours the router made into side lanes to touch roadside stops (a market,
    a town centre) have been cut out, because a bus stops on the main road

WHAT TO SAY ABOUT IT IN A DEMO

This is road-following geometry between real, checkable stops. It is *not* a
surveyed bus route: where a real bus takes a different road from the shortest
drive between two stops, this will differ. Nagercoil has no GTFS feed, no open
route list and no stop database, so there is nothing more authoritative to
import. Recording a real trip with driver.html in record mode still beats it.

An earlier version of this file used hand-placed coordinates. Checked against
OpenStreetMap they were off by up to 1.45 km, and one route visited its stops
in the wrong order -- which is why this now reads generated, verifiable data.

Usage:
    python tools/seed_nagercoil.py
    python tools/seed_nagercoil.py --api https://<app>.azurewebsites.net/api
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

DATA = os.path.join(os.path.dirname(__file__), "data", "nagercoil_routes.json")


def load(path: str = DATA) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("problems"):
        # The builder refuses to call a problem-free build clean, so a file
        # carrying problems was written deliberately. Seed it, but say so.
        print("Warning: this data was built with unresolved problems:", file=sys.stderr)
        for problem in data["problems"]:
            print(f"  ! {problem}", file=sys.stderr)
    return data


def seed(api: str, data: dict, admin_key: str = "") -> None:
    headers = {"Content-Type": "application/json"}
    if admin_key:
        headers["X-Admin-Key"] = admin_key

    city = data["city"]

    # Which routes serve each stop, so a shared stop such as Anna Bus Stand
    # lists every route through it.
    stop_routes: dict[str, set[str]] = {}
    for route in data["routes"]:
        for stop_id in route["stopIds"]:
            stop_routes.setdefault(stop_id, set()).add(route["routeId"])

    for route in data["routes"]:
        resp = requests.post(
            f"{api}/manage/routes",
            json={
                "routeId": route["routeId"],
                "name": route["name"],
                "city": city,
                "polyline": route["polyline"],
                "stopIds": route["stopIds"],
                "source": data["source"],
            },
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  route {route['routeId']:<12} {route['name']:<54} {route['distanceM'] / 1000:5.2f} km")

    for stop in data["stops"]:
        if stop["stopId"] not in stop_routes:
            continue
        # `position` is where the stop sits on its route line, i.e. where a bus
        # actually halts. The raw OSM feature can be the middle of a market.
        position = stop.get("position") or {"lat": stop["featureLat"], "lon": stop["featureLon"]}
        resp = requests.post(
            f"{api}/manage/stops",
            json={
                "stopId": stop["stopId"],
                "name": stop["name"],
                "lat": position["lat"],
                "lon": position["lon"],
                "city": city,
                "routeIds": sorted(stop_routes[stop["stopId"]]),
            },
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  stop  {stop['stopId']:<12} {stop['name']:<34} {stop['osm']:<18} "
              f"{len(stop_routes[stop['stopId']])} route(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.environ.get("BUSTRACK_API", "http://127.0.0.1:7071/api"))
    parser.add_argument("--admin-key", default=os.environ.get("ADMIN_KEY", ""))
    parser.add_argument("--data", default=DATA, help="routes file from build_osm_routes.py")
    args = parser.parse_args()

    data = load(args.data)
    print(f"Seeding Nagercoil ({data['source']}, built {data['generatedAt']}) into {args.api}")
    print(f"{data['attribution']}\n")

    try:
        seed(args.api, data, args.admin_key)
    except requests.RequestException as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1

    print("\nDone. Now run the simulator:")
    print("    python tools/simulator.py --route NGL-VAD-KKD --buses 3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
