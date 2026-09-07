"""Fake buses that drive real routes, so the demo works when no bus does.

The simulator is not a shortcut around the real system. It authenticates with
the same HMAC signature as a driver's phone, posts to the same /ping endpoint,
and is subject to every plausibility check. If the validation logic rejected
real buses, it would reject these too.

Every bus it creates is registered with isSimulated=true, which the API carries
through to the map so simulated buses are visibly labelled. Do not remove that:
a tracking demo that cannot tell you which dots are real is worthless.

Usage:
    python tools/simulator.py --route NGL-VAD-KKD --buses 3
    python tools/simulator.py --route NGL-KK --buses 2 --interval 8 --speed 30
    python tools/simulator.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
import uuid

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from shared.auth import BUS_ID_HEADER, SIGNATURE_HEADER, sign  # noqa: E402
from shared.geo import (  # noqa: E402
    bearing,
    decode_polyline,
    densify,
    point_at_distance,
    route_length_m,
)

# GPS noise in metres, roughly what a phone reports in a moving vehicle.
GPS_NOISE_M = 6.0
# Chance per tick that a bus is held at a stop or a junction.
DWELL_CHANCE = 0.12
DWELL_TICKS = (1, 3)

_running = True


def _stop(signum, frame):
    global _running
    _running = False
    print("\nStopping...")


signal.signal(signal.SIGINT, _stop)


class SimulatedBus:
    """One bus walking a polyline, out and back, for ever."""

    def __init__(self, bus_id: str, secret: str, route: list, speed_kmh: float, start_frac: float):
        self.bus_id = bus_id
        self.secret = secret
        self.route = route
        self.length_m = route_length_m(route)
        self.speed_kmh = speed_kmh
        self.along_m = self.length_m * start_frac
        self.direction = 1
        self.dwell_ticks = 0
        self.sent = 0
        self.rejected = 0

    def advance(self, seconds: float) -> None:
        if self.dwell_ticks > 0:
            self.dwell_ticks -= 1
            return

        if random.random() < DWELL_CHANCE:
            self.dwell_ticks = random.randint(*DWELL_TICKS)
            return

        # Vary speed a little so every bus does not move in lockstep.
        speed = self.speed_kmh * random.uniform(0.75, 1.15)
        self.along_m += self.direction * (speed / 3.6) * seconds

        # Turn round at the termini, the way a town bus actually works.
        if self.along_m >= self.length_m:
            self.along_m = self.length_m
            self.direction = -1
            self.dwell_ticks = random.randint(2, 4)
        elif self.along_m <= 0:
            self.along_m = 0.0
            self.direction = 1
            self.dwell_ticks = random.randint(2, 4)

    def current_fix(self) -> dict:
        lat, lon = point_at_distance(self.route, self.along_m)

        # Scatter the point the way a real GPS does. One degree of latitude is
        # about 111 km, so this is a few metres either way.
        jitter = GPS_NOISE_M / 111_000.0
        lat += random.gauss(0, jitter)
        lon += random.gauss(0, jitter)

        ahead = point_at_distance(
            self.route, min(self.length_m, max(0.0, self.along_m + self.direction * 30))
        )
        heading = bearing(lat, lon, ahead[0], ahead[1])

        moving = self.dwell_ticks == 0
        return {
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "ts": time.time(),
            "accuracy": round(random.uniform(4.0, 18.0), 1),
            "speed": round(self.speed_kmh / 3.6, 2) if moving else 0.0,
            "heading": round(heading, 1),
            "nonce": uuid.uuid4().hex[:16],
        }

    def send(self, api: str) -> tuple[bool, str]:
        body = json.dumps(self.current_fix()).encode("utf-8")
        try:
            resp = requests.post(
                f"{api}/ping",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    BUS_ID_HEADER: self.bus_id,
                    SIGNATURE_HEADER: sign(self.secret, body),
                },
                timeout=10,
            )
        except requests.RequestException as exc:
            self.rejected += 1
            return False, str(exc)[:60]

        if resp.status_code == 200:
            self.sent += 1
            flags = resp.json().get("flags") or []
            return True, ",".join(flags) if flags else "ok"

        self.rejected += 1
        try:
            return False, resp.json().get("reason", str(resp.status_code))
        except ValueError:
            return False, str(resp.status_code)


def admin_headers(admin_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if admin_key:
        headers["X-Admin-Key"] = admin_key
    return headers


def register(api: str, bus_id: str, route_id: str, label: str, admin_key: str) -> str:
    resp = requests.post(
        f"{api}/manage/buses",
        json={
            "busId": bus_id,
            "label": label,
            "routeId": route_id,
            "isSimulated": True,  # carried through to the map. Keep it.
        },
        headers=admin_headers(admin_key),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["secret"]


def fetch_routes(api: str) -> list[dict]:
    resp = requests.get(f"{api}/routes", timeout=30)
    resp.raise_for_status()
    return resp.json()["routes"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.environ.get("BUSTRACK_API", "http://127.0.0.1:7071/api"))
    parser.add_argument("--admin-key", default=os.environ.get("ADMIN_KEY", ""))
    parser.add_argument("--route", help="routeId to run buses on")
    parser.add_argument("--buses", type=int, default=3)
    parser.add_argument("--speed", type=float, default=22.0, help="average km/h")
    parser.add_argument(
        "--interval", type=float, default=10.0,
        help="seconds between pings; the API rejects anything under 5",
    )
    parser.add_argument("--list", action="store_true", help="list available routes and exit")
    args = parser.parse_args()

    try:
        routes = fetch_routes(args.api)
    except requests.RequestException as exc:
        print(f"Cannot reach the API at {args.api}: {exc}", file=sys.stderr)
        return 1

    if args.list or not args.route:
        if not routes:
            print("No routes yet. Run: python tools/seed_nagercoil.py")
            return 1
        print("Available routes:")
        for r in routes:
            print(f"  {r['routeId']:<16} {r['name']}")
        if not args.route:
            print("\nPick one with --route")
        return 0

    route = next((r for r in routes if r["routeId"] == args.route), None)
    if route is None:
        print(f"No route called {args.route}. Use --list to see what exists.", file=sys.stderr)
        return 1
    if not route.get("polyline"):
        print(f"Route {args.route} has no geometry yet.", file=sys.stderr)
        return 1

    if args.interval < 5.5:
        print(f"Interval {args.interval}s is below the API rate limit; using 5.5s")
        args.interval = 5.5

    # Densify so buses move smoothly even on a coarsely recorded route.
    points = densify(decode_polyline(route["polyline"]), max_gap_m=20.0)
    length_km = route_length_m(points) / 1000.0

    print(f"Route {route['routeId']}: {route['name']} ({length_km:.1f} km)")
    print(f"Registering {args.buses} simulated bus(es)...")

    buses: list[SimulatedBus] = []
    for i in range(args.buses):
        bus_id = f"SIM-{args.route}-{i + 1}"
        label = f"{route['routeId']} sim {i + 1}"
        try:
            secret = register(args.api, bus_id, args.route, label, args.admin_key)
        except requests.RequestException as exc:
            print(f"  could not register {bus_id}: {exc}", file=sys.stderr)
            return 1
        # Space them out along the route so they do not travel as a convoy.
        buses.append(
            SimulatedBus(bus_id, secret, points, args.speed, start_frac=i / max(1, args.buses))
        )
        print(f"  {bus_id}")

    print(f"\nDriving. Ping every {args.interval:.0f}s. Ctrl-C to stop.\n")

    tick = 0
    while _running:
        tick += 1
        line = []
        for bus in buses:
            bus.advance(args.interval)
            ok, note = bus.send(args.api)
            marker = "ok" if ok else f"REJECTED:{note}"
            km = bus.along_m / 1000.0
            arrow = ">" if bus.direction > 0 else "<"
            line.append(f"{bus.bus_id.split('-')[-1]}{arrow}{km:5.2f}km {marker}")
        # flush=True or the output sits in a buffer and the run looks dead.
        print(f"[{tick:4d}] " + " | ".join(line), flush=True)

        # Sleep in slices so Ctrl-C is responsive.
        slept = 0.0
        while _running and slept < args.interval:
            time.sleep(0.25)
            slept += 0.25

    total_sent = sum(b.sent for b in buses)
    total_rejected = sum(b.rejected for b in buses)
    print(f"\nSent {total_sent} fixes, {total_rejected} rejected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
