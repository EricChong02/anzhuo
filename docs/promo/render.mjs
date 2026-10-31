// 重新生成宣传资料：node render.mjs   （需要先 npm i playwright，并装好它的 Chromium：npx playwright install chromium）
// 输出：anzhuo-intro.pdf（4 页）和 cards/anzhuo-card-1..4.png（2:3，1620×2430）。素材来自 ../images 里的演示截图。
//
// 为什么 PDF 每一页是“高清图片”而不是矢量：版面里设备外框用了带模糊的阴影，Chrome 导出矢量 PDF 时会把阴影存成半透明遮罩，
// iPhone “文件”App 的查看器不认，会画成一块块灰色方框。整页转成 3 倍清晰度的图片再嵌进 PDF，所有查看器看到的都一样。
import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import fs from 'fs';
import path from 'path';
const here = path.dirname(fileURLToPath(import.meta.url));
const b = await chromium.launch();

// 1) 版面截成 3 倍清晰度的 JPEG（A4 比例 794×1123 CSS 像素）
let ctx = await b.newContext({ viewport: { width: 794, height: 1123 }, deviceScaleFactor: 3 });
let p = await ctx.newPage();
await p.goto('file://' + path.join(here, 'src/pdf.html')); await p.waitForTimeout(800);
const pages = await p.$$('.page');
const tmp = fs.mkdtempSync(path.join(here, '.tmp-'));
const shots = [];
for (let i = 0; i < pages.length; i++) {
  const f = path.join(tmp, `p${i + 1}.jpg`);
  await pages[i].screenshot({ path: f, type: 'jpeg', quality: 92 });
  shots.push(f);
}
// 2) 把这几张图按页嵌进 PDF
const html = `<!doctype html><style>@page{size:794px 1123px;margin:0}*{margin:0}img{display:block;width:794px;height:1123px;page-break-after:always}img:last-child{page-break-after:auto}</style>` +
  shots.map(f => `<img src="data:image/jpeg;base64,${fs.readFileSync(f).toString('base64')}">`).join('');
const q = await ctx.newPage();
await q.setContent(html); await q.waitForTimeout(500);
await q.pdf({ path: path.join(here, 'anzhuo-intro.pdf'), width: '794px', height: '1123px', printBackground: true, preferCSSPageSize: true });
fs.rmSync(tmp, { recursive: true, force: true });

// 3) 手机上看的 2:3 图：540×810 的版面，3 倍清晰度
ctx = await b.newContext({ viewport: { width: 560, height: 830 }, deviceScaleFactor: 3 });
p = await ctx.newPage();
await p.goto('file://' + path.join(here, 'src/cards.html')); await p.waitForTimeout(900);
for (const i of [1, 2, 3, 4]) await (await p.$('#c' + i)).screenshot({ path: path.join(here, `cards/anzhuo-card-${i}.png`) });
await b.close();
console.log('done');
