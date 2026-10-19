import { chromium } from "@playwright/test";
import { readFile, mkdir, writeFile } from "node:fs/promises";
const dir = new URL("../../test-results/", import.meta.url);
await mkdir(dir, { recursive: true });
const password = (
  await readFile(new URL("../../.admin-password", import.meta.url), "utf8")
).trim();
const browser = await chromium.launch({ channel: "chrome", headless: true });
const checks = [],
  errors = [];
try {
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
    viewport: { width: 1440, height: 1000 },
  });
  const page = await context.newPage();
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("https://127.0.0.1:18443");
  await page.getByLabel("账号", { exact: true }).fill("admin");
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("button", { name: "实时监考", exact: true }).waitFor();
  checks.push("login");
  await page.screenshot({
    path: new URL("wall-desktop.png", dir).pathname,
    fullPage: true,
  });
  for (const name of [
    "选手名单",
    "录像归档",
    "原件提交",
    "告警与日志",
    "设置",
  ]) {
    await page.getByRole("button", { name, exact: true }).click();
    await page.waitForTimeout(500);
    await page.locator(".loading").waitFor({ state: "detached" });
    if ((await page.locator("#content .notice").count()) && name !== "设置") {
      const text = await page.locator("#content .notice").allTextContents();
      if (text.some((t) => t.includes("失败"))) throw Error(name + text);
    }
    checks.push("tab-" + name);
    await page.screenshot({
      path: new URL("view-" + encodeURIComponent(name) + ".png", dir).pathname,
      fullPage: true,
    });
  }
  await page.getByRole("button", { name: "选手名单", exact: true }).click();
  await page.getByRole("button", { name: "添加选手", exact: true }).click();
  const cid = "TEST-UI-" + Date.now();
  await page.getByLabel("编号", { exact: true }).fill(cid);
  await page.getByLabel("姓名", { exact: true }).fill("界面验收选手");
  await page.getByLabel("组别", { exact: true }).fill("TEST 界面验收");
  await page.getByRole("button", { name: "添加", exact: true }).click();
  await page.locator("tr").filter({ hasText: cid }).waitFor();
  checks.push("create-contestant");
  await page
    .locator("tr")
    .filter({ hasText: cid })
    .getByRole("button", { name: "编辑选手", exact: true })
    .click();
  await page.getByLabel("停用选手", {exact:true}).check();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.locator("dialog").waitFor({ state: "hidden" });
  checks.push("disable-contestant");
  await page.getByRole("button", { name: "录像归档", exact: true }).click();
  const report = page.getByRole("button", { name: "查看校验报告" }).first();
  await report.waitFor();
  if (await report.count()) {
    await report.click();
    await page
      .getByRole("heading", { name: "录像校验报告", exact: true })
      .waitFor();
    await page.screenshot({
      path: new URL("media-report.png", dir).pathname,
      fullPage: true,
    });
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    checks.push("media-report");
  }
  await page.setViewportSize({ width: 390, height: 844 });
  for (const name of [
    "实时监考",
    "选手名单",
    "录像归档",
    "原件提交",
    "告警与日志",
    "设置",
  ]) {
    await page.getByRole("button", { name, exact: true }).click();
    await page.waitForTimeout(400);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    );
    if (overflow) throw Error("Page overflow: " + name);
    await page.screenshot({
      path: new URL("mobile-" + encodeURIComponent(name) + ".png", dir)
        .pathname,
      fullPage: true,
    });
    checks.push("mobile-" + name);
  }
  if (errors.length) throw Error(errors.join("\n"));
  await writeFile(
    new URL("browser-results.json", dir),
    JSON.stringify({ checks, errors, passed: checks.length }, null, 2),
  );
  console.log(JSON.stringify({ passed: checks.length, checks, errors }));
} finally {
  await browser.close();
}
