# Presentation guide — Bus Tracker (Nagercoil)

Everything you need for today: the links, what to say, the demo step by step,
likely questions with answers, what it costs, and how to take it down.

---

## 1. Links (deployed on Azure, 24 Sept 2026)

| What | URL |
|---|---|
| **Rider map** (show this first) | https://zealous-meadow-0a54d1100.3.azurestaticapps.net/ |
| **Driver page** | https://zealous-meadow-0a54d1100.3.azurestaticapps.net/driver.html |
| **Admin page** | printed by `.\infra\demo.ps1` (it includes the secret admin key; don't show the key on the projector for long) |
| **API health check** | https://bustrack-api-b7016d.azurewebsites.net/api/health |

Azure resources, all in resource group **`bustrack-rg`**:

| Resource | Azure service | Region |
|---|---|---|
| `bustrack-web-b7016d` | Static Web Apps (Free tier) | East Asia |
| `bustrack-api-b7016d` | Function App, Python 3.11, Linux Consumption plan | Central India |
| `bustrackb7016d` | Storage account (Table Storage) | Central India |
| `student-credit-guard` | Budget alert, ₹1,000/month, whole subscription | — |

### Before you present (5 minutes before)

1. Open PowerShell in the project folder and run:
   ```powershell
   .\infra\demo.ps1
   ```
   Two windows open: 3 simulated buses on the town route and 2 on the
   Kanyakumari route. **Leave them open during the demo.** Closing them stops
   the buses. The script also prints the admin link.
2. Open the rider map and the admin link in two browser tabs. The map tiles
   take a few seconds the first time, so load them before you're in front of
   the class.
3. Don't let the laptop sleep. The simulated buses run on your laptop.

---

## 2. The one-minute pitch

> India tracks trains well but hardly tracks buses at all, and the reason isn't
> technical. Trains are about 7,000 vehicles run by one organisation with central
> signalling, so their position already exists as data. Buses are about two
> million vehicles run by hundreds of unconnected operators. **Nobody records a
> bus's position until someone builds a way to.**
>
> So a bus tracker is really three problems:
> 1. **Ingest:** getting a position out of a bus that nobody is paid to fit with equipment.
> 2. **Trust:** making sure a report really came from the bus it claims to be.
> 3. **Meaning:** turning a latitude and longitude into "your bus reaches your stop in 6 minutes."
>
> I built all three on Azure for Nagercoil. The routes follow real roads from
> OpenStreetMap, and every position report has to pass security and
> plausibility checks before it can appear on the map.

---

## 3. How it works

```
 Driver's phone (browser) ─┐   signed HTTPS POST
 Simulator (Python)       ─┼──────────────────────►  Azure Functions (Python)
 [future: GPS tracker via  │                           /ping   check + store
  Azure IoT Hub] ──────────┘                           /live   bus positions
                                                       /eta    arrival estimates
                                                       timers: sweep, nightly learning, cleanup
                                                            │
                                                            ▼
                                                    Azure Table Storage
                                    Buses · LivePositions · PositionHistory
                                    Routes · Stops · Rejections · travel-time models
                                                            ▲
 Rider's browser ──── polls every 3 s ────────────────────┘
        ▲
        └── pages served by Azure Static Web Apps (free HTTPS)
```

**Key point: there is one way in.** The phone, the simulator and any future
hardware all go through the same `/api/ping` endpoint, so they all get the same
checks. To add a hardware GPS tracker later, you'd send Azure IoT Hub messages
into that same function. Nothing else would need rebuilding.

### Why each Azure service (and what was rejected)

| Choice | Instead of | Why |
|---|---|---|
| **Azure** | AWS | The student credit has no card attached, so it stops instead of billing. No surprise charges. |
| **Functions, Consumption plan** | A virtual machine | Scales to zero and bills only while code runs. A VM running a web server isn't really a cloud architecture. |
| **Table Storage** | Cosmos DB | The key design fits exactly (for example, "all buses on route X" reads one partition). Cosmos's free tier often fails to set up on student subscriptions. Cosmos's Table API speaks the same protocol, so switching later only means changing the connection string. |
| **Polling every 3 s** | SignalR (push) | SignalR's free tier allows only **20 connections**, so a class of 30 would break it in the middle of a demo. Polling has no cap. |
| **IoT Hub: phase 2** | IoT Hub now | Its free tier allows 8,000 messages/day. One bus pinging every 10 s sends **8,640**. Leaving it out was a cost decision, and the ingest was designed so it can be added later. |
| **Static Web Apps** | — | Free, with HTTPS included. The phone's GPS and crypto APIs only work over HTTPS. |
| **Budget alert first** | — | Created before anything that costs money. It emails at 50% and 90% of ₹1,000. |

---

## 4. Trust: the part that makes this more than dots on a map

**Every bus has its own secret key.** Each position report is signed with
HMAC-SHA256 over the exact bytes of the request. Without the right signature,
the report is rejected.

**Rejected** (impossible or hostile; never reaches the map, always logged):

| Reason | Rule |
|---|---|
| `bad_signature` | Signature doesn't match |
| `teleport` | Speed implied since the last report is over 90 km/h |
| `stale_timestamp` / `future_timestamp` | Clock more than 120 s off |
| `replayed_nonce` | Replay of a report that was already accepted |
| `too_frequent` | Less than 5 s since the last report |
| `bad_coords` | Out of range, or 0,0 (a GPS that hasn't started yet) |

**Downgraded** (plausible but weak; **kept**, shown with lower confidence):
off route by more than 150 m, or GPS accuracy worse than 100 m.

> Why are off-route reports flagged and not rejected? Real buses take
> diversions for roadworks, festivals and flooding. If we threw those reports
> away, the bus would just vanish, and the map would be lying by leaving it out.

**Colours on the map. How fresh the report is outranks everything else:**

- 🟢 **Live**: under 30 s old, on route
- 🟡 **Uncertain**: 30 s–2 min old, off route, or poor GPS accuracy
- ⚪ **Last known**: over 2 min old, greyed out, with the time shown

A perfect report from 4 minutes ago is never shown as live. Showing stale
positions as live is the main reason people stop trusting bus apps, and a test
enforces this rule.

A **dashed outline** means the bus is simulated. The map always says which
buses are simulated.

---

## 5. Where the routes come from

Nagercoil has no open transit data: no GTFS feed, no route list, no stop list.

- **Stops are pinned to real OpenStreetMap features.** For example, Vadasery
  Bus Stand is `way/227903070`, which anyone can open on openstreetmap.org to
  check.
- **Routes follow real roads.** They're built with OSRM routing between stops
  (`tools/build_osm_routes.py`). The builder removes wrong detours: it cut a
  286 m detour up a lane at Vadasery Market and an 86 m one at Suchindram, but
  kept detours into bus stands, because buses really do drive in.
- **The routes are labelled `osm-routed`, not surveyed.** They are the road a
  vehicle would take between those stops.
- **Record mode**: a driver can ride once with the driver page in record mode,
  and the admin can turn that trace into a route. That's how the system would
  build its own data in a town that has none.
- **Proves it scales**: the GTFS importer loaded Chennai MTC's public feed
  (4,614 routes, 1.36 million timetable rows, 3,934 routes imported) into the
  same tables with no code changes. That was tested locally. Only Nagercoil is
  deployed today.

The three routes deployed:

| Route | Path | Length |
|---|---|---|
| NGL-VAD-KKD | Vadasery → Anna Bus Stand → Kottar → Nagercoil Junction | 3.6 km |
| NGL-SUC | Anna Bus Stand → Suchindram | 5.1 km |
| NGL-KK | Anna Bus Stand → Suchindram → Kottaram → Kanyakumari | 19.3 km |

---

## 6. Arrival estimates (ETAs)

- **Always a range** ("2–4 min"), never one number. A tracker that says "7 min"
  and is 4 minutes wrong is worse than one that says "6–11" and is right.
- **No answer** when the bus has already passed the stop, is heading the other
  way, or its data is too old. It's better to say nothing than to make up a
  number.
- **Two methods**, and every answer says which one it used:
  - `speed`: remaining distance ÷ the bus's speed.
  - `learned`: travel time for every 200 m of road, in each direction, by time
    of day, learned from past trips. It retrains every night.
- A learned estimate is labelled **"Learned from simulated trips, not real
  traffic"**, because that's the truth: nobody has recorded real Nagercoil
  buses.

**Results** (`docs/eta-evaluation.md`, 21 simulated days: trained on days 1–14,
tested on days 15–21):

| Route | Speed-only error | Learned error |
|---|---|---|
| NGL-VAD-KKD | 3.04 min | **0.74 min** |
| NGL-KK | 2.47 min | **1.66 min** |

Learning *where and when* the road is slow cut error by **35–44%** compared
with a plain average pace. On a **control** (traffic with no rush hours), the
gain was **~0%**. That's what you'd expect, and it shows the gain comes from
learning real patterns and not from a bug.

> Say this before they ask: *all of this is simulated traffic.* The results
> show the method works when patterns exist. They don't show how accurate it
> would be on real Nagercoil roads.

---

## 7. Demo script (about 8 minutes)

| # | Do | Say |
|---|---|---|
| 1 | Show the **rider map**. Point at the moving buses and the legend. | "These buses are simulated, and the dashed outline says so. They go through exactly the same security checks as a real phone." |
| 2 | Pick **NGL-KK** in the dropdown, then press **Fit**. | "The routes follow real roads from OpenStreetMap. Every stop is a real, checkable map feature." |
| 3 | Click a **stop** (for example Kottar or Anna Bus Stand). | "The arrival estimate is a range. It says whether it's learned, and that it learned from simulated trips." |
| 4 | Switch to the **admin page**. Show the registered buses and routes. | "Each bus has its own secret key. The QR button gives a driver their link." |
| 5 | **Attack demo.** In a terminal, run the command below. | "Now I'll attack my own system: a fake signature, a bus teleporting to Chennai, replayed reports, flooding." |
| 6 | Refresh the admin page and scroll to **Rejected reports**. | "Every attack was rejected and logged, with the reason and the numbers. This log shows the checks actually run." |
| 7 | *(Optional)* Use your **phone as a bus** (see below). | "A real phone joins the same pipeline. In here it'll show amber because it's off route: flagged, not rejected." |
| 8 | Show the **Azure portal** → resource group `bustrack-rg`. | "Four managed services, a budget alert set up first, and everything scales to zero." |

**Attack demo command** (run in the project folder):

```powershell
$env:BUSTRACK_API = "https://bustrack-api-b7016d.azurewebsites.net/api"
$env:ADMIN_KEY = az functionapp config appsettings list -g bustrack-rg -n bustrack-api-b7016d --query "[?name=='ADMIN_KEY'].value" -o tsv
.venv\Scripts\python.exe -m pytest tests/test_api_e2e.py -k TestIngestRejects -v
```

It runs 11 tests in about 35 s, and all of them passed against Azure today.
Example rows from the log: `teleport 626893m in 20.0s = 112841 km/h`,
`bad_signature HMAC mismatch`, `bad_coords null island`.

**Phone as a bus (optional):**
1. Admin page → Register a bus → ID `DEMO-01`, route `NGL-VAD-KKD` → Register.
2. Click **QR** next to it and scan it with your phone. The driver page opens
   with the bus ID and key already filled in.
3. Tap **Start tracking** and allow location. Your phone appears on the rider
   map within about 10 s.
4. Delete `DEMO-01` from the admin page afterwards.

---

## 8. Questions you'll probably get

**Why not just use Google Maps?** Google shows transit only where operators
publish GTFS data. Nagercoil publishes nothing. The real problem is creating
the position data and deciding whether to trust it, not drawing the map.

**How would real buses report?** Short term: the driver's phone (the driver
page). Long term: a GPS unit through Azure IoT Hub. Indian public service
vehicles are already legally required to carry AIS-140 GPS units, so the best
answer is to take in those feeds.

**What stops a driver lying about their position?** Signatures stop other
people from pretending to be the bus. A real driver who reports a false
position *on* the route can't be detected today. The fix is several
independent reporters agreeing on one bus. That's designed but not built. Say
this plainly.

**Why polling instead of WebSockets/SignalR?** SignalR's free tier has a
20-connection cap. See the table in section 3.

**What if the phone screen locks?** Browser GPS stops. The page keeps the
screen awake, which helps but doesn't fix it. That's the concrete reason a
dedicated tracker is the real answer.

**How much does it cost?** Section 9: about ₹0.02 so far.

**How is it tested?** 214 automated tests: geometry, validation, storage,
learned ETAs, and end-to-end tests over real HTTP with real signing. The attack
tests you just saw are part of them.

**What about privacy?** Location history is deleted after 30 days. There's no
driver consent flow yet. That's an ethical gap and I name it as one.

**Is the ETA accurate?** On simulated traffic, yes, and it was measured
against a control. On real traffic it's unknown until trips are recorded.

---

## 9. Known limits (mention them before you're asked)

- Browser GPS stops when the phone screen locks.
- Routes are the drivable road between real stops, not surveyed bus paths.
- One admin key protects all bus secrets. It can't be rotated and there are no
  user roles. Azure Key Vault is the next step.
- QR codes are generated by an outside service (api.qrserver.com). That's fine
  for a demo but not for real use.
- The learned ETAs are trained on simulated history only.
- The map background comes from OpenFreeMap, a free service. Azure Maps would
  replace it for real use.

---

## 10. Cost

**Spent on this project so far (all of September): ₹0.02.** The Function App
was stopped for most of the month.

**Expected cost of running it today:** under ₹5, and most likely under ₹1.

| Service | Pricing | Today's use |
|---|---|---|
| Static Web Apps | Free tier | ₹0 |
| Functions (Consumption) | First 1 million runs and 400,000 GB-s per month are free | A class of 30 on the map for an hour ≈ 36,000 runs, plus buses ≈ 2,000. Inside the free allowance: ₹0 |
| Table Storage | About ₹0.03 per 10,000 operations, plus about ₹2 per GB per month | The demo history upload (≈125,000 rows) plus live use: a few paise to a rupee |
| Budget alert | Free | ₹0 |

**Something else on your subscription is costing money.** This month:

| Resource group | Spent in September | Why |
|---|---|---|
| `bustrack-rg` (this project) | ₹0.02 | — |
| `chat-app-rg` (your other project) | **₹1,523** | The VM is off, but its **disk (~₹8/day)** and its **reserved public IP (~₹11.5/day)** still charge about **₹20/day, or ~₹600/month**, while nothing is running |

That already puts the subscription past the ₹1,000/month budget. None of it is
the bus tracker. See the end of section 11.

---

## 11. Taking it down after the presentation

**Option A: delete everything (recommended once your grade is done).** Costs
₹0 afterwards.

```powershell
.\infra\deploy.ps1 -Destroy
```

It asks you to type `bustrack-rg` to confirm, then deletes the Function App,
storage, and web app in the background (a few minutes). The budget alert stays,
because it belongs to the subscription. To bring the project back later, run
`.\infra\deploy.ps1` again (about 10 minutes, and you'll get new URLs and a new
admin key), then load the routes with `tools/seed_nagercoil.py`.

**Option B: pause it (if the teacher might want to see it again).** Keeps the
same URLs.

```powershell
az functionapp stop -g bustrack-rg -n bustrack-api-b7016d
```

The website still loads but shows "Cannot reach the server". Storage costs
paise per month. To resume: `az functionapp start -g bustrack-rg -n bustrack-api-b7016d`.

In both cases, close the two simulator windows first.

**The chat app:** the ~₹20/day comes from `chat-app-vmpublicip` and the VM's OS
disk. Deleting the public IP stops ~₹11.5/day. Deleting the VM and its disk
stops the rest, but the VM is gone for good. That's your call. Nothing about
it was changed today.

---

## 12. If something goes wrong during the demo

| Symptom | Fix |
|---|---|
| Map shows "Cannot reach the server" | Function App stopped. Run `az functionapp start -g bustrack-rg -n bustrack-api-b7016d` and wait about 30 s |
| No buses on the map | The simulator windows were closed or the laptop slept. Run `.\infra\demo.ps1` again |
| Buses are grey | Their reports are over 2 minutes old: same cause as above |
| Map background is blank or plain | The tiles are still loading. Wait a few seconds or refresh. If a note says "Basemap tiles unavailable", the network is blocking the map service, but routes and buses still work |
| Arrival estimates say `speed`, not learned | The learned model only exists for routes with history. Both methods are correct; mention the difference |
| Admin page shows nothing or refuses | The key in the link is wrong. Use the link printed by `demo.ps1` |
| Phone driver page won't start | It needs HTTPS (the Azure link, not localhost) and location permission |
| Everything fails (Wi-Fi) | Run locally: `.\run-local.ps1`, then the simulator. See the README |
