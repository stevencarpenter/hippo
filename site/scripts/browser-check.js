// Run against `pnpm preview` with Playwright CLI:
// playwright-cli run-code "$(cat site/scripts/browser-check.js)"
async (page) => {
  const base = "http://127.0.0.1:4321";
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const routes = ["/", "/install", "/privacy", "/faq", "/status", "/search", "/docs", "/docs/contributing", "/docs/reference/schema"];
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const route of routes) {
      await page.goto(base + route);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${route} overflows at ${width}px`);
      if (width <= 768 && route.startsWith("/docs")) {
        assert(await page.locator("main").evaluate(el => el.getBoundingClientRect().top < 400), "Mobile docs must start above the fold");
      }
    }
  }
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto(base + "/docs");
  await page.locator("body").evaluate(el => { el.dataset.outline = "none"; });
  assert(await page.locator("main").evaluate(el => el.clientWidth > innerWidth - 50), "Docs without an outline must retain one mobile column");
  await page.getByText("Sections", { exact: true }).click();
  assert(await page.locator(".rail-details").evaluate(el => el.open), "Sections must expand");
  await page.getByText("Sections", { exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 900 });
  assert(await page.locator(".rail-details").evaluate(el => el.open), "Desktop sections must stay available");

  await page.goto(base);
  await page.keyboard.press("Tab");
  assert(await page.getByRole("link", { name: "Skip to content" }).evaluate(el => el === document.activeElement), "Skip link must be first");
  await page.keyboard.press("Enter");
  assert(await page.locator("main").evaluate(el => el === document.activeElement), "Skip link must focus content");
  await page.getByRole("button", { name: "Dark theme" }).click();
  const theme = await page.locator("html").getAttribute("data-theme");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Install", exact: true }).click();
  assert(await page.locator("html").getAttribute("data-theme") === theme, "Theme must persist during navigation");
  await page.evaluate(() => {
    window.copiedText = "";
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async text => { window.copiedText = text; } } });
  });
  await page.getByRole("button", { name: "Copy code to clipboard" }).first().click();
  assert(await page.evaluate(() => window.copiedText === document.querySelector(".code-block pre").textContent), "Copy must preserve exact code");
  await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error("Permission denied"); }; });
  await page.getByRole("button", { name: "Copy code to clipboard" }).first().click();
  assert(await page.getByText("Copy failed. Select the code and copy it manually.").isVisible(), "Clipboard failure must explain recovery");

  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Search", exact: true }).click();
  await page.getByRole("textbox", { name: "Search the docs and field notes", exact: true }).fill("redaction");
  await page.locator(".pagefind-ui__result-link").first().waitFor();
  await page.locator(".pagefind-ui__result-link").first().click();
  assert(await page.evaluate(() => location.pathname !== "/search"), "Search results must navigate");
  await page.goBack();
  await page.getByRole("textbox", { name: "Search the docs and field notes", exact: true }).fill('"hfxqkjzv987654321"');
  await page.getByText(/No entries match/).waitFor();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(base);
  assert(await page.locator(".motif.breathe svg").evaluate(el => getComputedStyle(el).animationName === "none"), "Reduced motion must stop the hero animation");
  await page.emulateMedia({ reducedMotion: "no-preference" });

  const noJS = await page.context().browser().newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 900 } });
  const fallback = await noJS.newPage();
  await fallback.goto(base + "/search");
  assert(await fallback.getByText(/Search requires JavaScript/).isVisible(), "No-JS search needs a fallback");
  await fallback.goto(base + "/install");
  assert(await fallback.getByRole("button").count() === 0, "No-JS pages must hide inactive controls");
  await noJS.close();
  await page.route("**/pagefind/pagefind-ui.js", route => route.abort());
  await page.goto(base + "/search");
  assert(await page.getByText(/Search could not load/).isVisible(), "Blocked search assets need a fallback");
  await page.unroute("**/pagefind/pagefind-ui.js");
  await page.route(/\.pf_meta$/, route => route.abort());
  await page.goto(base + "/search");
  await page.getByRole("textbox", { name: "Search the docs and field notes", exact: true }).fill("redaction");
  await page.getByText(/Search is taking longer than expected/).waitFor({ timeout: 15000 });
  await page.unroute(/\.pf_meta$/);
  await page.reload();
  await page.getByRole("textbox", { name: "Search the docs and field notes", exact: true }).fill("redaction");
  await page.locator(".pagefind-ui__result-link").first().waitFor();

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(base + "/docs/reference/lifecycle");
  await page.evaluate(() => {
    const headings = [...document.querySelectorAll("main h2")];
    window.scrollTo(0, headings[1].getBoundingClientRect().top + scrollY - 100);
  });
  await page.waitForTimeout(300);
  await page.evaluate(() => {
    const headings = [...document.querySelectorAll("main h2")];
    window.scrollTo(0, (headings[0].getBoundingClientRect().top + headings[1].getBoundingClientRect().top) / 2 + scrollY);
  });
  await page.waitForTimeout(300);
  assert(await page.locator('[aria-current="location"]').getAttribute("data-heading-link") === await page.locator("main h2").first().getAttribute("id"), "Outline must follow upward scrolling within a long section");
  assert(await page.locator('.section:has(a[aria-current="page"])').evaluate(el => el.open), "Current docs section must be expanded");
  await page.setViewportSize({ width: 390, height: 900 });
  assert(await page.locator(".breadcrumb .sep:not(.seg-mid)").isVisible(), "Mobile breadcrumb needs a separator");
  assert(!await page.locator(".breadcrumb .seg-mid").last().isVisible(), "Mobile breadcrumb hides intermediate labels");

  await page.goto(base + "/docs/reference/schema");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ media: "print" });
  assert(await page.locator("pre, table").evaluateAll(els => els.every(el => el.scrollWidth <= el.clientWidth + 1)), "Print must wrap code and tables without clipping");
  await page.emulateMedia({ media: "screen" });
  await page.goto(base + "/install");
  const uninstall = page.locator('.code-block').filter({ has: page.locator('pre', { hasText: /^hippo daemon uninstall$/ }) });
  assert(await uninstall.count() === 1, "Uninstall copy block must contain only the uninstall command");
  assert(!await page.getByText("rm ~/.local/share/hippo/hippo.db", { exact: true }).isVisible(), "Data deletion must require opening its own disclosure");
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto(base);
  assert(await page.locator(".arch").evaluate(el => el.scrollWidth > el.clientWidth && el.tabIndex === 0), "Mobile diagram must scroll with keyboard access");
  return "36 route/viewport checks plus navigation, keyboard, theme, copy safety, search success/empty/failure/recovery, outline, breadcrumbs, diagram, reduced-motion, and no-JS checks passed.";
}
