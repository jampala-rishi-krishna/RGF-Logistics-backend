// Renders the real Routes tab (vite dev server on :5199) with mocked API + stubbed Google Maps and saves screenshots.
// Scratch only - not committed. Usage: node shoot.mjs [outDir]
import { createRequire } from "node:module";
const require = createRequire("C:/Users/dexte/Downloads/RGF/RGF/projects/IntelliFleet-Logistics-Platform/IntelliFleet-Logistics-Platform/artifacts/intellifleet/package.json");
const { chromium } = require("playwright");
import fs from "node:fs";
import path from "node:path";

const OUT = process.argv[2] || "./";
fs.mkdirSync(OUT, { recursive: true });
const BASE = "http://127.0.0.1:5199";

const part = (km, min, distance, time, fuel, refrigeration, toll, total) => ({
  distanceKm: km, durationMin: min,
  costBreakdown: { distance, time, fuel, refrigeration, tolls: toll.amount, total },
  toll,
});
const unknown = { present: true, amount: 0, unknown: true, inferred: true };
const none = { present: false, amount: 0, unknown: false };
const expressway = {
  key: "expressway", label: "Via expressway", distanceKm: 76.7, durationMin: 115, cost: 1757.07, tollDataAvailable: false, geometry: null,
  costBreakdown: { distance: 383.27, time: 229.93, fuel: 1040.32, refrigeration: 103.55, tolls: 0, total: 1757.07 },
  toll: unknown,
  roundTrip: {
    outbound: part(27.9, 52, 139.45, 103.5, 378.52, 103.55, unknown, 725.02),
    return: part(48.8, 63, 243.82, 126.43, 661.8, 0, unknown, 1032.05),
    total: part(76.7, 115, 383.27, 229.93, 1040.32, 103.55, unknown, 1757.07),
  },
};
const avoid = {
  key: "avoid", label: "Avoid tolls", distanceKm: 76.7, durationMin: 232, cost: 2056.07, tollDataAvailable: false, geometry: null,
  costBreakdown: { distance: 383.27, time: 464.0, fuel: 1040.32, refrigeration: 168.48, tolls: 0, total: 2056.07 },
  toll: none,
  roundTrip: {
    outbound: part(31.0, 105, 155, 210, 420.7, 168.48, none, 954.18),
    return: part(45.7, 127, 228.5, 254, 619.6, 0, none, 1102.1),
    total: part(76.7, 232, 383.5, 464, 1040.3, 168.48, none, 2056.28),
  },
};
const warehouses = [
  { id: "mets", name: "Mets Cold Storage", address: "Mets Cold Storage, Km 41 Aguinaldo Hwy, Silang, Cavite, Philippines", google_place: "", lat: 14.29, lng: 121.01, place_id_hex: "", map_url: "https://maps.google.com/?q=mets" },
  { id: "glacier", name: "Glacier Cold Storage", address: "Glacier Cold Storage, Tambo, Parañaque City, Metro Manila, Philippines", google_place: "", lat: 14.49, lng: 120.99, place_id_hex: "", map_url: "https://maps.google.com/?q=glacier" },
];
const plan = {
  origin: { label: "Santa Maria, Bulacan, Philippines", lat: 14.83, lng: 120.98 },
  destination: { label: "Blumentritt Rd, Sampaloc, Manila, Metro Manila, Philippines", lat: 14.62, lng: 121.0 },
  stops: [], mode: "fastest", objectiveNote: "", returnToWarehouse: true, returnWarehouse: warehouses[0],
  ...expressway, geometry: null, warnings: [], tollsEnabled: true, expressways: "compare", tollOptions: [expressway, avoid],
  activeOption: "expressway", cheapestOption: null, fastestOption: "expressway", noTollFreeAlternative: false, tollDataAvailable: false,
  rates: {
    dieselPricePerLiter: 95, fuelKmPerLiter: 7, fuelCostPerKm: 13.57, distanceCostPerKm: 5, driverCostPerHour: 120, helperCostPerHour: 120,
    refrigerationLitersPerHourChilled: 0.8, refrigerationLitersPerHourFrozen: 1.2, refrigerationCostPerHourChilled: 76, refrigerationCostPerHourFrozen: 114,
    refrigerationOnReturnLeg: false, tollsEnabled: true, tollVehicleClass: 2, tollMultiplier: 2,
  },
  costAssumptions: { hasHelper: false, refrigerated: true, coldChain: "chilled", coldChainAssumed: true, serviceMinPerStop: 30 },
  routing: { provider: "google", profile: "DRIVE", trafficAware: true, calculatedAt: new Date(Date.now() - 11 * 60000).toISOString() },
};
plan.roundTrip = expressway.roundTrip;

const MAPS_STUB = `(function(){
  var d = (window.google = window.google || {}).maps = (window.google && window.google.maps) || {};
  var stub = new Proxy(function(){}, { get: function(t,p){ return p === 'then' ? undefined : stub; }, apply: function(){ return stub; }, construct: function(){ return stub; } });
  window.__acs = [];
  function Autocomplete(input){ this.input = input; this.l = []; window.__acs.push(this); }
  Autocomplete.prototype.addListener = function(e, cb){ this.l.push(cb); };
  Autocomplete.prototype.getPlace = function(){ return this.place; };
  function FakeMap(el){ el.innerHTML = '<div style="display:grid;place-items:center;height:100%;min-height:inherit;background:linear-gradient(135deg,#e8efe8,#dfe8f0);color:#55565a;font:12px DM Sans,sans-serif">Google Map (stub)</div>'; return new Proxy(this, { get: function(t,p){ return p in t ? t[p] : function(){}; } }); }
  ['Marker','Polyline','LatLngBounds','Size','Point','InfoWindow','OverlayView','Circle','LatLng'].forEach(function(n){ d[n] = stub; });
  d.Map = FakeMap; d.event = stub; d.SymbolPath = stub; d.Animation = stub;
  d.importLibrary = function(){ return Promise.resolve({ Autocomplete: Autocomplete }); };
  if (typeof d.__ib__ === 'function') d.__ib__();
})();`;

const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });

async function mockApi(page) {
  await page.route(/maps\.googleapis\.com\/maps\/api\/js/, (route) => route.fulfill({ contentType: "text/javascript", body: MAPS_STUB }));
  await page.route(/fonts\.(googleapis|gstatic)\.com/, (route) => route.continue());
  await page.route("http://mock.test/**", (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*" } });
    const url = new URL(req.url());
    const p = url.pathname;
    if (p === "/auth/session") return json(route, { user: { id: 1, email: "admin@rgf.com", role: "admin", full_name: "Admin", status: "active" } });
    if (p === "/routes/warehouses") return json(route, { warehouses });
    if (p === "/routes/plan" && req.method() === "POST") return json(route, plan);
    if (p.startsWith("/routes") || p.startsWith("/vehicles") || p.startsWith("/orders") || p.startsWith("/manifests")) return json(route, []);
    return json(route, {});
  });
}

async function pickPlace(page, index, label, lat, lng) {
  await page.evaluate(([i, text, la, ln]) => {
    const ac = window.__acs[i];
    ac.place = { formatted_address: text, geometry: { location: { lat: () => la, lng: () => ln } } };
    ac.l.forEach((cb) => cb());
  }, [index, label, lat, lng]);
}

const sizes = [
  ["1440", 1440, 900],
  ["1024", 1024, 800],
  ["768", 768, 1024],
  ["390", 390, 844],
];

const browser = await chromium.launch();
const report = [];
for (const [name, width, height] of sizes) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2, hasTouch: width < 700, isMobile: width < 700 });
  await context.addInitScript(() => localStorage.setItem("if-access-token", "mock-token"));
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await mockApi(page);
  await page.goto(`${BASE}/app/routes`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('text=Plan a live route', { timeout: 30000 });
  await page.waitForFunction(() => (window.__acs || []).length >= 2, null, { timeout: 15000 });
  await pickPlace(page, 0, plan.origin.label, 14.83, 120.98);
  await pickPlace(page, 1, plan.destination.label, 14.62, 121.0);
  await page.getByText("Mets Cold Storage").first().click();
  await page.screenshot({ path: path.join(OUT, `${name}-builder.png`), fullPage: false });
  await page.getByRole("button", { name: /Calculate route/ }).click();
  await page.waitForSelector('[data-testid="route-cost-table"]', { timeout: 15000 });
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, `${name}-full.png`), fullPage: true });
  // expand "How this is calculated" for a second capture of the card
  await page.locator(".rc-how summary").click();
  await page.locator('[data-testid="route-cost-table"]').screenshot({ path: path.join(OUT, `${name}-cost-card-open.png`) });
  const metrics = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    docScrollWidth: document.documentElement.scrollWidth,
    bodyScrollWidth: document.body.scrollWidth,
    tableVisible: !!document.querySelector(".rc-scroll") && getComputedStyle(document.querySelector(".rc-scroll")).display !== "none",
    cardsVisible: !!document.querySelector(".rc-cards") && getComputedStyle(document.querySelector(".rc-cards")).display !== "none",
    tableScrolls: (() => { const e = document.querySelector('.rc-scroll'); return e ? e.scrollWidth > e.clientWidth + 1 : null; })(),
    actionBarPosition: getComputedStyle(document.querySelector(".route-action-bar")).position,
    inputHeights: [...document.querySelectorAll("form input[type=text], form input:not([type])")].map((i) => Math.round(i.getBoundingClientRect().height)),
    inputFont: [...document.querySelectorAll("form input:not([type=radio]):not([type=checkbox])")].map((i) => getComputedStyle(i).fontSize),
    tapTargetsUnder44: [...document.querySelectorAll("form button, form .route-choice, summary")].filter((e) => { const r = e.getBoundingClientRect(); return r.height > 0 && r.height < 43.5; }).map((e) => (e.textContent || "").trim().slice(0, 30)),
    offenders: [...document.querySelectorAll(".route-tab *")].filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right > window.innerWidth + 1 && !e.closest(".rc-scroll"); }).slice(0, 5).map((e) => e.className?.toString().slice(0, 50) || e.tagName),
  }));
  report.push({ name, ...metrics, errors });
  await context.close();
}
await browser.close();
fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
