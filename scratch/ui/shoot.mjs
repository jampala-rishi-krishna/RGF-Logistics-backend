// Renders Confirmed SO with mocked API data and saves screenshots + measured control heights.
import { createRequire } from "node:module";
import fs from "node:fs";
const require = createRequire("C:/Users/dexte/Downloads/RGF/RGF/projects/IntelliFleet-Logistics-Platform/IntelliFleet-Logistics-Platform/artifacts/intellifleet/package.json");
const { chromium } = require("playwright");
const OUT = "C:/Users/dexte/Downloads/RGF/RGF/projects/IntelliFleet-Logistics-Platform/IntelliFleet-Logistics-Platform/backend/scratch/ui";

const order = (n) => ({
  id: `id${n}`, salesorder_number: `SO26-${18000 + n}`, customer_name: ["EMBLUJU INC.", "Denny's Philippines", "Golden Arches Dev Corp", "Jollibee Foods Corp"][n % 4] + ` #${n}`,
  order_status: "confirmed", expected_shipment_date: "2026-10-06", total: 12500.5 * n, synced_at: "2026-10-05T07:00:00Z",
  assignment_status: "assigned", vehicle_id: n % 2 ? "NFX5791" : "DCD8953", driver_id: 3, driver_name: "Juan Dela Cruz",
  shipping_city: "Makati", shipping_address: { address: "12 Ayala Ave", city: "Makati", state: "NCR" },
  notes: "Deliver before 9am", product_count: 2, zoho_lock: n % 3 === 0 ? { is_locked: true, config_name: "For Fulfillment" } : { is_locked: false },
  products: [{ name: "Wagyu Striploin", sku: "WS-1", quantity: 4, unit: "case", total_weight_kg: 40 + n, line_item_id: `l${n}a`, item_id: `i${n}a`, quantity_packed: 0, quantity_shipped: 0 }, { name: "Fries", sku: "FR-2", quantity: 2, unit: "case", total_weight_kg: n % 2 ? null : 20, line_item_id: `l${n}b`, item_id: `i${n}b`, quantity_packed: 0, quantity_shipped: 0 }],
  raw_json: { line_items: [{ line_item_id: `l${n}a`, item_id: `i${n}a`, sku: "WS-1", quantity: 4, unit: "case", location_name: "Mets" }, { line_item_id: `l${n}b`, item_id: `i${n}b`, sku: "FR-2", quantity: 2, unit: "case", location_name: "Glacier" }] },
});

async function run(browser, width, rows, { kpi = false, name }) {
  const ctx = await browser.newContext({ viewport: { width, height: width < 700 ? 900 : 900 } });
  const page = await ctx.newPage();
  await page.addInitScript((mode) => { localStorage.setItem("if-access-token", "t"); localStorage.setItem("intellifleet.confirmedSo.viewMode", mode); }, kpi ? "kpi" : "spreadsheet");
  await page.route("http://mock.test/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body) => route.fulfill({ status: 200, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*" } });
    if (url.pathname.endsWith("/auth/session")) return json({ user: { id: "1", fullName: "Pau", email: "p@x.com", role: "admin", status: "active" } });
    if (url.pathname.endsWith("/inventory/sales-orders")) return json({ items: rows, page: 1, per_page: 100, total: rows.length, has_more: false, stock_pending: false });
    return json({});
  });
  await page.goto("http://127.0.0.1:5199/app/loads");
  const TAB = process.env.TAB || "Confirmed SO";
  await page.getByRole("button", { name: TAB, exact: true }).click();
  await page.waitForSelector('[data-testid="lp-toolbar"]');
  await page.waitForTimeout(rows.length ? 1200 : 800);
  const heights = await page.evaluate(() => {
    const h = (el) => (el ? Math.round(el.getBoundingClientRect().height * 10) / 10 : null);
    const tb = document.querySelector('[data-testid="lp-toolbar"]');
    const q = (sel) => tb.querySelector(sel);
    const btn = (t) => [...tb.querySelectorAll("button")].find((b) => b.textContent.trim().startsWith(t));
    const fs = (el) => (el ? getComputedStyle(el).fontSize : null);
    const inputs = [...tb.querySelectorAll("input[type=date]")];
    return {
      "Search": [h(q('input[aria-label^="Search"]')), fs(q('input[aria-label^="Search"]'))],
      "From": [h(inputs[0]), fs(inputs[0])], "To": [h(inputs[1]), fs(inputs[1])], "Order status": [h(q("select")), fs(q("select"))], "Weight text": [fs(q('[data-testid="load-planning-total-weight"]'))],
      "Cities": [h(btn("All cities")), fs(btn("All cities"))], "Export": [h(btn("Export")), fs(btn("Export"))], "Send to Email": [h(btn("Send to Email")), fs(btn("Send to Email"))],
      "Acknowledge": [h(btn("Acknowledge")), fs(btn("Acknowledge"))], "Refresh": [h(btn("Refresh")), fs(btn("Refresh"))],
      "Toggle (box)": [h(q('[data-testid="view-toggle"]')), fs(q('[data-testid="view-toggle"] button'))],
      "Toggle btn widths": [...tb.querySelectorAll('[data-testid="view-toggle"] button')].map((b) => Math.round(b.getBoundingClientRect().width)),
      "scrollWidth>clientWidth (page)": document.documentElement.scrollWidth > document.documentElement.clientWidth,
    };
  });
  await page.screenshot({ path: `${OUT}/${process.env.TAB ? "loadplanning-" : ""}${name}.png` });
  const cards = await page.locator('[data-testid="sales-order-card"]').count();
  await ctx.close();
  return { name, width, rows: rows.length, cards, heights };
}

const browser = await chromium.launch();
const eight = Array.from({ length: 8 }, (_, i) => order(i + 1));
const results = [];
for (const w of (process.env.W ? [Number(process.env.W)] : [1440, 1024, 390])) {
  results.push(await run(browser, w, [], { name: `confirmed-0rows-${w}` }));
  results.push(await run(browser, w, eight, { name: `confirmed-8rows-${w}` }));
}
for (const w of (process.env.W ? [] : [1440, 390])) results.push(await run(browser, w, eight, { kpi: true, name: `kpi-8rows-${w}` }));
await browser.close();
fs.writeFileSync(`${OUT}/measurements.json`, JSON.stringify(results, null, 1));
for (const r of results) console.log(r.name, "cards=" + r.cards, JSON.stringify(r.heights));
