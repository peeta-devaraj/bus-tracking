"""HTTP surface for the bus tracking system.

Azure Functions, Python v2 programming model. One ingest endpoint that
everything writes through, a handful of read endpoints the rider and admin
pages call, a timer that sweeps stale buses, and a nightly job that learns
travel times from history.

The ingest endpoint is anonymous at the platform level on purpose: a bus
authenticates with its own HMAC signature, not with a shared function key that
would end up pasted into every driver's browser.
"""

from __future__ import annotations

import json
import logging
import os
import time

import azure.functions as func

from shared import storage, training
from shared.auth import BUS_ID_HEADER, SIGNATURE_HEADER, new_secret, verify
from shared.eta import (
    average_speed_kmh_from_history,
    display_range,
    estimate,
    infer_direction,
    range_text,
)
from shared.geo import decode_polyline, encode_polyline, route_length_m, snap_to_route
from shared.validation import (
    Fix,
    confidence_for,
    validate_fix,
)

app = func.FunctionApp()

log = logging.getLogger("bustrack")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": os.environ.get("BUSTRACK_ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Headers": f"Content-Type,{SIGNATURE_HEADER},{BUS_ID_HEADER},X-Admin-Key",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
    "Access-Control-Max-Age": "3600",
}


def _json(payload: object, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, default=str),
        status_code=status,
        mimetype="application/json",
        headers=dict(CORS_HEADERS),
    )


def _preflight() -> func.HttpResponse:
    return func.HttpResponse("", status_code=204, headers=dict(CORS_HEADERS))


def _is_admin(req: func.HttpRequest) -> bool:
    """Guard for endpoints that hand out secrets or rewrite routes.

    If ADMIN_KEY is unset we are running locally against the emulator, where
    demanding a key only slows development down. It is required in Azure --
    see infra/deploy.ps1, which always sets one.
    """
    expected = os.environ.get("ADMIN_KEY")
    if not expected:
        return True
    return req.headers.get("X-Admin-Key", "") == expected


# --------------------------------------------------------------------------
# Ingest -- the one door every position report comes through
# --------------------------------------------------------------------------

@app.route(route="ping", methods=["POST", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def ping(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _preflight()

    raw = req.get_body()

    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return _json({"error": "body must be JSON"}, 400)

    bus_id = req.headers.get(BUS_ID_HEADER) or body.get("busId")
    if not bus_id:
        return _json({"error": "missing bus id"}, 400)

    bus = storage.get_bus(bus_id)
    if bus is None:
        # Deliberately vague: an unregistered caller learns nothing about
        # which bus ids exist.
        return _json({"error": "unknown or unauthorised bus"}, 401)
    if not bus.get("active", True):
        return _json({"error": "bus is not active"}, 403)

    signature = req.headers.get(SIGNATURE_HEADER, "")
    if not verify(bus["secret"], raw, signature):
        storage.record_rejection(bus_id, "bad_signature", "HMAC mismatch")
        return _json({"error": "signature does not match", "reason": "bad_signature"}, 401)

    try:
        fix = Fix(
            bus_id=bus_id,
            lat=float(body["lat"]),
            lon=float(body["lon"]),
            ts=float(body.get("ts") or time.time()),
            accuracy_m=_opt_float(body.get("accuracy")),
            speed_mps=_opt_float(body.get("speed")),
            heading=_opt_float(body.get("heading")),
            nonce=str(body.get("nonce") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _json({"error": f"bad fix payload: {exc}"}, 400)

    # Recording mode intentionally bypasses the assigned route so a driver can
    # trace a route that does not exist yet.
    recording = bool(body.get("record"))
    route_id = "" if recording else str(bus.get("routeId") or "")

    route_points = None
    if route_id:
        route = storage.find_route_anywhere(route_id)
        if route and route.get("polyline"):
            route_points = decode_polyline(route["polyline"])

    previous_entity = storage.get_live_position(bus_id, route_id)
    previous_fix = None
    seen_nonces: set[str] = set()
    if previous_entity:
        previous_fix = Fix(
            bus_id=bus_id,
            lat=previous_entity["lat"],
            lon=previous_entity["lon"],
            ts=previous_entity["ts"],
        )
        seen_nonces = set(previous_entity.get("recentNonces") or [])

    verdict = validate_fix(
        fix,
        previous=previous_fix,
        route=route_points,
        seen_nonces=seen_nonces,
    )

    if not verdict.accepted:
        storage.record_rejection(bus_id, verdict.reason or "unknown", verdict.detail)
        log.warning("rejected fix from %s: %s (%s)", bus_id, verdict.reason, verdict.detail)
        return _json(
            {
                "accepted": False,
                "reason": verdict.reason,
                "detail": verdict.detail,
            },
            422,
        )

    nonces = list(seen_nonces)
    if fix.nonce:
        nonces.append(fix.nonce)

    along_m = verdict.snap.along_m if verdict.snap else None
    previous_direction = int((previous_entity or {}).get("direction") or 0)
    previous_anchor = (previous_entity or {}).get("dirAnchorM")
    direction, anchor = infer_direction(previous_anchor, previous_direction, along_m)

    storage.upsert_live_position(
        bus_id=bus_id,
        route_id=route_id,
        lat=fix.lat,
        lon=fix.lon,
        ts=fix.ts,
        accuracy_m=fix.accuracy_m,
        speed_mps=fix.speed_mps,
        heading=fix.heading,
        flags=verdict.flags,
        along_m=along_m,
        offset_m=verdict.snap.offset_m if verdict.snap else None,
        is_simulated=bool(bus.get("isSimulated")),
        label=str(bus.get("label") or bus_id),
        recent_nonces=nonces,
        direction=direction,
        dir_anchor_m=anchor,
    )

    storage.append_history(
        bus_id,
        fix.ts,
        fix.lat,
        fix.lon,
        speedMps=fix.speed_mps,
        heading=fix.heading,
        accuracyM=fix.accuracy_m,
        alongM=along_m,
        # Which route and which way: what the travel-time model learns from.
        routeId=route_id or None,
        direction=direction,
        isSimulated=bool(bus.get("isSimulated")),
    )

    return _json(
        {
            "accepted": True,
            "flags": verdict.flags,
            "quality": verdict.quality,
            "offsetM": round(verdict.snap.offset_m, 1) if verdict.snap else None,
            "alongM": round(verdict.snap.along_m, 1) if verdict.snap else None,
            "direction": direction,
            "recording": recording,
        }
    )


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Read endpoints
# --------------------------------------------------------------------------

@app.route(route="live", methods=["GET", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def live(req: func.HttpRequest) -> func.HttpResponse:
    """Current position of every bus, optionally filtered by route or bbox."""
    if req.method == "OPTIONS":
        return _preflight()

    route_id = req.params.get("routeId")
    now = time.time()

    positions = storage.list_live_positions(route_id)
    bbox = _parse_bbox(req.params.get("bbox"))

    out = []
    for p in positions:
        if bbox and not (
            bbox[0] <= p["lat"] <= bbox[2] and bbox[1] <= p["lon"] <= bbox[3]
        ):
            continue

        age = now - float(p["ts"])
        out.append(
            {
                "busId": p["busId"],
                "label": p.get("label") or p["busId"],
                "routeId": p.get("routeId") or "",
                "lat": p["lat"],
                "lon": p["lon"],
                "heading": p.get("heading"),
                "speedMps": p.get("speedMps"),
                "ageS": round(age, 1),
                "ts": p["ts"],
                "confidence": confidence_for(age, p.get("flags") or []),
                "flags": p.get("flags") or [],
                "isSimulated": bool(p.get("isSimulated")),
                "alongM": p.get("alongM"),
                "direction": int(p.get("direction") or 0),
            }
        )

    out.sort(key=lambda b: b["ageS"])
    return _json({"serverTime": now, "count": len(out), "buses": out})


def _parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    if not raw:
        return None
    try:
        parts = [float(x) for x in raw.split(",")]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    min_lat, min_lon, max_lat, max_lon = parts
    return (min(min_lat, max_lat), min(min_lon, max_lon), max(min_lat, max_lat), max(min_lon, max_lon))


@app.route(route="routes", methods=["GET", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def routes(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _preflight()

    city = req.params.get("city")
    include_geometry = req.params.get("geometry", "1") != "0"

    # Asking for one route by id returns just that route, with geometry. This
    # is what lets a client holding a 4,000-route city fetch the single
    # polyline it is about to draw instead of all of them.
    wanted = req.params.get("routeId")

    # stopIds is long (up to 200 per route) and no caller needs it just to
    # populate a menu, so it is opt-in. On the imported Chennai network,
    # leaving it out is the difference between a 1.2 MB list and a 0.2 MB one.
    include_stops = req.params.get("detail") == "1"

    out = []
    for r in storage.list_routes(city):
        if wanted and r["routeId"] != wanted:
            continue
        entry = {
            "routeId": r["routeId"],
            "name": r.get("name") or r["routeId"],
            "city": r["PartitionKey"],
            "source": r.get("source", "manual"),
        }
        if include_stops:
            entry["stopIds"] = r.get("stopIds", [])
        if include_geometry or wanted:
            entry["polyline"] = r.get("polyline", "")
        out.append(entry)

    out.sort(key=lambda r: r["name"])
    return _json({"count": len(out), "routes": out})


@app.route(route="stops", methods=["GET", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def stops(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _preflight()

    city = req.params.get("city")
    route_id = req.params.get("routeId")

    out = []
    for s in storage.list_stops(city):
        if route_id and route_id not in (s.get("routeIds") or []):
            continue
        out.append(
            {
                "stopId": s["stopId"],
                "name": s.get("name") or s["stopId"],
                "lat": s["lat"],
                "lon": s["lon"],
                "city": s["PartitionKey"],
                "routeIds": s.get("routeIds", []),
            }
        )

    return _json({"count": len(out), "stops": out})


@app.route(route="eta", methods=["GET", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def eta(req: func.HttpRequest) -> func.HttpResponse:
    """When does the next bus reach this stop?

    Returns an empty list rather than a guess when nothing can be said, and
    always reports which buses were considered so the rider can tell the
    difference between "no bus is coming" and "no bus is being tracked".

    Each arrival says how it was estimated. `method` is "speed" for remaining
    distance over the bus's speed, or "learned" when a travel-time model
    trained on history priced most of the remaining road. A learned arrival
    also carries `trainedOn` -- "simulated", "real" or "mixed" -- which clients
    must show: a model learned from simulated buses knows nothing about real
    traffic.
    """
    if req.method == "OPTIONS":
        return _preflight()

    stop_id = req.params.get("stopId")
    if not stop_id:
        return _json({"error": "stopId is required"}, 400)

    city = req.params.get("city", "nagercoil")
    stop = storage.get_stop(stop_id, city)
    if stop is None:
        return _json({"error": f"unknown stop {stop_id}"}, 404)

    now = time.time()
    arrivals = []
    tracked_buses = 0

    for route_id in stop.get("routeIds") or []:
        route = storage.find_route_anywhere(route_id)
        if not route or not route.get("polyline"):
            continue

        points = decode_polyline(route["polyline"])
        stop_snap = snap_to_route((stop["lat"], stop["lon"]), points)
        if stop_snap is None:
            continue

        total_length = route_length_m(points)
        model, model_meta = training.get_model(route_id)

        for bus in storage.list_live_positions(route_id):
            tracked_buses += 1
            age = now - float(bus["ts"])
            history = storage.recent_history(bus["busId"])

            result = estimate(
                bus_id=bus["busId"],
                label=bus.get("label") or bus["busId"],
                bus_along_m=bus.get("alongM"),
                stop_along_m=stop_snap.along_m,
                age_s=age,
                confidence=confidence_for(age, bus.get("flags") or []),
                reported_speed_mps=bus.get("speedMps"),
                average_speed_kmh=average_speed_kmh_from_history(history),
                is_simulated=bool(bus.get("isSimulated")),
                route_length_m=total_length,
                loops=False,
                direction=int(bus.get("direction") or 0),
            )
            if result is None:
                continue

            entry = result.to_dict()
            entry["routeId"] = route_id
            entry["routeName"] = route.get("name") or route_id
            entry["method"] = "speed"

            # A learned model replaces the range when it covers most of the
            # remaining road. It falls back to the same speed the naive
            # estimate used for any stretch it has no data on.
            if model is not None and result.speed_source != "arrived" and result.speed_kmh_used > 0:
                prediction = model.predict(
                    int(bus.get("direction") or 0),
                    float(bus["alongM"]),
                    stop_snap.along_m,
                    now,
                    3.6 / result.speed_kmh_used,
                )
                if prediction is not None and prediction.learned_share >= 0.5:
                    low, high = display_range(prediction.low_seconds, prediction.high_seconds)
                    entry.update({
                        "lowMin": low,
                        "highMin": high,
                        "text": range_text(low, high),
                        "method": "learned",
                        "learnedShare": round(prediction.learned_share, 2),
                        "trainedOn": training.trained_on(float((model_meta or {}).get("simulatedShare", 1.0))),
                    })

            arrivals.append(entry)

    arrivals.sort(key=lambda a: a["lowMin"])

    return _json(
        {
            "stop": {"stopId": stop["stopId"], "name": stop.get("name"), "lat": stop["lat"], "lon": stop["lon"]},
            "serverTime": now,
            "busesTracked": tracked_buses,
            "arrivals": arrivals,
        }
    )


@app.route(route="rejections", methods=["GET", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def rejections(req: func.HttpRequest) -> func.HttpResponse:
    """Recent rejected reports. This is the evidence that the checks are real."""
    if req.method == "OPTIONS":
        return _preflight()

    limit = min(int(req.params.get("limit", 50)), 200)
    rows = storage.recent_rejections(limit)
    return _json(
        {
            "count": len(rows),
            "rejections": [
                {
                    "busId": r.get("busId"),
                    "reason": r.get("reason"),
                    "detail": r.get("detail"),
                    "ts": r.get("ts"),
                }
                for r in rows
            ],
        }
    )


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    try:
        storage.ensure_tables()
        return _json({"ok": True, "time": time.time()})
    except Exception as exc:  # surfaced deliberately; this is the smoke test
        log.exception("health check failed")
        return _json({"ok": False, "error": str(exc)}, 503)


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

@app.route(route="manage/buses", methods=["GET", "POST", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def admin_buses(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _preflight()
    if not _is_admin(req):
        return _json({"error": "admin key required"}, 401)

    if req.method == "GET":
        buses = []
        for b in storage.list_buses():
            buses.append(
                {
                    "busId": b["RowKey"],
                    "label": b.get("label"),
                    "routeId": b.get("routeId", ""),
                    "isSimulated": bool(b.get("isSimulated")),
                    "active": bool(b.get("active", True)),
                    # The secret is returned so the admin page can build the
                    # driver QR code. This endpoint is admin-key protected in
                    # Azure precisely because of this field.
                    "secret": b.get("secret"),
                }
            )
        return _json({"count": len(buses), "buses": buses})

    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "body must be JSON"}, 400)

    bus_id = str(body.get("busId") or "").strip()
    if not bus_id:
        return _json({"error": "busId is required"}, 400)

    existing = storage.get_bus(bus_id)
    secret = (existing or {}).get("secret") or new_secret()

    entity = storage.upsert_bus(
        bus_id=bus_id,
        secret=secret,
        route_id=str(body.get("routeId") or ""),
        label=str(body.get("label") or bus_id),
        is_simulated=bool(body.get("isSimulated")),
        active=bool(body.get("active", True)),
    )

    return _json(
        {
            "busId": bus_id,
            "label": entity["label"],
            "routeId": entity["routeId"],
            "secret": secret,
            "isSimulated": entity["isSimulated"],
            "created": existing is None,
        }
    )


@app.route(route="manage/buses/{busId}", methods=["DELETE", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def admin_delete_bus(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _preflight()
    if not _is_admin(req):
        return _json({"error": "admin key required"}, 401)

    bus_id = req.route_params.get("busId", "")
    # Every partition, not just the current route: a bus that recorded a trace
    # or changed route would otherwise stay on the map as a ghost.
    storage.delete_all_live_positions(bus_id)
    storage.delete_bus(bus_id)
    return _json({"deleted": bus_id})


@app.route(route="manage/routes", methods=["POST", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def admin_save_route(req: func.HttpRequest) -> func.HttpResponse:
    """Save a route, either hand-drawn on the admin map or from a recorded trace."""
    if req.method == "OPTIONS":
        return _preflight()
    if not _is_admin(req):
        return _json({"error": "admin key required"}, 401)

    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "body must be JSON"}, 400)

    route_id = str(body.get("routeId") or "").strip()
    if not route_id:
        return _json({"error": "routeId is required"}, 400)

    polyline = body.get("polyline")
    if not polyline:
        points = body.get("points") or []
        if len(points) < 2:
            return _json({"error": "need a polyline or at least two points"}, 400)
        polyline = encode_polyline([(float(p[0]), float(p[1])) for p in points])

    city = str(body.get("city") or "nagercoil")
    entity = storage.upsert_route(
        route_id=route_id,
        name=str(body.get("name") or route_id),
        polyline=polyline,
        city=city,
        stop_ids=body.get("stopIds") or [],
        source=str(body.get("source") or "manual"),
    )

    points = decode_polyline(polyline)
    return _json(
        {
            "routeId": route_id,
            "name": entity["name"],
            "city": city,
            "vertices": len(points),
            "lengthM": round(route_length_m(points)),
        }
    )


@app.route(route="manage/stops", methods=["POST", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def admin_save_stop(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _preflight()
    if not _is_admin(req):
        return _json({"error": "admin key required"}, 401)

    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "body must be JSON"}, 400)

    stop_id = str(body.get("stopId") or "").strip()
    if not stop_id:
        return _json({"error": "stopId is required"}, 400)

    entity = storage.upsert_stop(
        stop_id=stop_id,
        name=str(body.get("name") or stop_id),
        lat=float(body["lat"]),
        lon=float(body["lon"]),
        city=str(body.get("city") or "nagercoil"),
        route_ids=body.get("routeIds") or [],
    )
    return _json({"stopId": stop_id, "name": entity["name"]})


@app.route(route="manage/trace/{busId}", methods=["GET", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def admin_trace(req: func.HttpRequest) -> func.HttpResponse:
    """The raw recorded trace for a bus, for turning into a route."""
    if req.method == "OPTIONS":
        return _preflight()
    if not _is_admin(req):
        return _json({"error": "admin key required"}, 401)

    bus_id = req.route_params.get("busId", "")
    limit = min(int(req.params.get("limit", 1000)), 5000)
    rows = storage.recent_history(bus_id, req.params.get("day"), limit)

    # History comes back newest-first; a trace reads better oldest-first.
    rows.sort(key=lambda r: float(r["ts"]))
    points = [(r["lat"], r["lon"]) for r in rows]

    return _json(
        {
            "busId": bus_id,
            "count": len(points),
            "polyline": encode_polyline(points) if len(points) >= 2 else "",
            "lengthM": round(route_length_m(points)) if len(points) >= 2 else 0,
            "points": [[r["lat"], r["lon"], r["ts"]] for r in rows],
        }
    )


@app.route(route="manage/learn", methods=["POST", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def admin_learn(req: func.HttpRequest) -> func.HttpResponse:
    """Train travel-time models now, instead of waiting for the nightly run.

    ?routeId=X trains one route; without it, every route that has a bus
    assigned. ?days=N sets how much history to use (default 14, max 60).
    """
    if req.method == "OPTIONS":
        return _preflight()
    if not _is_admin(req):
        return _json({"error": "admin key required"}, 401)

    try:
        days = max(1, min(int(req.params.get("days", training.HISTORY_DAYS)), 60))
    except ValueError:
        return _json({"error": "days must be a number"}, 400)

    route_id = req.params.get("routeId")
    route_ids = [route_id] if route_id else sorted(
        {b.get("routeId") for b in storage.list_buses() if b.get("routeId")}
    )
    return _json({"results": [training.train_route(rid, days) for rid in route_ids]})


# 22:00 UTC is 03:30 in Nagercoil: after the last bus, before the first.
@app.timer_trigger(schedule="0 0 22 * * *", arg_name="timer", run_on_startup=False)
def learn_nightly(timer: func.TimerRequest) -> None:
    """Retrain every route's travel-time model from the last two weeks."""
    route_ids = sorted({b.get("routeId") for b in storage.list_buses() if b.get("routeId")})
    for route_id in route_ids:
        try:
            training.train_route(route_id)
        except Exception:  # one bad route must not stop the others training
            log.exception("nightly training failed for %s", route_id)


# --------------------------------------------------------------------------
# Timer: retire buses that stopped reporting
# --------------------------------------------------------------------------

@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False)
def sweep(timer: func.TimerRequest) -> None:
    """Drop live rows for buses that have gone quiet for a long time.

    Confidence scoring already stops a stale bus from being shown as live, so
    this is only housekeeping: it keeps the live table small and stops
    yesterday's buses from cluttering today's map.
    """
    max_age = float(os.environ.get("BUSTRACK_SWEEP_MAX_AGE_S", 3600))
    now = time.time()
    removed = 0

    for position in storage.list_live_positions():
        if now - float(position["ts"]) > max_age:
            storage.delete_live_position(position["busId"], position.get("routeId") or "")
            removed += 1

    if removed:
        log.info("sweep removed %d stale live position(s)", removed)
