// Playwright screenshots for the Fleet Health page, with the whole API mocked from ./fixtures.
//
// 1) from artifacts/intellifleet (background):
//      $env:VITE_API_BASE_URL="http://mock.local"; $env:VITE_CARTRACK_WS_URL="ws://mock.local/ws/fleet"
//      npx vite --config vite.config.ts --port 5199
//    (the process env overrides artifacts/intellifleet/.env, so nothing here can reach the real API)
// 2) node backend/scratch/ui/fleet-health/shoot.mjs        (optionally: BASE=http://localhost:5199 ONLY=04)
//
// Every request to http://mock.local is answered here; requests to the production API host are aborted.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.resolve(HERE, "../../../../artifacts/intellifleet");
const require = createRequire(path.join(APP, "package.json"));
const { chromium } = require("playwright");

const BASE = process.env.BASE || "http://localhost:5199";
const ONLY = process.env.ONLY || "";
const API = "http://mock.local";
const FIX = path.join(HERE, "fixtures");
const OUT = HERE;
const NOW = "2026-10-08T06:58:52Z";

const load = (name) => JSON.parse(fs.readFileSync(path.join(FIX, `${name}.json`), "utf8"));
const fx = {
  summary: load("summary"),
  trucks: load("trucks"),
  eco_drivers: load("eco_drivers"),
  eco_drivers_prev: load("eco_drivers_prev"),
  eco_trucks: load("eco_trucks"),
  fuel_checks: load("eco_fuel_checks"),
  co2: load("eco_co2"),
  fuel_logs: load("fuel_logs"),
  scorecard: load("scorecard"),
};
const details = {};
for (const f of fs.readdirSync(FIX)) {
  const m = /^truck_(-?\d+)\.json$/.exec(f);
  if (m) details[m[1]] = load(f.replace(".json", ""));
}

const unmocked = new Set();
const apiCalls = [];

function user(role) {
  return { user: { id: 1, email: "rishi@rareglobalfood.com", fullName: "Rishi", role, status: "active", mustChangePassword: false } };
}

function detailFor(id) {
  if (details[id]) return details[id];
  const t = fx.trucks.trucks.find((x) => String(x.id) === String(id));
  if (!t) return null;
  if (t.kind === "third_party") return { truck: t };
  return { ...details["4"], truck: t };
}

function makeHandler(opts) {
  const state = { scorecardEnabled: opts.scorecardEnabled ?? false };
  const data = opts.data || fx;
  return async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname;
    const method = req.method();
    const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*" } });
    apiCalls.push(`${method} ${p}${url.search}`);
    if (p === "/auth/session") return json(user(opts.role));
    if (!p.startsWith("/api/fleet-health")) {
      unmocked.add(`${method} ${p}`);
      return json(method === "GET" ? [] : { ok: true });
    }
    const sub = p.replace("/api/fleet-health", "");
    if (method === "GET") {
      if (sub === "/summary") return json(data.summary);
      if (sub === "/trucks") return json(data.trucks);
      const d = /^\/trucks\/(-?\d+)$/.exec(sub);
      if (d) {
        if (opts.detailDelay) await new Promise((r) => setTimeout(r, opts.detailDelay));
        const body = (opts.detailFor || detailFor)(d[1]);
        return body ? json(body) : json({ detail: "Vehicle not found" }, 404);
      }
      if (sub === "/eco/drivers") {
        const week = url.searchParams.get("week");
        return json(week && week < data.eco_drivers.week_start ? data.eco_drivers_prev : data.eco_drivers);
      }
      if (sub === "/eco/trucks") return json(data.eco_trucks);
      if (sub === "/eco/fuel-checks") return json(data.fuel_checks);
      if (sub === "/eco/co2") return json(data.co2);
      if (sub === "/fuel-logs") return json(data.fuel_logs);
      if (sub === "/eco/scorecard") return json({ ...data.scorecard, switch: { ...data.scorecard.switch, enabled: state.scorecardEnabled } });
    }
    if (method === "PUT" && sub === "/eco/scorecard") {
      const body = JSON.parse(req.postData() || "{}");
      state.scorecardEnabled = !!body.enabled;
      return json({ ...data.scorecard.switch, enabled: state.scorecardEnabled, changed_by: "Rishi", changed_at: NOW });
    }
    if (method === "POST" && sub === "/pretrip-checklists") {
      const body = JSON.parse(req.postData() || "{}");
      const issues = Object.entries(body.items || {}).filter(([, v]) => v === "issue").map(([k]) => k);
      const flag = issues.length
        ? { created: true, flag: { id: 99, vehicle_id: body.vehicle_id, source: "checklist", severity: issues.includes("brakes") ? "critical" : "warning", message: `Pre-trip check failed: ${issues.join(", ").replace(/_/g, " ")}.`, ref: "checklist:99", photo_ref: null, reported_by: "Rishi", occurred_at: NOW, created_at: NOW, resolved_at: null, resolved_by: null, resolution_note: null } }
        : null;
      return json({ id: 99, vehicle_id: body.vehicle_id, checked_at: NOW, items: body.items, passed: !issues.length, reefer_temp_c: body.reefer_temp_c ?? null, notes: body.notes ?? null, flag }, 201);
    }
    if (method === "POST" && sub === "/issues") {
      const body = JSON.parse(req.postData() || "{}");
      return json({ created: true, flag: { id: 98, vehicle_id: 4, source: body.source, severity: body.severity, message: body.message, ref: body.ref ?? null, photo_ref: body.photo_ref ?? null, reported_by: "Rishi", occurred_at: null, created_at: NOW, resolved_at: null, resolved_by: null, resolution_note: null } }, 201);
    }
    if (method === "DELETE") return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*" } });
    if (method === "POST" || method === "PUT") return json({ id: 97, ok: true, ...(JSON.parse(req.postData() || "{}")) }, method === "POST" ? 201 : 200);
    unmocked.add(`${method} ${p}`);
    return json({});
  };
}

const violations = [];
const written = [];

async function newPage(browser, { width, height, dsf = 1, role = "admin", data, scorecardEnabled, detailDelay, detailFor: df, tab = "maintenance" }) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: dsf, locale: "en-PH", timezoneId: "Asia/Manila" });
  await context.route(/rgf-logistics-backend\.onrender\.com/, (r) => r.abort());
  await context.route(`${API}/**`, makeHandler({ role, data, scorecardEnabled, detailDelay, detailFor: df }));
  await context.route(/googleapis\.com\/maps|maps\.gstatic/, (r) => r.abort());
  const page = await context.newPage();
  await page.clock.setFixedTime(new Date(NOW));
  await page.addInitScript(([t]) => {
    try {
      localStorage.setItem("if-access-token", "fake-token");
      localStorage.setItem("fh-tab", t);
    } catch {}
  }, [tab]);
  page.on("pageerror", (e) => console.log("  [pageerror]", e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !/Failed to load resource|WebSocket|net::ERR|fonts\.g/.test(m.text())) console.log("  [console.error]", m.text().slice(0, 240));
  });
  return { context, page };
}

async function goto(page, ready = '[data-testid="fh-truck-list"], [data-testid="fh-leaderboard"]') {
  await page.goto(`${BASE}/app/fleet-health`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="fleet-health-page"]', { timeout: 30000 });
  await page.waitForSelector(ready, { timeout: 30000 });
  await page.waitForTimeout(900);
}

async function shot(page, name, width, opts = {}) {
  if (ONLY && !name.startsWith(ONLY) && !ONLY.split(",").some((o) => name.startsWith(o))) return;
  const file = path.join(OUT, `${name}-${width}.png`);
  if (opts.element) await opts.element.screenshot({ path: file, animations: "disabled" });
  else await page.screenshot({ path: file, fullPage: opts.full ?? false, animations: "disabled" });
  written.push(path.basename(file));
  const { sw, iw } = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth }));
  if (width <= 480 && sw > iw) violations.push(`${name}-${width}: scrollWidth ${sw} > innerWidth ${iw}`);
  if (width <= 480) {
    const offenders = await page.evaluate(() => {
      const iw = window.innerWidth;
      const out = [];
      const clipped = (el) => { for (let p = el.parentElement; p; p = p.parentElement) { const o = getComputedStyle(p).overflowX; if ((o === "auto" || o === "scroll" || o === "hidden" || o === "clip") && p !== document.body && p !== document.documentElement) return true; } return false; };
      for (const el of document.querySelectorAll("body *")) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > iw + 1 && !clipped(el) && getComputedStyle(el).position !== "fixed") out.push((el.tagName + "." + String(el.className).slice(0, 50)).slice(0, 80) + " right=" + Math.round(r.right));
        if (out.length > 4) break;
      }
      return out;
    });
    if (offenders.length) violations.push(`${name}-${width}: elements past the viewport edge: ${offenders.join(" | ")}`);
  }
  console.log(`  shot ${path.basename(file)}${width <= 480 && sw > iw ? "  <-- HORIZONTAL SCROLL" : ""}`);
}

async function closeTop(page) {
  await page.keyboard.press("Escape");
  await page.waitForTimeout(350);
}

async function openDrawer(page, plate, mobile) {
  const sel = mobile ? `[data-testid="fh-card-${plate}"] button[aria-label^="Open"]` : `[data-testid="fh-open-${plate}"]`;
  await page.locator(sel).locator("visible=true").first().click();
  await page.waitForSelector('[data-testid="fh-truck-drawer"] [data-testid="fh-drawer-tab-overview"]', { timeout: 15000 });
  await page.waitForTimeout(700);
}

async function drawerTab(page, tab) {
  await page.locator(`[data-testid="fh-drawer-tab-${tab}"]`).click();
  await page.waitForTimeout(700);
}

function emptyData() {
  const kp = { ...fx.summary.kpis, trucks_needing_attention: 0, services_overdue: 0, open_issues: 0, fleet_kmpl_30d: null, co2_month_kg: null, co2_month_litres: null, avg_eco_score_last_week: null, eco_scored_drivers: 0 };
  return {
    ...fx,
    summary: { ...fx.summary, kpis: kp, needs_attention: [], intervals_unconfirmed: false },
    trucks: { ...fx.trucks, intervals_unconfirmed: false, trucks: fx.trucks.trucks.filter((t) => t.kind === "third_party").slice(0, 2) },
    eco_drivers: { ...fx.eco_drivers, drivers: [], summary: { avg_score: null, scored: 0, not_enough_data: 0 }, unassigned: { km: 0, days: 0, note: fx.eco_drivers.unassigned.note } },
    fuel_logs: { count: 0, logs: [] },
    fuel_checks: { ...fx.fuel_checks, sensor_checks: [], log_checks: [] },
    co2: { ...fx.co2, months: [] },
    scorecard: { ...fx.scorecard, preview: { ...fx.scorecard.preview, items: [], will_send_count: 0 } },
  };
}

function notEnoughData() {
  const d = structuredClone(fx.eco_drivers);
  d.drivers.push({ score: null, status: "not_enough_data", reason: "needs at least 50 km in the week (has 31 km)", totals: { ...d.drivers[0].totals, km: 31, assigned_km: 31, unassigned_km: 0 }, breakdown: [], staff_id: 21, trucks: ["NAN9911"], kmpl: null, name: "Mario Santos", band: "none", previous_score: null, trend: null, rank: null });
  d.summary.not_enough_data = 1;
  const sc = structuredClone(fx.scorecard);
  sc.preview.items.push({ staff_id: 21, name: "Mario Santos", score: null, trucks: ["NAN9911"], message: null, has_phone: true, will_send: false, skip_reason: "Not enough data this week" });
  sc.preview.items.push({ staff_id: 22, name: "Lito Ramos", score: 91, trucks: ["NFX5791"], message: "Hi Lito! Eco-driving score mo this week: 91/100 (+4 vs last week). Ituloy lang ang maayos na pagmamaneho! - RGF Logistics", has_phone: false, will_send: false, skip_reason: "No phone number on file" });
  return { ...fx, eco_drivers: d, scorecard: sc };
}

async function run() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  for (const [width, height, dsf] of [[1440, 900, 1], [390, 844, 2]]) {
    const mobile = width < 500;
    console.log(`\n== ${width}x${height}`);
    const { context, page } = await newPage(browser, { width, height, dsf, data: notEnoughData(), scorecardEnabled: false });
    await goto(page);

    // 01 maintenance
    await shot(page, "01-maintenance", width, { full: true });
    await shot(page, "01a-maintenance-top", width);

    // third-party rows
    const tp = page.locator('[data-testid="fh-table"], [data-testid="fh-cards"]').first();
    await page.locator(mobile ? '[data-testid="fh-card-ASIAN CONNECT"]' : '[data-testid="fh-row-ASIAN CONNECT"]').locator("visible=true").first().scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    await shot(page, "07-third-party-rows", width);

    // tooltip on risk badge + service bar
    await page.evaluate(() => window.scrollTo(0, 0));
    const riskBtn = page.locator('[data-testid="fh-row-NFX5791"] button[aria-label^="Risk"], [data-testid="fh-card-NFX5791"] button[aria-label^="Risk"]').locator("visible=true").first();
    await riskBtn.scrollIntoViewIfNeeded();
    await riskBtn.click();
    await page.waitForTimeout(500);
    await shot(page, "09-risk-tooltip", width);
    await page.keyboard.press("Escape");
    const oilBtn = page.locator('[data-testid="fh-row-NFX5791"] button[aria-label^="Oil"], [data-testid="fh-card-NFX5791"] button[aria-label^="Oil"]').locator("visible=true").first();
    await oilBtn.click();
    await page.waitForTimeout(500);
    await shot(page, "09b-service-tooltip", width);
    await page.mouse.click(5, 5);
    await page.waitForTimeout(300);

    // 03 drawer
    await openDrawer(page, "NFX5791", mobile);
    await shot(page, "03-truck-drawer", width);
    for (const [tab, name] of [["services", "03a-drawer-services"], ["history", "03b-drawer-history"], ["issues", "03c-drawer-issues"], ["checklists", "03d-drawer-checklists"], ["fuel", "03e-drawer-fuel"], ["battery", "03f-drawer-battery"], ["distance", "03g-drawer-distance"]]) {
      await drawerTab(page, tab);
      await shot(page, name, width);
    }
    // report issue form + interval editor from drawer
    await drawerTab(page, "issues");
    await page.locator('[data-testid="fh-report-issue"]').click();
    await page.waitForTimeout(500);
    await page.locator('[data-testid="fh-issue-form"] textarea').fill("Brake pedal feels soft since this morning");
    await shot(page, "10-issue-form", width);
    await closeTop(page);
    await drawerTab(page, "services");
    await page.locator('[data-testid="fh-edit-intervals"]').click();
    await page.waitForTimeout(600);
    await shot(page, "06-interval-editor-truck", width);
    await closeTop(page);
    await page.locator('[data-testid="fh-service-oil_change"] button:has-text("Log service")').click();
    await page.waitForTimeout(500);
    await shot(page, "11-record-form", width);
    await closeTop(page);
    await closeTop(page);

    // repair truck NAJ6018 drawer
    await openDrawer(page, "NAJ6018", mobile);
    await shot(page, "03h-drawer-repair-truck", width);
    await drawerTab(page, "history");
    await shot(page, "03i-drawer-repair-history", width);
    await closeTop(page);

    // motorcycle drawer
    await openDrawer(page, "Motorcycle 1", mobile);
    await drawerTab(page, "services");
    await shot(page, "03j-drawer-motorcycle", width);
    await closeTop(page);

    // third-party drawer
    await page.locator(mobile ? '[data-testid="fh-card-ASIAN CONNECT"]' : '[data-testid="fh-row-ASIAN CONNECT"] button').locator("visible=true").first().click();
    await page.waitForTimeout(700);
    await shot(page, "03k-drawer-third-party", width);
    await closeTop(page);

    // interval editor (fleet) from banner
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.locator('[data-testid="fh-intervals-banner"] button').click();
    await page.waitForTimeout(600);
    await shot(page, "06-interval-editor", width);
    await closeTop(page);

    // 04 checklist form
    await page.locator('[data-testid="fh-new-checklist-top"]').click();
    await page.waitForTimeout(500);
    await shot(page, "04-checklist-form-empty", width);
    await page.locator('[data-testid="fh-checklist-form"] select').selectOption({ label: "DCD8953 · reefer" });
    await page.locator('[data-testid="fh-checklist-form"] [role=radiogroup][aria-label="Leaks"] button:has-text("Issue")').click();
    await page.locator('[data-testid="fh-checklist-form"] input[inputmode=decimal]').fill("-18.5");
    await page.waitForTimeout(300);
    await shot(page, "04-checklist-form", width);
    await page.locator('[data-testid="fh-checklist-form"] [data-testid="fh-checklist-outcome"]').scrollIntoViewIfNeeded();
    await page.locator('[data-testid="fh-checklist-submit"]').click();
    await page.waitForSelector('[data-testid="fh-checklist-done"]');
    await page.waitForTimeout(400);
    await shot(page, "04b-checklist-done", width);
    await closeTop(page);

    // 02 eco driving
    await page.getByRole("tab", { name: "Eco driving" }).click();
    await page.waitForSelector('[data-testid="fh-leaderboard"]');
    await page.waitForTimeout(1200);
    await shot(page, "02-eco-driving", width, { full: true });
    await page.evaluate(() => window.scrollTo(0, 0));
    await shot(page, "02a-eco-top", width);

    // score tooltip
    const scoreBtn = page.locator('[data-testid="fh-driver-8"] button[aria-label*="eco score"], [data-testid="fh-driver-cards"] button[aria-label*="eco score"]').locator("visible=true").first();
    await scoreBtn.scrollIntoViewIfNeeded();
    await scoreBtn.click();
    await page.waitForTimeout(500);
    await shot(page, "09c-eco-score-tooltip", width);
    await page.mouse.click(5, 5);

    // scorecard + sections
    await shot(page, "08-scorecard", width, { element: page.locator('[data-testid="fh-scorecard"]') });
    await shot(page, "12-fuel-log", width, { element: page.locator('[data-testid="fh-fuel-log"]') });
    await shot(page, "13-fuel-checks", width, { element: page.locator('[data-testid="fh-fuel-checks"]') });
    await shot(page, "14-co2", width, { element: page.locator('[data-testid="fh-co2"]') });
    await page.locator('[data-testid="fh-co2"] button[role=tab]:has-text("By truck")').click();
    await page.waitForTimeout(300);
    await shot(page, "14b-co2-by-truck", width, { element: page.locator('[data-testid="fh-co2"]') });

    // scorecard switch confirmation
    await page.locator('[data-testid="fh-scorecard-switch"]').scrollIntoViewIfNeeded();
    await page.locator('[data-testid="fh-scorecard-switch"]').click();
    await page.waitForTimeout(500);
    await shot(page, "08b-scorecard-confirm", width);
    await closeTop(page);

    // truck view
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.getByRole("tab", { name: "Trucks" }).click();
    await page.waitForSelector('[data-testid="fh-truck-view"] [data-testid^="fh-eco-truck"], [data-testid="fh-truck-view-cards"]', { state: "attached" });
    await page.waitForTimeout(700);
    await shot(page, "02b-eco-trucks", width, { full: true });

    // 05 fuel log form
    await page.locator('[data-testid="fh-add-fuel"]').scrollIntoViewIfNeeded();
    await page.locator('[data-testid="fh-add-fuel"]').click();
    await page.waitForTimeout(500);
    await shot(page, "05-fuel-log-form-empty", width);
    await page.locator('[data-testid="fh-fuel-form"] select').selectOption({ label: "DCD8953" });
    const inputs = page.locator('[data-testid="fh-fuel-form"] input[inputmode=decimal]');
    await inputs.nth(0).fill("220");
    await inputs.nth(1).fill("13530");
    await inputs.nth(2).fill("45100");
    await page.waitForTimeout(300);
    await shot(page, "05-fuel-log-form", width);
    await closeTop(page);
    await context.close();
  }

  // empty states
  for (const [width, height, dsf] of [[1440, 900, 1], [390, 844, 2]]) {
    console.log(`\n== empty states ${width}`);
    const { context, page } = await newPage(browser, { width, height, dsf, data: emptyData() });
    await goto(page);
    await shot(page, "15-empty-maintenance", width, { full: true });
    await page.getByRole("tab", { name: "Eco driving" }).click();
    await page.waitForSelector('[data-testid="fh-leaderboard"]');
    await page.waitForTimeout(1000);
    await shot(page, "15b-empty-eco", width, { full: true });
    await context.close();
  }

  // loading skeleton + error
  {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await ctx.route(`${API}/**`, async (route) => {
      const p = new URL(route.request().url()).pathname;
      if (p === "/auth/session") return route.fulfill({ status: 200, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(user("admin")) });
      await new Promise((r) => setTimeout(r, 60000));
      return route.abort();
    });
    const page = await ctx.newPage();
    await page.addInitScript(() => { try { localStorage.setItem("if-access-token", "fake"); } catch {} });
    await page.goto(`${BASE}/app/fleet-health`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-testid="fleet-health-page"]', { timeout: 30000 });
    await page.waitForTimeout(800);
    await shot(page, "16-loading-skeletons", 1440);
    await ctx.close();
  }
  {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await ctx.route(`${API}/**`, async (route) => {
      const p = new URL(route.request().url()).pathname;
      const h = { "access-control-allow-origin": "*" };
      if (p === "/auth/session") return route.fulfill({ status: 200, contentType: "application/json", headers: h, body: JSON.stringify(user("admin")) });
      return route.fulfill({ status: 500, contentType: "application/json", headers: h, body: JSON.stringify({ detail: "The fleet health service is temporarily unavailable." }) });
    });
    const page = await ctx.newPage();
    await page.addInitScript(() => { try { localStorage.setItem("if-access-token", "fake"); } catch {} });
    await page.goto(`${BASE}/app/fleet-health`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-testid="fleet-health-page"]', { timeout: 30000 });
    await page.waitForSelector('[role=alert]', { timeout: 20000 });
    await page.waitForTimeout(600);
    await shot(page, "17-error-state", 1440);
    await ctx.close();
  }

  // warehouse role
  for (const [width, height, dsf] of [[1440, 900, 1], [390, 844, 2]]) {
    const mobile = width < 500;
    console.log(`\n== warehouse ${width}`);
    const { context, page } = await newPage(browser, { width, height, dsf, role: "warehouse", data: fx });
    await goto(page);
    const checks = {};
    checks.checklistButton = await page.locator('[data-testid="fh-new-checklist-top"]').count();
    checks.intervalsButton = await page.getByRole("button", { name: /Service intervals/ }).count();
    checks.reviewIntervals = await page.locator('[data-testid="fh-intervals-banner"] button').count();
    await shot(page, "18-warehouse-maintenance", width, { full: true });
    await openDrawer(page, "NFX5791", mobile);
    checks.logService = await page.locator('[data-testid="fh-log-service"]').count();
    checks.drawerChecklist = await page.getByRole("button", { name: "New pre-trip check" }).count();
    checks.drawerReport = await page.getByRole("button", { name: "Report an issue" }).count();
    await shot(page, "18a-warehouse-drawer", width);
    await drawerTab(page, "issues");
    checks.resolveButtons = await page.getByRole("button", { name: "Resolve" }).count();
    checks.reportIssue = await page.locator('[data-testid="fh-report-issue"]').count();
    await drawerTab(page, "history");
    checks.editRecord = await page.getByRole("button", { name: /Edit record/ }).count();
    checks.addRecord = await page.getByRole("button", { name: "Add record" }).count();
    await closeTop(page);
    await page.getByRole("tab", { name: "Eco driving" }).click();
    await page.waitForSelector('[data-testid="fh-leaderboard"]');
    await page.waitForTimeout(900);
    checks.addFill = await page.locator('[data-testid="fh-add-fuel"]').count();
    checks.editFill = await page.getByRole("button", { name: /Edit fill-up/ }).count();
    checks.deleteFill = await page.getByRole("button", { name: /Delete fill-up/ }).count();
    checks.scorecardSwitch = await page.locator('[data-testid="fh-scorecard-switch"]').count();
    await shot(page, "18b-warehouse-eco", width, { full: true });
    console.log("  warehouse checks:", JSON.stringify(checks));
    const expect = { checklistButton: 1, intervalsButton: 0, reviewIntervals: 0, logService: 0, drawerChecklist: 1, drawerReport: 1, resolveButtons: 0, reportIssue: 1, editRecord: 0, addRecord: 0, addFill: 1, editFill: 0, deleteFill: 0, scorecardSwitch: 0 };
    for (const [k, v] of Object.entries(expect)) if (checks[k] !== v && !(mobile && ["editFill", "deleteFill"].includes(k))) violations.push(`warehouse-${width}: ${k} expected ${v} got ${checks[k]}`);
    await context.close();
  }

  // admin: scorecard ON + paused whatsapp variant
  {
    const data = structuredClone(fx);
    data.scorecard.switch.whatsapp_active = true;
    const { context, page } = await newPage(browser, { width: 1440, height: 900, dsf: 1, data, scorecardEnabled: true, tab: "eco" });
    await goto(page, '[data-testid="fh-scorecard"]');
    await page.waitForTimeout(800);
    await shot(page, "08c-scorecard-on", 1440, { element: page.locator('[data-testid="fh-scorecard"]') });
    await context.close();
  }

  await browser.close();
  console.log("\nunmocked requests:", [...unmocked]);
  console.log("written:", written.length, "files");
  if (violations.length) {
    console.log("\nVIOLATIONS:");
    for (const v of violations) console.log("  -", v);
    process.exitCode = 1;
  } else console.log("\nNo violations: no horizontal scroll at 390 px; warehouse role view as expected.");
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
