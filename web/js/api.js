// Shared helpers for talking to the API and for the small amount of
// presentation logic that all three pages need.

const CFG = window.BUSTRACK_CONFIG;

function apiUrl(path, params) {
  const url = new URL(CFG.apiBase.replace(/\/$/, "") + path, window.location.href);
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
    });
  }
  return url.toString();
}

async function apiGet(path, params) {
  const res = await fetch(apiUrl(path, params));
  if (!res.ok) throw new Error(`${path} returned ${res.status}`);
  return res.json();
}

async function apiPost(path, body, extraHeaders) {
  const res = await fetch(apiUrl(path), {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, extraHeaders || {}),
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let parsed = null;
  try { parsed = JSON.parse(text); } catch (e) { /* non-JSON error body */ }
  if (!res.ok) {
    const message = (parsed && (parsed.error || parsed.reason)) || text || res.status;
    const err = new Error(message);
    err.status = res.status;
    err.body = parsed;
    throw err;
  }
  return parsed;
}

// --------------------------------------------------------------------------
// Request signing, matching api/shared/auth.py exactly.
//
// The signature covers the exact body string that goes on the wire, so the
// caller must send this same string rather than re-serialising the object.
// Web Crypto needs a secure context, which means https:// or localhost.
// --------------------------------------------------------------------------

async function signBody(secret, bodyString) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(bodyString));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function sendPing(busId, secret, fix) {
  const bodyString = JSON.stringify(fix);
  const signature = await signBody(secret, bodyString);

  const res = await fetch(apiUrl("/ping"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Bus-Id": busId,
      "X-Bus-Signature": signature,
    },
    body: bodyString, // must be byte-identical to what was signed
  });

  let parsed = null;
  try { parsed = await res.json(); } catch (e) { /* empty body */ }
  return { ok: res.ok, status: res.status, body: parsed };
}

// --------------------------------------------------------------------------
// Presentation
// --------------------------------------------------------------------------

const CONFIDENCE = {
  live:       { color: "#16a34a", label: "Live",       hint: "Reported in the last 30 seconds" },
  uncertain:  { color: "#d97706", label: "Uncertain",  hint: "Delayed, off-route, or weak GPS" },
  last_known: { color: "#6b7280", label: "Last known", hint: "Not reporting; this is where it was" },
};

const FLAG_TEXT = {
  off_route: "off the expected route",
  coarse_accuracy: "weak GPS signal",
  no_route: "no route assigned",
};

function confidenceOf(level) {
  return CONFIDENCE[level] || CONFIDENCE.last_known;
}

function formatAge(seconds) {
  if (seconds === null || seconds === undefined) return "unknown";
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

function describeFlags(flags) {
  if (!flags || !flags.length) return "";
  return flags.map((f) => FLAG_TEXT[f] || f).join(", ");
}

// Decode a Google encoded polyline into [lon, lat] pairs for MapLibre.
// (Note the order: GeoJSON is lon,lat while the API speaks lat,lon.)
function decodePolyline(encoded, precision = 5) {
  const factor = Math.pow(10, precision);
  const coords = [];
  let index = 0, lat = 0, lon = 0;

  while (index < encoded.length) {
    let result = 0, shift = 0, byte;
    do {
      if (index >= encoded.length) return coords;
      byte = encoded.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    lat += (result & 1) ? ~(result >> 1) : (result >> 1);

    result = 0; shift = 0;
    do {
      if (index >= encoded.length) return coords;
      byte = encoded.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    lon += (result & 1) ? ~(result >> 1) : (result >> 1);

    coords.push([lon / factor, lat / factor]);
  }
  return coords;
}

function encodePolyline(latLonPairs, precision = 5) {
  const factor = Math.pow(10, precision);
  let out = "", prevLat = 0, prevLon = 0;

  const encodeValue = (delta) => {
    let v = delta < 0 ? ~(delta << 1) : (delta << 1);
    let s = "";
    while (v >= 0x20) {
      s += String.fromCharCode((0x20 | (v & 0x1f)) + 63);
      v >>= 5;
    }
    return s + String.fromCharCode(v + 63);
  };

  for (const [lat, lon] of latLonPairs) {
    const iLat = Math.round(lat * factor);
    const iLon = Math.round(lon * factor);
    out += encodeValue(iLat - prevLat) + encodeValue(iLon - prevLon);
    prevLat = iLat;
    prevLon = iLon;
  }
  return out;
}

// The map style: OpenStreetMap raster tiles, no API key, no billing surface.
//
// The background layer underneath the tiles matters more than it looks. When
// tiles are slow, blocked by a network, or unavailable offline, the map still
// renders a neutral canvas with the routes and buses drawn on top, instead of
// a black void that reads as "the app is broken". The position data is the
// point; the basemap is context.
function osmStyle() {
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  return {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        maxzoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      },
    },
    layers: [
      { id: "background", type: "background", paint: { "background-color": dark ? "#1a2030" : "#e8e6e1" } },
      { id: "osm", type: "raster", source: "osm" },
    ],
  };
}

// Run `fn` once the style can accept addSource/addLayer.
//
// Using map.once("styledata") is a trap: if the style finishes loading before
// the handler is attached, no further styledata event ever fires and the
// callback is dropped silently. Listening persistently and detaching after a
// successful run is the version that actually works.
function whenStyleReady(map, fn) {
  if (map.isStyleLoaded()) { fn(); return; }

  let done = false;
  const run = () => {
    if (done || !map.isStyleLoaded()) return;
    done = true;
    map.off("style.load", run);
    map.off("styledata", run);
    clearInterval(poll);
    fn();
  };

  // "style.load" is the event that actually means "you may add layers now",
  // and unlike map "load" it does not wait for tiles to arrive. The styledata
  // listener and the poll are belt and braces for the case where the style
  // finishes between our check and our subscription.
  map.on("style.load", run);
  map.on("styledata", run);
  const poll = setInterval(run, 200);
  setTimeout(() => clearInterval(poll), 15000);
}

// Watch for basemap tiles failing so the page can say so plainly rather than
// leaving the user staring at an empty rectangle wondering what went wrong.
function onTileTrouble(map, callback) {
  let reported = false;
  map.on("error", (e) => {
    const isTile = e && e.sourceId === "osm";
    if (isTile && !reported) {
      reported = true;
      callback();
    }
  });
}
