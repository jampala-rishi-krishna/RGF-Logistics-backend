// Routes tab states: empty -> calculating -> result -> stale (hidden) -> error. Scratch only - not committed.
// Needs the vite dev server on :5199 (see shoot.mjs). Usage: node shoot-empty.mjs [outDir]
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
const require = createRequire("C:/Users/dexte/Downloads/RGF/RGF/projects/IntelliFleet-Logistics-Platform/IntelliFleet-Logistics-Platform/artifacts/intellifleet/package.json");
const { chromium } = require("playwright");

const OUT = process.argv[2] || "./routes-empty";
fs.mkdirSync(OUT, { recursive: true });
const BASE = "http://127.0.0.1:5199";

const part = (km, min, distance, time, fuel, refrigeration, toll, total) => ({ distanceKm: km, durationMin: min, costBreakdown: { distance, time, fuel, refrigeration, tolls: toll.amount, total }, toll });
const unknown = { present: true, amount: 0, unknown: true, inferred: true };
const none = { present: false, amount: 0, unknown: false };
const expressway = { key: "expressway", label: "Via expressway", distanceKm: 76.7, durationMin: 115, cost: 1757.07, tollDataAvailable: false, geometry: null,
  costBreakdown: { distance: 383.27, time: 229.93, fuel: 1040.32, refrigeration: 103.55, tolls: 0, total: 1757.07 }, toll: unknown,
  roundTrip: { outbound: part(27.9, 52, 139.45, 103.5, 378.52, 103.55, unknown, 725.02), return: part(48.8, 63, 243.82, 126.43, 661.8, 0, unknown, 1032.05), total: part(76.7, 115, 383.27, 229.93, 1040.32, 103.55, unknown, 1757.07) } };
const avoid = { key: "avoid", label: "Avoid tolls", distanceKm: 76.7, durationMin: 232, cost: 2056.07, tollDataAvailable: false, geometry: null,
  costBreakdown: { distance: 383.27, time: 464.0, fuel: 1040.32, refrigeration: 168.48, tolls: 0, total: 2056.07 }, toll: none,
  roundTrip: { outbound: part(31.0, 105, 155, 210, 420.7, 168.48, none, 954.18), return: part(45.7, 127, 228.5, 254, 619.6, 0, none, 1102.1), total: part(76.7, 232, 383.5, 464, 1040.3, 168.48, none, 2056.28) } };
const warehouses = [
  { id: "mets", name: "Mets Cold Storage", address: "Governors Drive, Brgy. Bancal, Carmona, Cavite", google_place: "", lat: 14.2907776, lng: 121.0134132, place_id_hex: "", map_url: "https://maps.google.com/?q=mets" },
  { id: "glacier", name: "Glacier Cold Storage", address: "Amvel Business Park, Ninoy Aquino Ave, Parañaque City", google_place: "", lat: 14.4922771, lng: 120.9929815, place_id_hex: "", map_url: "https://maps.google.com/?q=glacier" },
];
const plan = {
  origin: { label: "Santa Maria, Bulacan, Philippines", lat: 14.83, lng: 120.98 },
  destination: { label: "Blumentritt Rd, Sampaloc, Manila, Metro Manila, Philippines", lat: 14.62, lng: 121.0 },
  stops: [], mode: "fastest", objectiveNote: "", returnToWarehouse: true, returnWarehouse: warehouses[0], ...expressway, geometry: null, warnings: [],
  tollsEnabled: true, expressways: "compare", tollOptions: [expressway, avoid], activeOption: "expressway", cheapestOption: null, fastestOption: "expressway",
  noTollFreeAlternative: false, tollDataAvailable: false,
  rates: { dieselPricePerLiter: 95, fuelKmPerLiter: 7, fuelCostPerKm: 13.57, distanceCostPerKm: 5, driverCostPerHour: 120, helperCostPerHour: 120, refrigerationLitersPerHourChilled: 0.8, refrigerationLitersPerHourFrozen: 1.2, refrigerationCostPerHourChilled: 76, refrigerationCostPerHourFrozen: 114, refrigerationOnReturnLeg: false, tollsEnabled: true, tollVehicleClass: 2, tollMultiplier: 2 },
  costAssumptions: { hasHelper: false, refrigerated: true, coldChain: "chilled", coldChainAssumed: true, serviceMinPerStop: 30 },
  routing: { provider: "google", profile: "DRIVE", trafficAware: true, calculatedAt: new Date(Date.now() - 60000).toISOString() },
};
plan.roundTrip = expressway.roundTrip;

const MAPS_STUB = `(function(){
  var d = (window.google = window.google || {}).maps = (window.google && window.google.maps) || {};
  var stub = new Proxy(function(){}, { get: function(t,p){ return p === 'then' ? undefined : stub; }, apply: function(){ return stub; }, construct: function(){ return stub; } });
  window.__acs = []; window.__markers = []; window.__fits = [];
  function Autocomplete(input){ this.input = input; this.l = []; window.__acs.push(this); }
  Autocomplete.prototype.addListener = function(e, cb){ this.l.push(cb); };
  Autocomplete.prototype.getPlace = function(){ return this.place; };
  function Bounds(){ this.pts = []; } Bounds.prototype.extend = function(p){ this.pts.push(p); };
  function Marker(o){ window.__markers.push({ title: o.title, lat: o.position.lat, lng: o.position.lng }); }
  Marker.prototype.setMap = function(){};
  function FakeMap(el){ this.el = el; el.innerHTML = '<div style="display:grid;place-items:center;height:100%;background:linear-gradient(135deg,#e8efe8,#dfe8f0);color:#55565a;font:12px DM Sans,sans-serif">Google Map (stub)</div>'; var self = this; return new Proxy(this, { get: function(t,p){ if (p === 'fitBounds') return function(b, pad){ window.__fits.push({ points: b.pts ? b.pts.length : null, padding: pad, h: self.el.clientHeight }); }; return p in t ? t[p] : function(){}; } }); }
  ['Polyline','Size','Point','InfoWindow','OverlayView','Circle','LatLng'].forEach(function(n){ d[n] = stub; });
  d.Marker = Marker; d.LatLngBounds = Bounds; d.Map = FakeMap; d.event = stub; d.SymbolPath = stub; d.Animation = stub;
  d.importLibrary = function(){ return Promise.resolve({ Autocomplete: Autocomplete }); };
  if (typeof d.__ib__ === 'function') d.__ib__();
})();`;

const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
let planMode = "ok";
async function mockApi(page) {
  await page.route(/maps\.googleapis\.com\/maps\/api\/js/, (route) => route.fulfill({ contentType: "text/javascript", body: MAPS_STUB }));
  await page.route("http://mock.test/**", async (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*" } });
    const p = new URL(req.url()).pathname;
    if (p === "/auth/session") return json(route, { user: { id: 1, email: "planner@rgf.com", role: "dispatcher", full_name: "Planner", status: "active" } });
    if (p === "/routes/warehouses") return json(route, { warehouses });
    if (p === "/routes/plan" && req.method() === "POST") {
      await new Promise((r) => setTimeout(r, 1500));
      if (planMode === "error") return json(route, { error: "Google Routes API returned no route." }, 502);
      return json(route, plan);
    }
    if (p.startsWith("/routes") || p.startsWith("/vehicles") || p.startsWith("/orders") || p.startsWith("/manifests")) return json(route, []);
    return json(route, {});
  });
}
const pick = (page, i, label, lat, lng) => page.evaluate(([i, text, la, ln]) => { const ac = window.__acs[i]; ac.place = { formatted_address: text, geometry: { location: { lat: () => la, lng: () => ln } } }; ac.l.forEach((cb) => cb()); }, [i, label, lat, lng]);
const state = (page) => page.evaluate(() => {
  const wrap = document.querySelector('[data-testid="route-map-wrap"]');
  const r = wrap.getBoundingClientRect();
  return {
    mapState: wrap.dataset.state, mapHeight: Math.round(r.height), viewportH: innerHeight,
    kpiCards: document.querySelectorAll(".rk-card").length, costCard: !!document.querySelector('[data-testid="route-cost-table"]'),
    compareCards: !!document.querySelector('[data-testid="toll-options"]'), timeline: !!document.querySelector(".rt-card"),
    dashPlaceholders: [...document.querySelectorAll(".rk-value")].filter((e) => e.textContent.trim() === "-").length,
    hintVisible: !!document.querySelector(".rm-hint"), overlayVisible: !!document.querySelector(".rm-overlay"),
    markers: window.__markers.map((m) => m.title), lastFit: window.__fits.at(-1) || null,
    docScrollWidth: document.documentElement.scrollWidth,
  };
});

const browser = await chromium.launch();
const report = {};
for (const [name, width, height] of [["1440", 1440, 900], ["390", 390, 844]]) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2, hasTouch: width < 700, isMobile: width < 700 });
  await context.addInitScript(() => localStorage.setItem("if-access-token", "mock-token"));
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await mockApi(page);
  planMode = "ok";
  await page.goto(`${BASE}/app/routes`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("text=Plan a live route", { timeout: 30000 });
  await page.waitForFunction(() => (window.__acs || []).length >= 2 && window.__markers.length >= 2, null, { timeout: 15000 });
  await page.waitForTimeout(350);
  const r = { errors };
  r.empty = await state(page);
  if (width >= 1024) await page.evaluate(() => window.scrollTo(0, 260));
  await page.screenshot({ path: path.join(OUT, `${name}-1-empty.png`), fullPage: width < 700 });

  await pick(page, 0, plan.origin.label, 14.83, 120.98);
  await pick(page, 1, plan.destination.label, 14.62, 121.0);
  await page.getByText("Mets Cold Storage").first().click();
  await page.getByRole("button", { name: /Calculate route/ }).click();
  await page.waitForSelector(".rm-overlay", { timeout: 5000 });
  r.calculating = await state(page);
  await page.screenshot({ path: path.join(OUT, `${name}-2-calculating.png`), fullPage: false });

  await page.waitForSelector('[data-testid="route-cost-table"]', { timeout: 15000 });
  await page.waitForTimeout(500);
  r.result = await state(page);
  await page.screenshot({ path: path.join(OUT, `${name}-3-result.png`), fullPage: true });

  // inputs changed -> stale result is hidden, full-height map returns
  await page.getByRole("button", { name: "Shortest" }).click();
  await page.waitForTimeout(450);
  r.afterInputChange = await state(page);
  await page.screenshot({ path: path.join(OUT, `${name}-4-stale-hidden.png`), fullPage: width < 700 });

  // calculate error
  planMode = "error";
  await page.getByRole("button", { name: /Calculate route/ }).click();
  await page.waitForSelector('[role="alert"]', { timeout: 10000 });
  await page.waitForTimeout(300);
  r.error = await state(page);
  r.error.alertText = await page.locator('[role="alert"]').first().innerText();
  await page.screenshot({ path: path.join(OUT, `${name}-5-error.png`), fullPage: width < 700 });
  report[name] = r;
  await context.close();
}
await browser.close();
fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
