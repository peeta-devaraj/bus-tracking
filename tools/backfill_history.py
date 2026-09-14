"""Write simulated bus history, then train the travel-time models on it.

Why this exists
---------------
The learned arrival model needs weeks of history, and nobody can ride a bus in
Nagercoil to collect it. This fills that gap for demonstration, and only for
demonstration:

  * Every history bus is registered with isSimulated=true, so every model
    trained from it reports trainedOn="simulated" on every estimate. The rider
    map shows that. An estimate learned here is never presented as knowledge of
    real traffic.
  * History buses are registered *inactive*, so they cannot send live pings and
    never appear on the map. They exist only as training data.
  * Re-running overwrites the same rows rather than doubling them.

It writes to storage directly rather than through /ping, because the ingest
checks rightly reject a fix timestamped days ago.

Usage (local, against Azurite and the local API):
    python tools/backfill_history.py

Writing is the slow part: about nine minutes for three routes locally. Against
Azure, fewer days and sparser fixes keep it reasonable (--days 8 --sample 30).

Against Azure, point it at the storage account and the deployed API:
    $env:STORAGE_CONNECTION_STRING = az storage account show-connection-string ...
    python tools/backfill_history.py --api https://<app>.azurewebsites.net/api --admin-key <key>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from shared import storage, training  # noqa: E402
from shared.auth import new_secret  # noqa: E402
from traffic import DATA_PATH, STRUCTURED, Traffic, load_profile  # noqa: E402


def route_ids_in_data() -> list[str]:
    with open(DATA_PATH, encoding="utf-8") as fh:
        return [r["routeId"] for r in json.load(fh)["routes"]]


def backfill_route(route_id: str, days: int, buses: int, sample_s: float, seed: int) -> int:
    profile = load_profile(route_id)
    traffic = Traffic(profile, mode=STRUCTURED, seed=seed)

    bus_ids = []
    for n in range(1, buses + 1):
        bus_id = f"SIMHIST-{route_id}-{n}"
        existing = storage.get_bus(bus_id)
        storage.upsert_bus(
            bus_id,
            existing["secret"] if existing else new_secret(),
            route_id=route_id,
            label=f"{route_id} simulated history {n}",
            is_simulated=True,
            active=False,  # training data only: cannot ping, never on the map
        )
        bus_ids.append(bus_id)

    # Training reads the last `days` days *including today*. Today cannot be
    # backfilled (its later fixes would be timestamped in the future), so write
    # the days - 1 before it. An earlier version wrote `days` days ending
    # yesterday, and the oldest always fell outside the training window.
    today = datetime.now(timezone.utc).date()
    written = 0
    for back in range(days - 1, 0, -1):
        day = today - timedelta(days=back)
        rows = []
        for index, bus_id in enumerate(bus_ids):
            for fix in traffic.run_day(day, index, sample_s=sample_s):
                rows.append({
                    "busId": bus_id,
                    "ts": fix.ts,
                    "lat": fix.lat,
                    "lon": fix.lon,
                    "alongM": fix.along_m,
                    "direction": fix.direction,
                    "routeId": route_id,
                    "speedMps": fix.speed_mps,
                    "isSimulated": True,
                })
        written += storage.batch_append_history(rows)
        print(f"    {day}  {len(rows):6,d} fixes", flush=True)
    return written


def train(route_ids: list[str], api: str, admin_key: str, days: int) -> list[dict]:
    """Train through the API when it is up, so the deployed code path is used."""
    headers = {"X-Admin-Key": admin_key} if admin_key else {}
    results = []
    for route_id in route_ids:
        try:
            resp = requests.post(
                f"{api}/manage/learn", params={"routeId": route_id, "days": days},
                headers=headers, timeout=600,
            )
            resp.raise_for_status()
            results.extend(resp.json()["results"])
        except requests.RequestException as exc:
            print(f"  API training unavailable for {route_id} ({exc}); training in-process", flush=True)
            results.append(training.train_route(route_id, days))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--routes", nargs="+", default=None, help="default: every route in tools/data")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--buses", type=int, default=3)
    parser.add_argument("--sample", type=float, default=20.0, help="seconds between simulated fixes")
    parser.add_argument("--seed", type=int, default=20260914)
    parser.add_argument("--api", default=os.environ.get("BUSTRACK_API", "http://127.0.0.1:7071/api"))
    parser.add_argument("--admin-key", default=os.environ.get("ADMIN_KEY", ""))
    parser.add_argument("--no-train", action="store_true", help="write history only")
    args = parser.parse_args()

    route_ids = args.routes or route_ids_in_data()
    print(f"Writing {args.days - 1} days of SIMULATED history (up to yesterday) for {len(route_ids)} route(s) "
          f"to {storage.connection_string()[:45]}...")

    started = time.time()
    for k, route_id in enumerate(route_ids):
        print(f"  {route_id}")
        backfill_route(route_id, args.days, args.buses, args.sample, args.seed + k)
    print(f"History written in {time.time() - started:.0f}s.")

    if args.no_train:
        return 0

    print("\nTraining travel-time models...")
    ok = True
    for result in train(route_ids, args.api, args.admin_key, args.days):
        if result.get("trained"):
            low, high = result["range"]
            print(f"  {result['routeId']:<12} {result['fixes']:7,d} fixes over {result['daysWithData']} days | "
                  f"range {low:.2f}x-{high:.2f}x | trained on {result['trainedOn']} data")
        else:
            ok = False
            print(f"  {result['routeId']:<12} NOT trained: {result.get('reason')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
