"""Azure Table Storage repository.

Table Storage was chosen over Cosmos DB deliberately: its partition/row key
model is an exact fit for "the current position of every bus on a route", it
is available on every subscription without a free-tier opt-in, and it costs
pennies. If the project ever outgrows it, Cosmos DB's Table API speaks the
same protocol and the swap is a connection string.

Partitioning, and why:

  Buses           PK = operator      RK = busId
  LivePositions   PK = routeId       RK = busId      <- one cheap query per route
  PositionHistory PK = busId + date  RK = inverted timestamp  <- newest first
  Routes          PK = city          RK = routeId
  Stops           PK = city          RK = stopId
  Rejections      PK = date          RK = inverted timestamp  <- the reject log

PositionHistory and Rejections use an inverted timestamp as the row key so
that Table Storage's natural ascending order returns the most recent rows
first, which is what every query against them actually wants.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any, Iterable, Iterator

from azure.core.exceptions import (
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.data.tables import TableServiceClient, UpdateMode

# Table names
T_BUSES = "Buses"
T_LIVE = "LivePositions"
T_HISTORY = "PositionHistory"
T_ROUTES = "Routes"
T_STOPS = "Stops"
T_REJECTIONS = "Rejections"

ALL_TABLES = (T_BUSES, T_LIVE, T_HISTORY, T_ROUTES, T_STOPS, T_REJECTIONS)

# Year 3000 in epoch milliseconds, used to invert timestamps for descending order.
_MAX_TS_MS = 32_503_680_000_000

_service: TableServiceClient | None = None
_ensured: set[str] = set()


def connection_string() -> str:
    """Where the data lives.

    Functions sets AzureWebJobsStorage for us; STORAGE_CONNECTION_STRING is an
    override for the simulator and import scripts, which run outside the host.
    """
    conn = (
        os.environ.get("STORAGE_CONNECTION_STRING")
        or os.environ.get("AzureWebJobsStorage")
        or "UseDevelopmentStorage=true"
    )
    return conn


def service() -> TableServiceClient:
    global _service
    if _service is None:
        _service = TableServiceClient.from_connection_string(connection_string())
    return _service


def table(name: str):
    """Get a table client, creating the table on first use."""
    client = service().get_table_client(name)
    if name not in _ensured:
        try:
            client.create_table()
        except ResourceExistsError:
            pass
        _ensured.add(name)
    return client


def ensure_tables() -> None:
    for name in ALL_TABLES:
        table(name)


def _invert(ts: float) -> str:
    """Row key that sorts newest-first under Table Storage's ascending order.

    Millisecond resolution plus a random suffix. Both parts matter: the
    rejection log keeps a whole day in one partition, so several buses can be
    rejected inside the same millisecond, and a colliding row key would make
    the append fail with a 409 instead of recording the event.
    """
    inverted = int(_MAX_TS_MS - ts * 1000)
    return f"{inverted:015d}_{secrets.token_hex(3)}"


# --------------------------------------------------------------------------
# Buses
# --------------------------------------------------------------------------

def get_bus(bus_id: str, operator: str = "default") -> dict[str, Any] | None:
    try:
        entity = table(T_BUSES).get_entity(operator, bus_id)
    except ResourceNotFoundError:
        return None
    return dict(entity)


def upsert_bus(
    bus_id: str,
    secret: str,
    route_id: str = "",
    label: str = "",
    is_simulated: bool = False,
    active: bool = True,
    operator: str = "default",
) -> dict[str, Any]:
    entity = {
        "PartitionKey": operator,
        "RowKey": bus_id,
        "secret": secret,
        "routeId": route_id,
        "label": label or bus_id,
        "isSimulated": is_simulated,
        "active": active,
        "registeredAt": time.time(),
    }
    table(T_BUSES).upsert_entity(entity, mode=UpdateMode.MERGE)
    return entity


def list_buses(operator: str = "default") -> list[dict[str, Any]]:
    query = f"PartitionKey eq '{operator}'"
    return [dict(e) for e in table(T_BUSES).query_entities(query)]


def delete_bus(bus_id: str, operator: str = "default") -> None:
    try:
        table(T_BUSES).delete_entity(operator, bus_id)
    except ResourceNotFoundError:
        pass


# --------------------------------------------------------------------------
# Live positions
# --------------------------------------------------------------------------

def _live_partition(route_id: str) -> str:
    # A bus in recording mode has no route yet, but still needs a partition.
    return route_id or "_unassigned"


def get_live_position(bus_id: str, route_id: str) -> dict[str, Any] | None:
    try:
        entity = table(T_LIVE).get_entity(_live_partition(route_id), bus_id)
    except ResourceNotFoundError:
        return None
    return dict(entity)


def upsert_live_position(
    bus_id: str,
    route_id: str,
    lat: float,
    lon: float,
    ts: float,
    *,
    accuracy_m: float | None = None,
    speed_mps: float | None = None,
    heading: float | None = None,
    flags: Iterable[str] = (),
    along_m: float | None = None,
    offset_m: float | None = None,
    is_simulated: bool = False,
    label: str = "",
    recent_nonces: Iterable[str] = (),
) -> dict[str, Any]:
    entity = {
        "PartitionKey": _live_partition(route_id),
        "RowKey": bus_id,
        "busId": bus_id,
        "routeId": route_id,
        "lat": float(lat),
        "lon": float(lon),
        "ts": float(ts),
        "accuracyM": float(accuracy_m) if accuracy_m is not None else None,
        "speedMps": float(speed_mps) if speed_mps is not None else None,
        "heading": float(heading) if heading is not None else None,
        "flags": json.dumps(list(flags)),
        "alongM": float(along_m) if along_m is not None else None,
        "offsetM": float(offset_m) if offset_m is not None else None,
        "isSimulated": bool(is_simulated),
        "label": label or bus_id,
        "recentNonces": json.dumps(list(recent_nonces)[-20:]),
        "updatedAt": time.time(),
    }
    table(T_LIVE).upsert_entity(entity, mode=UpdateMode.REPLACE)
    return entity


def list_live_positions(route_id: str | None = None) -> list[dict[str, Any]]:
    client = table(T_LIVE)
    if route_id:
        entities = client.query_entities(f"PartitionKey eq '{_live_partition(route_id)}'")
    else:
        entities = client.list_entities()
    return [_decode_live(dict(e)) for e in entities]


def _decode_live(entity: dict[str, Any]) -> dict[str, Any]:
    entity["flags"] = json.loads(entity.get("flags") or "[]")
    entity["recentNonces"] = json.loads(entity.get("recentNonces") or "[]")
    return entity


def delete_live_position(bus_id: str, route_id: str) -> None:
    try:
        table(T_LIVE).delete_entity(_live_partition(route_id), bus_id)
    except ResourceNotFoundError:
        pass


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

def append_history(bus_id: str, ts: float, lat: float, lon: float, **extra: Any) -> None:
    day = time.strftime("%Y%m%d", time.gmtime(ts))
    entity = {
        "PartitionKey": f"{bus_id}_{day}",
        "RowKey": _invert(ts),
        "busId": bus_id,
        "ts": float(ts),
        "lat": float(lat),
        "lon": float(lon),
    }
    for key, value in extra.items():
        if value is not None and not isinstance(value, (list, dict)):
            entity[key] = value
    table(T_HISTORY).create_entity(entity)


def recent_history(bus_id: str, day: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    day = day or time.strftime("%Y%m%d", time.gmtime())
    query = f"PartitionKey eq '{bus_id}_{day}'"
    rows = table(T_HISTORY).query_entities(query, results_per_page=limit)
    out: list[dict[str, Any]] = []
    for entity in rows:
        out.append(dict(entity))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# Routes and stops
# --------------------------------------------------------------------------

def upsert_route(
    route_id: str,
    name: str,
    polyline: str,
    city: str = "nagercoil",
    stop_ids: Iterable[str] = (),
    source: str = "manual",
) -> dict[str, Any]:
    entity = {
        "PartitionKey": city,
        "RowKey": route_id,
        "routeId": route_id,
        "name": name,
        "polyline": polyline,
        "stopIds": json.dumps(list(stop_ids)),
        "source": source,
        "updatedAt": time.time(),
    }
    table(T_ROUTES).upsert_entity(entity, mode=UpdateMode.REPLACE)
    return entity


def get_route(route_id: str, city: str = "nagercoil") -> dict[str, Any] | None:
    try:
        entity = dict(table(T_ROUTES).get_entity(city, route_id))
    except ResourceNotFoundError:
        return None
    entity["stopIds"] = json.loads(entity.get("stopIds") or "[]")
    return entity


def find_route_anywhere(route_id: str) -> dict[str, Any] | None:
    """Look up a route without knowing its city.

    The ingest path only receives a routeId, so it needs this. Cities are few,
    so a cross-partition query here is cheap.
    """
    rows = list(table(T_ROUTES).query_entities(f"RowKey eq '{route_id}'"))
    if not rows:
        return None
    entity = dict(rows[0])
    entity["stopIds"] = json.loads(entity.get("stopIds") or "[]")
    return entity


def list_routes(city: str | None = None) -> list[dict[str, Any]]:
    client = table(T_ROUTES)
    entities = (
        client.query_entities(f"PartitionKey eq '{city}'") if city else client.list_entities()
    )
    out = []
    for e in entities:
        entity = dict(e)
        entity["stopIds"] = json.loads(entity.get("stopIds") or "[]")
        out.append(entity)
    return out


def delete_route(route_id: str, city: str = "nagercoil") -> None:
    try:
        table(T_ROUTES).delete_entity(city, route_id)
    except ResourceNotFoundError:
        pass


def upsert_stop(
    stop_id: str,
    name: str,
    lat: float,
    lon: float,
    city: str = "nagercoil",
    route_ids: Iterable[str] = (),
) -> dict[str, Any]:
    entity = {
        "PartitionKey": city,
        "RowKey": stop_id,
        "stopId": stop_id,
        "name": name,
        "lat": float(lat),
        "lon": float(lon),
        "routeIds": json.dumps(list(route_ids)),
    }
    table(T_STOPS).upsert_entity(entity, mode=UpdateMode.REPLACE)
    return entity


def list_stops(city: str | None = None) -> list[dict[str, Any]]:
    client = table(T_STOPS)
    entities = (
        client.query_entities(f"PartitionKey eq '{city}'") if city else client.list_entities()
    )
    out = []
    for e in entities:
        entity = dict(e)
        entity["routeIds"] = json.loads(entity.get("routeIds") or "[]")
        out.append(entity)
    return out


def get_stop(stop_id: str, city: str = "nagercoil") -> dict[str, Any] | None:
    try:
        entity = dict(table(T_STOPS).get_entity(city, stop_id))
    except ResourceNotFoundError:
        return None
    entity["routeIds"] = json.loads(entity.get("routeIds") or "[]")
    return entity


def batch_upsert_routes(routes: list[dict[str, Any]], city: str) -> int:
    """Bulk route insert for the GTFS importer.

    Same 100-operation, single-partition batch limit as stops. Since every
    route in one import belongs to one city, the partition constraint is
    satisfied for free.
    """
    written = 0
    client = table(T_ROUTES)
    for i in range(0, len(routes), 100):
        chunk = routes[i : i + 100]
        operations = [
            (
                "upsert",
                {
                    "PartitionKey": city,
                    "RowKey": r["routeId"],
                    "routeId": r["routeId"],
                    "name": r["name"],
                    "polyline": r["polyline"],
                    "stopIds": json.dumps(r.get("stopIds", [])),
                    "source": r.get("source", "gtfs"),
                    "updatedAt": time.time(),
                },
            )
            for r in chunk
        ]
        client.submit_transaction(operations)
        written += len(chunk)
    return written


def batch_upsert_stops(stops: list[dict[str, Any]], city: str) -> int:
    """Bulk stop insert for the GTFS importer.

    Table Storage batches are limited to 100 operations and must stay inside
    one partition, which suits us since a batch is always one city.
    """
    written = 0
    client = table(T_STOPS)
    for i in range(0, len(stops), 100):
        chunk = stops[i : i + 100]
        operations = []
        for stop in chunk:
            operations.append(
                (
                    "upsert",
                    {
                        "PartitionKey": city,
                        "RowKey": stop["stopId"],
                        "stopId": stop["stopId"],
                        "name": stop["name"],
                        "lat": float(stop["lat"]),
                        "lon": float(stop["lon"]),
                        "routeIds": json.dumps(stop.get("routeIds", [])),
                    },
                )
            )
        client.submit_transaction(operations)
        written += len(chunk)
    return written


# --------------------------------------------------------------------------
# Rejection log -- what the admin page shows to prove the checks are real
# --------------------------------------------------------------------------

def record_rejection(bus_id: str, reason: str, detail: str, ts: float | None = None) -> None:
    ts = ts if ts is not None else time.time()
    table(T_REJECTIONS).create_entity(
        {
            "PartitionKey": time.strftime("%Y%m%d", time.gmtime(ts)),
            "RowKey": _invert(ts),
            "busId": bus_id,
            "reason": reason,
            "detail": detail[:512],
            "ts": ts,
        }
    )


def recent_rejections(limit: int = 50) -> list[dict[str, Any]]:
    day = time.strftime("%Y%m%d", time.gmtime())
    rows = table(T_REJECTIONS).query_entities(
        f"PartitionKey eq '{day}'", results_per_page=limit
    )
    out: list[dict[str, Any]] = []
    for entity in rows:
        out.append(dict(entity))
        if len(out) >= limit:
            break
    return out
