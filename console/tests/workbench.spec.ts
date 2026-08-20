import { expect, Page, Route, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const scenarios = [
  { id: "text.nonstream", purpose: "Non-streaming text protocol", stream: false, tools: false, response_format: "text", fault: false },
  { id: "fault.rate_limit", purpose: "Provider rate-limit behavior", stream: false, tools: false, response_format: "text", fault: true },
  { id: "fault.timeout", purpose: "Provider timeout behavior", stream: false, tools: false, response_format: "text", fault: true },
  { id: "fault.partial_stream", purpose: "Partial SSE failure behavior", stream: true, tools: false, response_format: "text", fault: true },
];

const policy = {
  version: "policy-workbench",
  request_limit: 100,
  token_limit: 100000,
  budget_usd_micros: 1000000,
  price_table_version: "synthetic-v1",
};

const liveConfiguration = {
  proposed_targets: [
    { provider: "openai", model: "gpt-5-mini-2025-08-07" },
    { provider: "anthropic", model: "claude-haiku-4-5-20251001" },
  ],
  provider_processing_notice: "If live mode is later approved and armed, committed synthetic content is processed by the selected provider. Default API retention may be up to 30 days; exact account settings require same-session verification. Zero Data Retention is not claimed.",
};

function metadataHeaders(contentType = "application/json") {
  return {
    "content-type": contentType,
    "x-request-id": "chatcmpl-workbench",
    "x-trace-id": "trace-workbench",
    "x-gateway-route": "gateway/general",
    "x-gateway-provider": "simulator",
    "x-gateway-model": "simulator-v1",
    "x-gateway-attempts": "1",
    "x-gateway-cost-usd": "0.000000",
    "x-gateway-usage-status": "reconciled",
  };
}

async function mockWorkbench(
  page: Page,
  options: { role?: "owner" | "demo_operator"; catalog?: typeof scenarios; delayRun?: number } = {},
) {
  const role = options.role ?? "demo_operator";
  let liveSession: Record<string, unknown> | null = null;
  const runBodies: unknown[] = [];

  await page.route("**/v1/**", async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/v1/session") {
      await route.fulfill({ json: { email: `${role}@example.com`, role, tenant_id: "tenant_alpha" } });
      return;
    }
    if (path === "/v1/operations/status") {
      await route.fulfill({ json: { policy, live_session: liveSession, live_configuration: liveConfiguration } });
      return;
    }
    if (path === "/v1/workbench/scenarios") {
      await route.fulfill({ json: { scenarios: options.catalog ?? scenarios } });
      return;
    }
    if (path === "/v1/admin/live-session" && request.method() === "POST") {
      liveSession = {
        session_id: "session-workbench",
        state: "active",
        expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString(),
        request_limit: 20,
        requests_charged: 0,
        spend_limit_micros: 1000000,
        spend_charged_micros: 0,
        reserved_spend_micros: 12500,
        reconciled_spend_micros: 2500,
      };
      await route.fulfill({ json: liveSession });
      return;
    }
    if (path === "/v1/admin/live-session" && request.method() === "DELETE") {
      liveSession = null;
      await route.fulfill({ status: 204 });
      return;
    }
    if (path === "/v1/workbench/runs") {
      const body = request.postDataJSON();
      runBodies.push(body);
      if (options.delayRun) await new Promise((resolve) => setTimeout(resolve, options.delayRun));
      if (body.scenario_id === "fault.rate_limit") {
        await route.fulfill({
          status: 429,
          headers: metadataHeaders(),
          json: { error: { code: "quota_exceeded" } },
        });
        return;
      }
      if (body.scenario_id === "fault.timeout") {
        await route.fulfill({
          status: 504,
          headers: metadataHeaders(),
          json: { error: { code: "provider_timeout" } },
        });
        return;
      }
      if (body.scenario_id === "fault.partial_stream") {
        await route.fulfill({
          headers: metadataHeaders("text/event-stream"),
          body: [
            'data: {"choices":[{"delta":{"content":"Partial synthetic response"}}]}',
            "",
            'data: {"error":{"code":"provider_stream_error"}}',
            "",
            "data: [DONE]",
            "",
          ].join("\n"),
        });
        return;
      }
      await route.fulfill({
        headers: metadataHeaders(),
        json: {
          choices: [{ message: { role: "assistant", content: "Synthetic gateway response." } }],
          usage: { prompt_tokens: 12, completion_tokens: 9, total_tokens: 21 },
        },
      });
      return;
    }
    await route.abort("failed");
  });

  return { runBodies };
}

test("demo operator runs only a scenario ID and inspects transient content plus metadata", async ({ page }) => {
  const mocked = await mockWorkbench(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Choose a scenario" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Owner live-session control" })).toHaveCount(0);
  await expect(page.getByText("Read-only demo access.", { exact: false })).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await expect(page.getByText("openai / gpt-5-mini-2025-08-07", { exact: false })).toBeVisible();
  await expect(page.getByText("Zero Data Retention is not claimed.", { exact: false })).toBeVisible();

  const scenarioSelect = page.getByLabel("Synthetic scenario");
  await scenarioSelect.focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Run committed scenario" })).toBeFocused();
  await page.keyboard.press("Enter");

  await expect(page.getByLabel("Transient provider response")).toContainText("Synthetic gateway response.");
  await expect(page.getByText("chatcmpl-workbench")).toBeVisible();
  await expect(page.getByText("trace-workbench")).toBeVisible();
  expect(mocked.runBodies).toEqual([{ scenario_id: "text.nonstream" }]);
  expect(JSON.stringify(mocked.runBodies)).not.toContain("Authorization");
  expect(JSON.stringify(mocked.runBodies)).not.toContain("messages");
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });

  await page.reload();
  await expect(page.getByText("Select a committed scenario to begin.")).toBeVisible();
  await expect(page.getByText("Synthetic gateway response.")).toHaveCount(0);
});

test("loading, refusal, partial stream, and recovery states are announced", async ({ page }) => {
  await mockWorkbench(page, { delayRun: 75 });
  await page.goto("/");

  await page.getByLabel("Synthetic scenario").selectOption("fault.rate_limit");
  await page.getByRole("button", { name: "Run committed scenario" }).click();
  await expect(page.getByText("Request in progress.")).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("Request refused: quota_exceeded.");
  await expect(page.getByRole("button", { name: "Run again" })).toBeVisible();

  await page.getByLabel("Synthetic scenario").selectOption("fault.partial_stream");
  await page.getByRole("button", { name: "Run committed scenario" }).click();
  await expect(page.getByRole("alert")).toContainText("Partial response. Stream ended with provider_stream_error.");
  await expect(page.getByLabel("Transient provider response")).toContainText("Partial synthetic response");
  await expect(page.getByText("partial", { exact: true })).toBeVisible();

  await page.getByLabel("Synthetic scenario").selectOption("fault.timeout");
  await page.getByRole("button", { name: "Run committed scenario" }).click();
  await expect(page.getByRole("alert")).toContainText("Request failed: provider_timeout.");
  await expect(page.getByText("error", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run again" })).toBeVisible();
});

test("empty catalog has an explicit safe state", async ({ page }) => {
  await mockWorkbench(page, { catalog: [] });
  await page.goto("/");
  await expect(page.getByText("No committed scenarios are available.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Run committed scenario" })).toHaveCount(0);
});

test("owner must acknowledge all fixed caps before arming live mode", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Owner mutation is covered once in desktop Chromium.");
  await mockWorkbench(page, { role: "owner" });
  await page.goto("/");

  const arm = page.getByRole("button", { name: "Arm bounded live session" });
  await expect(arm).toBeDisabled();
  await page.getByRole("checkbox", { name: /30 minutes, 20 provider requests, or \$1.00/ }).check();
  await expect(arm).toBeEnabled();
  await arm.click();
  await expect(page.getByText("Live armed", { exact: true })).toBeVisible();
  await expect(page.getByText("Provider attempts", { exact: true })).toBeVisible();
  await expect(page.getByText("$0.0125", { exact: true })).toBeVisible();
  await expect(page.getByText("$0.0025", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop live session" })).toBeVisible();
});

test("Chromium accessibility tree exposes the workbench controls and live status", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Accessibility tree is covered once in desktop Chromium.");
  await mockWorkbench(page);
  await page.goto("/");
  await expect(page.getByRole("combobox", { name: "Synthetic scenario" })).toBeVisible();
  const cdp = await page.context().newCDPSession(page);
  const tree = await cdp.send("Accessibility.getFullAXTree");
  const rolesAndNames = tree.nodes.map((node) => `${node.role?.value}:${node.name?.value}`);
  expect(rolesAndNames).toContain("combobox:Synthetic scenario");
  expect(rolesAndNames).toContain("button:Run committed scenario");
  expect(rolesAndNames).toContain("heading:Response");
  expect(rolesAndNames).toContain("heading:Receipt");
  expect(tree.nodes.some((node) => node.properties?.some((property) => property.name === "live" && property.value?.value === "polite"))).toBe(true);
});

test("WCAG 2.2 A and AA automated checks report no violations", async ({ page }) => {
  await mockWorkbench(page);
  await page.goto("/");
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(results.violations).toEqual([]);
});

test("forced colors, zoom, narrow reflow, and focus stability preserve the workflow", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Viewport variants are covered once in desktop Chromium.");
  await page.emulateMedia({ forcedColors: "active", reducedMotion: "reduce" });
  await mockWorkbench(page, { delayRun: 50 });
  await page.goto("/");

  const runButton = page.getByRole("button", { name: "Run committed scenario" });
  await runButton.focus();
  const outline = await runButton.evaluate((element) => getComputedStyle(element).outlineStyle);
  expect(outline).not.toBe("none");
  await page.keyboard.press("Enter");
  await expect(page.getByText("Request complete and accounting reconciled.")).toBeVisible();
  await expect(page.locator(".primary-action")).toBeFocused();

  // This viewport matches 200% reflow on a 1280-pixel desktop layout.
  await page.setViewportSize({ width: 640, height: 800 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.setViewportSize({ width: 320, height: 800 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await expect(page.getByRole("button", { name: "Run committed scenario" })).toBeVisible();
});

test("owner live-control state also passes the automated accessibility gate", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Owner accessibility variant is covered once in desktop Chromium.");
  await mockWorkbench(page, { role: "owner" });
  await page.goto("/");
  await page.getByRole("checkbox", { name: /30 minutes, 20 provider requests, or \$1.00/ }).check();
  await page.getByRole("button", { name: "Arm bounded live session" }).click();
  await expect(page.getByText("Live armed", { exact: true })).toBeVisible();
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(results.violations).toEqual([]);
});

test("mobile order, reflow, and reduced motion remain usable", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "Mobile geometry is covered in the mobile project.");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockWorkbench(page);
  await page.goto("/");

  const safety = await page.locator(".safety-context").boundingBox();
  const controls = await page.locator(".controls").boundingBox();
  const response = await page.locator(".response").boundingBox();
  const receipt = await page.locator(".receipt").boundingBox();
  expect(safety && controls && response && receipt).toBeTruthy();
  expect(safety!.y).toBeLessThan(controls!.y);
  expect(controls!.y).toBeLessThan(response!.y);
  expect(response!.y).toBeLessThan(receipt!.y);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
});
