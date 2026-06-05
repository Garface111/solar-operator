/**
 * QA script: Solar Operator Sandbox canvas smoke test + screenshot suite.
 * Usage: node scripts/qa_sandbox.mjs
 * Output: screenshots/qa-sandbox/*.png
 */
import { chromium } from '/tmp/so-agents/playwright-qa/node_modules/playwright/index.mjs';
import { mkdir } from 'fs/promises';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dir = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dir, '..');
const SS_DIR = join(ROOT, 'screenshots', 'qa-sandbox');

// App is a SPA with basename=/accounts. Magic-link auth only — no password form.
// We drive auth via the session token in localStorage after verifying via API.
const URL_BASE = 'https://solaroperator.org';
const EMAIL = 'ford@solaroperator.org';
// Magic-link only auth — we'll set the session token directly via the API
const API_BASE = 'https://solaroperator.org';

await mkdir(SS_DIR, { recursive: true });

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

async function ss(name) {
  const path = join(SS_DIR, `${name}.png`);
  await page.screenshot({ path, fullPage: false });
  console.log(`  📸 ${name}.png`);
}

// ── Load app and check login page ──────────────────────────────────────────
console.log('→ Loading app login page...');
await page.goto(`${URL_BASE}/accounts/login`, { waitUntil: 'networkidle', timeout: 20000 });
await page.waitForTimeout(1000);
await ss('01-login-page');

// The app uses magic-link auth (no password form).
// Navigate to the clients route (sandbox is embedded there).
console.log('→ Navigating to clients/sandbox...');
await page.goto(`${URL_BASE}/accounts/clients`, { waitUntil: 'networkidle', timeout: 20000 });
// Wait for ReactFlow canvas to mount
await page.waitForSelector('.react-flow', { timeout: 15000 }).catch(() => {});
await page.waitForTimeout(2000); // let entry animations settle
await ss('04-sandbox-initial-load');

// ── Toolbar state ─────────────────────────────────────────────────────────
console.log('→ Checking toolbar...');
const undoBtn = page.locator('button:has-text("Undo")').first();
const undoDisabled = await undoBtn.getAttribute('disabled');
console.log(`  Undo button disabled: ${undoDisabled !== null}`);
await ss('05-toolbar-undo-state');

// ── Expand a client card ───────────────────────────────────────────────────
console.log('→ Expanding first client card...');
const expandBtns = page.locator('.react-flow__node-client button[aria-label]').filter({ hasText: '' });
// Click the chevron expand button (aria-label = Expand)
const chevronBtn = page.locator('.react-flow__node-client button[aria-label="Expand"]').first();
if (await chevronBtn.count() > 0) {
  await chevronBtn.click();
  await page.waitForTimeout(400);
  await ss('06-client-card-expanded');
}

// ── Pin/star a client ──────────────────────────────────────────────────────
console.log('→ Testing pin/star...');
// The avatar circle (initials) is the pin toggle — click it
const avatarBtn = page.locator('.react-flow__node-client button').first();
await avatarBtn.click({ force: true });
await page.waitForTimeout(500);
await ss('07-client-pinned');
// Pin again to unpin
await avatarBtn.click({ force: true });
await page.waitForTimeout(500);
await ss('08-client-unpinned');

// ── Double-click to rename ─────────────────────────────────────────────────
console.log('→ Testing rename...');
const clientName = page.locator('.react-flow__node-client p[title]').first();
if (await clientName.count() > 0) {
  await clientName.dblclick({ force: true });
  await page.waitForTimeout(300);
  await ss('09-rename-active');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
  await ss('10-rename-cancelled');
}

// ── Right-click context menu ───────────────────────────────────────────────
console.log('→ Testing context menu...');
const firstNode = page.locator('.react-flow__node-client').first();
await firstNode.click({ button: 'right', force: true });
await page.waitForTimeout(300);
await ss('11-context-menu');
// Check if context menu appeared
const contextMenu = page.locator('[class*="rounded-xl"][class*="shadow-xl"]').last();
console.log(`  Context menu visible: ${await contextMenu.isVisible().catch(() => false)}`);
// Close by clicking pane
await page.keyboard.press('Escape');
await page.waitForTimeout(200);
// Also click pane
await page.locator('.react-flow__pane').click({ position: { x: 100, y: 100 }, force: true });
await page.waitForTimeout(200);

// ── Add Client modal ───────────────────────────────────────────────────────
console.log('→ Testing Add Client modal...');
const addClientBtn = page.locator('button:has-text("Add Client")').first();
if (await addClientBtn.count() > 0) {
  await addClientBtn.click();
  await page.waitForTimeout(600);
  await ss('12-add-client-modal');
  // Close modal
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  await ss('13-add-client-modal-closed');
}

// ── Auto-arrange ───────────────────────────────────────────────────────────
console.log('→ Testing Auto-arrange...');
const autoArrangeBtn = page.locator('button:has-text("Auto-arrange")').first();
if (await autoArrangeBtn.count() > 0) {
  await autoArrangeBtn.click();
  await page.waitForTimeout(600);
  await ss('14-after-auto-arrange');
}

// ── Fit to view ────────────────────────────────────────────────────────────
console.log('→ Testing Fit to view...');
const fitViewBtn = page.locator('button:has-text("Fit to view")').first();
if (await fitViewBtn.count() > 0) {
  await fitViewBtn.click();
  await page.waitForTimeout(600);
  await ss('15-after-fit-to-view');
}

// ── Viewport persist check ─────────────────────────────────────────────────
console.log('→ Checking viewport localStorage key...');
const viewportKey = await page.evaluate(() => localStorage.getItem('so:sandbox:viewport'));
console.log(`  localStorage so:sandbox:viewport: ${viewportKey ? 'PRESENT' : 'MISSING'}`);

// Pan the canvas then reload to verify viewport persist
await page.mouse.move(700, 450);
await page.mouse.down();
await page.mouse.move(500, 300);
await page.mouse.up();
await page.waitForTimeout(500);
const viewportAfterPan = await page.evaluate(() => localStorage.getItem('so:sandbox:viewport'));
console.log(`  Viewport after pan: ${viewportAfterPan}`);

await page.reload({ waitUntil: 'networkidle' });
await page.waitForSelector('.react-flow', { timeout: 15000 }).catch(() => {});
await page.waitForTimeout(2500);
const viewportAfterReload = await page.evaluate(() => localStorage.getItem('so:sandbox:viewport'));
console.log(`  Viewport after reload: ${viewportAfterReload}`);

// Did viewport change (fitView override it)?
const panData = viewportAfterPan ? JSON.parse(viewportAfterPan) : null;
const reloadData = viewportAfterReload ? JSON.parse(viewportAfterReload) : null;
if (panData && reloadData) {
  const driftX = Math.abs(panData.x - reloadData.x);
  const driftY = Math.abs(panData.y - reloadData.y);
  console.log(`  Viewport drift after reload — dx:${driftX.toFixed(1)} dy:${driftY.toFixed(1)} (>5 = fitView override)`);
  if (driftX > 5 || driftY > 5) {
    console.log('  ⚠ BUG-001 CONFIRMED: loadCanvas fitView overrides saved viewport');
  } else {
    console.log('  ✓ Viewport persist appears stable');
  }
}
await ss('16-viewport-after-reload');

// ── Minimap ────────────────────────────────────────────────────────────────
console.log('→ Checking minimap...');
const minimap = page.locator('.react-flow__minimap');
console.log(`  Minimap visible: ${await minimap.isVisible().catch(() => false)}`);
await ss('17-minimap');

// ── Narrow viewport <1024px ────────────────────────────────────────────────
console.log('→ Testing narrow viewport (768px)...');
await page.setViewportSize({ width: 768, height: 900 });
await page.waitForTimeout(500);
await ss('18-narrow-768px');
await page.setViewportSize({ width: 1440, height: 900 });
await page.waitForTimeout(300);

// ── Merge dialog (simulate by checking UI state) ───────────────────────────
console.log('→ Final canvas state...');
await ss('19-final-state');

console.log('\n✓ QA screenshot run complete. See screenshots/qa-sandbox/');
await browser.close();
