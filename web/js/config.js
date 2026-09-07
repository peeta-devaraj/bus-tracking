// Where the API lives.
//
// Local development talks to the Functions host on :7071. Once deployed, the
// pages are served from Azure Static Web Apps and the API sits on its own
// Function App domain, so this gets overridden at deploy time by writing
// web/config.local.js (which is gitignored and loaded after this file).
window.BUSTRACK_CONFIG = {
  apiBase: "http://localhost:7071/api",

  // Rider map refresh. Three seconds is frequent enough to feel live without
  // hammering a consumption-plan Function App.
  pollIntervalMs: 3000,

  // Driver ping interval. The API rejects anything under 5 s, and a slower
  // rate is also kinder to a driver's battery and data.
  pingIntervalMs: 10000,

  // Default map view: Nagercoil town.
  defaultCenter: [77.4340, 8.1780], // [lon, lat] for MapLibre
  defaultZoom: 13,

  city: "nagercoil",
};

// Deployment writes config.local.js to override the above.

