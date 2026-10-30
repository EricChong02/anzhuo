// 重新生成宣传资料：node render.mjs   （需要先 npm i playwright，并装好它的 Chromium：npx playwright install chromium）
// 输出：anzhuo-intro.pdf（4 页）和 cards/anzhuo-card-1..4.png（2:3，1620×2430）。素材来自 ../images 里的演示截图。
import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import path from 'path';
const here = path.dirname(fileURLToPath(import.meta.url));
const b = await chromium.launch();

// PDF：A4 比例的 4 页
let ctx = await b.newContext({ viewport: { width: 794, height: 1123 }, deviceScaleFactor: 2 });
let p = await ctx.newPage();
await p.goto('file://' + path.join(here, 'src/pdf.html')); await p.waitForTimeout(800);
await p.pdf({ path: path.join(here, 'anzhuo-intro.pdf'), width: '794px', height: '1123px', printBackground: true, preferCSSPageSize: true });

// 手机上看的 2:3 图：540×810 的版面，3 倍清晰度
ctx = await b.newContext({ viewport: { width: 560, height: 830 }, deviceScaleFactor: 3 });
p = await ctx.newPage();
await p.goto('file://' + path.join(here, 'src/cards.html')); await p.waitForTimeout(900);
for (const i of [1, 2, 3, 4]) await (await p.$('#c' + i)).screenshot({ path: path.join(here, `cards/anzhuo-card-${i}.png`) });
await b.close();
console.log('done');
