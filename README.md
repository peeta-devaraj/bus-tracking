# Bus Tracking

Live bus tracking for a small area, built on Azure. Primary target: **Nagercoil**.

## The problem, stated properly

India tracks trains well and buses barely at all, and the gap is **not technical**.

Trains run about 7,000 vehicles on fixed rails under centralised signalling that
already emits position as a by-product. That data exists whether or not anyone
builds an app on top of it. Buses are roughly two million vehicles spread across
hundreds of unconnected operators, with no central system and nobody paid to
publish anything. **A bus's position does not exist as data anywhere until
somebody creates it.**

So "build a bus tracker" is really three problems:

1. **Ingest** — get a position out of a bus nobody is being paid to instrument.
2. **Trust** — know that a report actually came from the bus it claims to be.
3. **Meaning** — turn a latitude and longitude into "your bus reaches your stop
   in 6 minutes."

This repository is a working answer to all three, small enough to demonstrate
and honest about what it does not know.

> Design reasoning, diagrams, and the decision log live in
> [docs/architecture.md](docs/architecture.md).

## What actually runs

```
 Driver phone (browser) ──HTTPS POST──┐
                                      │
 Simulator (Python)      ─────────────┼──►  Azure Function App (Python)
                                      │       /api/ping    validate, score, store
 [later: GPS hardware ──MQTT──►       │       /api/live    current positions
  Azure IoT Hub ─────────────────────┘       /api/eta     arrival estimates
                                              sweep        timer: retire stale buses
                                                  │
                                                  ▼
                                        Azure Table Storage
                                    Buses · LivePositions · PositionHistory
                                    Routes · Stops · Rejections
                                                  │
 Rider browser ◄────poll every 3s──── Azure Static Web Apps (rider/driver/admin)
```

Every position — phone, simulator, or a future hardware tracker — goes through
the **same ingest endpoint**, where it is identity-checked and plausibility-scored
before it is allowed to become "truth". Adding hardware later means routing IoT
Hub messages into that function, not rebuilding the pipeline.

### Azure services and why each one

| Service | Role | Cost |
|---|---|---|
| **Static Web Apps** | Hosts the three pages; free TLS, which browser GPS requires | Free tier |
| **Functions** (Python, Consumption) | Ingest, queries, ETA, timer sweep | 1M free executions/month |
| **Table Storage** | Registry, live positions, history, routes, stops, reject log | Pennies |
| **Budget alert** | Cost guardrail, created before anything billable | Free |

**Table Storage rather than Cosmos DB.** The partition/row-key model is an exact
fit for "current position of every bus on a route", it needs no free-tier opt-in,
and it provisions reliably on student subscriptions where Cosmos sometimes does
not. Cosmos DB's Table API speaks the same protocol, so outgrowing this is a
connection-string change.

**Polling rather than SignalR.** SignalR's free tier caps at **20 concurrent
connections**, which a classroom would exhaust mid-demo. Polling every 3s has no
such cap and costs nothing extra.

**IoT Hub is deliberately phase 2.** Its free tier allows 8,000 messages/day at
0.5 KB. One bus pinging every 10 seconds consumes 8,640 on its own. That is a
costed engineering decision, not an omission.

## Running it locally

Needs Python 3.11+, Node, Azure Functions Core Tools v4, and Azurite
(`npm install -g azurite`).

```powershell
.\run-local.ps1
```

That starts the storage emulator, the API on `:7071`, and the pages on `:5500`,
then prints the URLs. Stop everything with `.\run-local.ps1 -Stop`.

Then, in another terminal, put something on the map:

```bash
.venv/Scripts/python.exe tools/seed_nagercoil.py
.venv/Scripts/python.exe tools/simulator.py --route NGL-VAD-KKD --buses 3
```

Open <http://localhost:5500/> and buses will be moving.

> **Note on Python versions.** Functions Core Tools v4.13 selects its worker from
> the activated virtualenv, which is why `run-local.ps1` sets `VIRTUAL_ENV`
> before launching. Local development runs 3.14 because that is what Core Tools
> defaults to; Azure is deployed on 3.11. The code uses no version-specific
> features, but keep that in mind if you add dependencies.

## Deploying to Azure

```powershell
az login
.\infra\deploy.ps1
```

It creates the budget alert **first**, then the storage account, Function App
and Static Web App, deploys both halves, and prints the URLs plus a generated
admin key. Save that key: it is the only thing protecting bus secrets.

Tear everything down with `.\infra\deploy.ps1 -Destroy`.

## The three pages

- **`/`** — rider map. Routes, live buses coloured by confidence, stops you can
  tap for arrival estimates.
- **`/driver.html`** — what a driver opens. Reads its bus ID and key from the URL
  fragment so a QR code can carry them, then posts a signed position every 10s.
  Also has **record mode** (below).
- **`/admin.html?key=…`** — register buses, issue QR codes, draw or promote
  routes, and watch the reject log.

## Trust model

This is the part that separates the project from a map with dots on it.

**Identity.** Each bus has its own secret. A position report is accepted only
with a valid HMAC-SHA256 signature over the *exact* request body. Signing the
raw bytes rather than a reconstructed string removes any chance of the browser
and the server disagreeing about number formatting.

**Rejections** — impossible or hostile, never reaches the map:

| Reason | Rule |
|---|---|
| `teleport` | Implied speed since the last fix over 90 km/h |
| `stale_timestamp` / `future_timestamp` | Clock more than 120 s off server time |
| `too_frequent` | More than one fix per 5 s |
| `replayed_nonce` | Nonce already seen for this bus |
| `bad_coords` | Out of range, or 0,0 (an uninitialised GPS, not a bus) |
| `bad_signature` | HMAC mismatch |

**Degradations** — plausible but weak, kept and shown with less confidence:
`off_route` (over 150 m from the assigned route), `coarse_accuracy` (GPS worse
than 100 m), `no_route`.

Off-route reports are **flagged, not rejected**. Real buses take diversions;
discarding those reports would make the system lie by omission.

**What the rider sees.** Freshness dominates everything else:

- 🟢 **Live** — under 30 s old, on route, good accuracy
- 🟡 **Uncertain** — 30 s to 2 min old, or off route, or coarse
- ⚪ **Last known** — over 2 min old, greyed, with an explicit timestamp

A perfect fix from four minutes ago is still only a last-known position. Drawing
it as live is the single behaviour that destroys trust in bus tracking apps, and
it is tested against in `tests/test_validation.py`.

Every rejection is written to a log the admin page displays live. That log is
the evidence the checks actually run rather than merely being described.

## Where route data comes from

Nagercoil has no GTFS feed, no open route list, and no published stop database.
There is nothing accurate to import. So the system **bootstraps its own**:

**Record mode.** A driver — or you, riding once — runs `driver.html` with record
mode on. The trace becomes the route. In `admin.html`, pick that bus under
"Promote a recorded trace", load it onto the map, name it, save it. The system
creates its route data by being used. That is the answer to "how would this ever
work in a town with no data?"

**Road-following routes from OpenStreetMap**, for when nobody can ride the bus.
`tools/build_osm_routes.py` pins every stop to a specific, named OpenStreetMap
feature (Vadasery Bus Stand is `way/227903070` — open it on openstreetmap.org to
check), routes between consecutive stops over the real road network with OSRM,
and writes the result to `tools/data/nagercoil_routes.json`, which is committed.
`tools/seed_nagercoil.py` loads that file, so nothing depends on OSM being
reachable at demo time.

Two details matter:

- **Roadside stops don't bend the route.** A stop pinned to a market or a town
  centre snaps to the nearest lane, and the router drives up that lane and back
  to touch it. The builder detects those out-and-back detours and cuts them out
  (286 m at Vadasery Market, 86 m at Suchindram). Detours into *bus stations*
  are kept, because buses really do drive in.
- **It is labelled `osm-routed`, not surveyed.** It is the road a vehicle would
  take between these stops. Where a real bus uses a different road, a recorded
  trip still beats it.

This replaced hand-placed coordinates that, checked against OpenStreetMap, were
up to 1.45 km out and visited one route's stops in the wrong order.

## Arrival estimates

Remaining distance along the route divided by a speed estimate, reported as a
**range** ("6–11 min"), never a single number. The response says which speed it
used — `reported`, `average`, or `default` — so the interface can explain why an
estimate is weak instead of just showing a wide window.

It returns **nothing** when the bus has already passed the stop or its position
is too stale to reason about. Refusing to answer beats inventing a number.

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

133 tests. Geometry and validation are pure unit tests;
`test_route_data.py` checks the committed Nagercoil routes offline (stops on
their lines, in travel order, no side-lane detours); storage tests run against
Azurite; `test_api_e2e.py` drives the real HTTP surface with real HMAC signing
and **skips itself** if nothing is listening.

`tests/test_api_e2e.py::TestIngestRejects` is the demo script in executable
form — teleporting to Chennai, replaying an old timestamp, tampering with a
signed body, flooding the endpoint. Run it with the admin page open and watch
the reject log fill.

## Known limits

- **Browser GPS stops when the screen locks** or the driver switches apps, on
  both iOS and Android. The page requests a screen wake lock, which helps and
  does not fix it. This is the concrete reason a dedicated tracker is the real
  answer — not a bug that was missed.
- **Web Crypto and Geolocation both need a secure context**, so the driver page
  only works over `https://` or on `localhost`.
- **Only a few routes exist**, and their geometry is the drivable road between
  real stops rather than a surveyed bus path. The claim is "a working system
  demonstrated on real roads", not "Nagercoil is covered".
- **OpenStreetMap tiles** are used directly. That is fine for a classroom demo
  but their tile policy discourages heavier use; Azure Maps is the swap for
  anything real.
- **Bus secrets sit in Table Storage** (encrypted at rest) because the server
  must recompute the HMAC. Azure Key Vault is the documented hardening step.
- **QR codes are rendered by an external service**, so the key travels to that
  service. Acceptable for a demo, not for a deployment.

## Repository layout

```
api/            Azure Functions app
  function_app.py     HTTP + timer triggers
  shared/             geo, auth, validation, eta, storage
web/            rider, driver, admin pages
tools/          simulator, Nagercoil seed, GTFS importer
infra/          deploy.ps1
tests/          unit, storage, and end-to-end suites
docs/           architecture, threat model
```
