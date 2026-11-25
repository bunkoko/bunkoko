// ios/Design/AppIcon*.svg を 1024px の PNG にして、アプリのアセットカタログに入れる。
// Playwright（Chromium）を使う。App Store の決まりで、アイコンには透明な部分（アルファ）を入れない。
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const here = dirname(fileURLToPath(import.meta.url));
const target = join(here, "..", "App", "Assets.xcassets", "AppIcon.appiconset");

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1024, height: 1024 }, deviceScaleFactor: 1 });
for (const name of ["AppIcon", "AppIcon-Dark", "AppIcon-Tinted"]) {
  const svg = readFileSync(join(here, `${name}.svg`), "utf8").replace("<svg ", '<svg width="1024" height="1024" ');
  await page.setContent(`<html><body style="margin:0;background:#000">${svg}</body></html>`);
  const out = join(target, `${name}.png`);
  await page.screenshot({ path: out, clip: { x: 0, y: 0, width: 1024, height: 1024 } });
  // 透明な部分をなくす（RGB にする）
  execFileSync("python3", ["-c", "import sys; from PIL import Image; Image.open(sys.argv[1]).convert('RGB').save(sys.argv[1])", out]);
  console.log(`wrote ${out}`);
}
await browser.close();
