# 宣传资料

给想快速了解远窗、或想转发给朋友的人看的素材。

| 文件 | 是什么 |
|---|---|
| [`anzhuo-intro.pdf`](anzhuo-intro.pdf) | 4 页项目介绍：价值与功能、用法与页面、架构与安全 |
| [`cards/anzhuo-card-1.png`](cards/anzhuo-card-1.png) … `card-4` | 4 张 2:3 的手机竖图（1620×2430）：封面 / 为什么需要 / 三步用起来 / 架构与安全 |
| `src/` | 上面这些的 HTML 源文件（`pdf.html`、`cards.html`、共用样式） |
| `render.mjs` | 一键重新生成：`npm i playwright && npx playwright install chromium && node render.mjs` |

图里的界面截图来自演示模式（`scripts/demo.py`）里的虚构对话，不含任何真实数据。
