# Threat model

Who would attack a bus tracker, why, and what this system does about it.

The short version: the valuable thing here is not data to steal, it is **the
map's credibility**. Almost every realistic attack is an attempt to make the map
say something false. So the defences are about the integrity of position
reports, not about confidentiality.

## Assets

| Asset | Why it matters |
|---|---|
| Position reports | The product. If they can be forged, nothing else matters. |
| Per-bus secrets | Holding one lets you speak as that bus, permanently. |
| The admin key | Grants access to every bus secret at once. |
| Route and stop definitions | Corrupting these silently breaks every ETA. |

## Adversaries

1. **A bored student** who opens dev tools and tries to put a bus in the sea.
   Unskilled, opportunistic, and by far the most likely.
2. **A driver gaming the system** — reporting a position ahead of where they
   really are so riders stop complaining, or turning tracking off during a
   break. Has legitimate credentials, which makes this the hardest case.
3. **A rival operator** wanting the service to look unreliable.
4. **A passive observer** watching traffic to learn where buses and drivers are.

## Threats and responses

### T1 — Forged position for a bus you do not control
*Adversary 1, 3.* Post to `/api/ping` claiming to be `NGL-01`.

**Response.** Every report needs an HMAC-SHA256 signature over the exact request
body, keyed by that bus's secret. No secret, no signature, no entry. Unknown bus
IDs get a deliberately vague 401 so probing reveals nothing about which IDs
exist. *Tested:* `test_an_unsigned_fix_is_rejected`,
`test_a_wrong_signature_is_rejected`, `test_an_unregistered_bus_is_rejected`.

### T2 — Replaying a captured report
*Adversary 1, 3.* Capture a valid signed request and re-send it, pinning the bus
in place or rewinding it.

**Response.** Two layers. Reports carry a timestamp that must be within 120 s of
server time, and a nonce; the last 20 nonces per bus are stored and a repeat is
rejected. *Tested:* `test_a_stale_timestamp_is_rejected`,
`test_reused_nonce_is_rejected`.

### T3 — Tampering with a signed body
*Adversary 1.* Keep a valid signature, change the coordinates.

**Response.** The signature covers the raw body bytes, so any edit invalidates
it. Signing the exact bytes rather than a canonical reconstruction also removes
the class of bugs where client and server disagree about float formatting.
*Tested:* `test_a_tampered_body_is_rejected`.

### T4 — Physically impossible movement
*Adversary 1, 2.* A bus that is in Nagercoil and then, twenty seconds later,
claims Chennai.

**Response.** Implied speed between consecutive fixes is capped at 90 km/h.
Anything faster is rejected with the arithmetic in the reason string, which is
also what makes it a convincing thing to demonstrate live. *Tested:*
`test_a_teleport_to_chennai_is_rejected`.

### T5 — Flooding the endpoint
*Adversary 3.* Burn the Function App's execution quota, or bloat storage.

**Response.** One fix per bus per 5 seconds; anything faster is rejected before
it touches storage. Note the limit is **per bus**, so it is a quota control
rather than a general DDoS defence — a real flood is Azure's front door to
absorb, not this function's. *Tested:* `test_flooding_is_rate_limited`.

### T6 — A legitimate driver reporting a false position
*Adversary 2.* The hardest threat, because the credentials are genuine.

**Partial response, honestly labelled.** The off-route check flags a bus more
than 150 m from its assigned route. It **flags rather than rejects**, because
real diversions happen and discarding them would make the system lie by
omission. A driver who stays on the route but lies about *where* on it is not
detectable by this system today.

The real answer is corroboration — several independent reporters on the same
bus agreeing. That is designed (below) and not built.

### T7 — Stale data presented as live
Not an attacker; the system deceiving users by accident. This is the failure
that actually destroys trust in bus tracking apps in practice.

**Response.** Confidence is computed from freshness at read time and always
dominates. Over 30 s is `uncertain`; over 2 minutes is `last_known`, greyed with
an explicit timestamp. A perfect fix from four minutes ago is never drawn as
live. The rider map also distinguishes "no bus is approaching" from "no bus is
being tracked", and reports when it cannot reach the server at all rather than
leaving old dots looking current. *Tested:*
`test_a_perfect_but_old_fix_is_never_shown_as_live`.

### T8 — Secret disclosure
*Adversary 1, 4.*

**Current state, and its gaps.** Secrets sit in Table Storage, encrypted at rest.
The server needs the secret itself to recompute an HMAC, so it cannot store only
a hash. The admin endpoints that return secrets are behind `ADMIN_KEY`. The
driver page takes credentials in the **URL fragment**, which browsers never send
to servers and which stays out of server logs.

Known weaknesses, stated plainly:

- The driver page caches the key in `localStorage`; anyone with the unlocked
  phone can read it.
- QR codes are rendered by a third-party service, so the key is sent there.
  Fine for a classroom, not for a deployment — generate them locally first.
- A leaked secret is valid until the bus is deleted and re-registered. There is
  no rotation or expiry.

### T9 — Corrupting routes or stops
*Adversary 3.* Overwrite a route so every ETA on it becomes wrong. Quiet and
damaging, because nothing looks broken.

**Response.** All write endpoints for routes and stops require `ADMIN_KEY`. Note
that this makes the admin key a single point of failure — see below.

## What is deliberately not defended

Saying this explicitly is more useful than implying coverage that does not exist.

- **No per-user accounts or roles.** One admin key for everything. Losing it
  compromises every bus at once.
- **No rotation.** Secrets and the admin key are fixed until changed by hand.
- **No transport-layer protection beyond TLS.** Sufficient, but there is no
  certificate pinning or device attestation, so a rooted phone can extract its
  own key.
- **No abuse throttling by IP**, only per bus.
- **No audit trail for admin actions.** Rejections are logged; route edits and
  bus registrations are not.
- **Location privacy of drivers.** A driver running the page is continuously
  tracked, and history is retained indefinitely. A real deployment needs a
  retention policy and the driver's informed consent. This is an ethical
  obligation, not a technical one, and it is unaddressed today.

## Designed but not built

- **Multi-reporter consensus.** Several independent devices on the same bus
  agreeing raises confidence; a lone outlier loses it. This is the only real
  answer to T6.
- **Device attestation** to bind a secret to one physical device.
- **AIS-140 / VLTD ingestion.** Indian public service vehicles are already
  legally required to carry GPS units. Long term, the right move is ingesting
  those regulated feeds rather than relying on drivers' phones — which
  simultaneously solves T6, the wake-lock limitation, and coverage.
- **Key Vault** for secrets, and short-lived signed tokens instead of long-lived
  bus secrets.
